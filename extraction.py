# extraction.py
import sys
import torch.multiprocessing.spawn
sys.path.append('.')
import os
import json
import argparse
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, DistributedSampler
import numpy as np
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from pathlib import Path
from tqdm import tqdm
from dataclasses import dataclass
from model import CLIPModel, DinoV2Model, FeatExtractInterace
from faisss import FaissIndexStrategy
from datasets import ObjectDataset, ImageProcessor
import logging
from datetime import timedelta
from PIL import Image
from datetime import datetime
import time
from torchvision import transforms
from enum import Enum
from torchvision.utils import save_image
import os



@dataclass
class ModelConfig:
    name:Optional[str] = None
    type_model:Optional[str] = None
    pretrained_path:Optional[str] = None
    model_config_path:Optional[str] = None

class ModelFactory:
    @staticmethod
    def create_model(model_type: str, config: ModelConfig) -> FeatExtractInterace:
        """Create a single model instance based on the specified type.

        Args:
            model_type (ModelType): Type of model to create
            config (ModelConfig): Model configuration

        Returns:
            FeatExtractInterace: Initialized model instance
        """
        
        if model_type == "CLIP":
            return CLIPModel(
                model_name=config.name
            )
        elif model_type == "DINOV2":
            return DinoV2Model(
                model_name=config.name
            )
        else:
            raise ValueError(f"Unsupported model type: {model_type}")

@dataclass
class FaissConfig:
    index_type:str
    dimension:int
    use_gpu:bool
    device:int
    metric:str
    nlist:Optional[int] = None
    M: Optional[int] = None
    nbits: Optional[int] = None
    nprobe: Optional[int] = None


@dataclass
class DataConfig:
    data_dir:str
    batch_size:int
    num_workers:int
    target_size: Optional[Tuple[int, int]] = None


@dataclass
class Config:
    models: Dict[str, ModelConfig]
    faiss: FaissConfig
    data: DataConfig
    output_dir: str
    distributed:bool
    world_size:int 
    seed:int=42

# =======================
# Utility Functions
# =======================

def load_config(config_path:str) -> Config:
    """Load configuration

    Args:
        config_path (str): Path to the configuration JSON file

    Returns:
        Config: Parsed Configuration
    """
    with open(config_path, 'r') as f:
        config_dict = json.load(f)
    
    config = Config(
        models= {k: ModelConfig(**v) for k, v in config_dict['models'].items()},
        faiss=FaissConfig(**config_dict['faiss']),
        data = DataConfig(**config_dict['data']),
        output_dir= config_dict['output_dir'],
        distributed=config_dict['distributed'],
        world_size=config_dict['world_size'],
        seed = config_dict.get('seed',42)
    )
    return config

def setup_logging(output_dir: str, rank: Optional[int] = None) -> Tuple[logging.Logger, Path]:
    """
    Setup logging configuration with both file and console handlers.

    Args:
        output_dir (str): Directory for log files.
        rank (Optional[int]): Process rank for distributed training.

    Returns:
        Tuple[logging.Logger, Path]: Configured logger and path to the log file.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path(output_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    rank_suffix = f"_rank{rank}" if rank is not None else ""
    log_file = log_dir / f"process_log_{timestamp}{rank_suffix}.txt"
    
    logger = logging.getLogger(f"Rank{rank}" if rank is not None else "Main")
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
        )
        file_handler.setFormatter(file_formatter)
        
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter('%(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
    
    return logger, log_file

def setup_distributed(rank:int, world_size:int, logger:logging.Logger, timeout:int=3600) -> None:
    """Initialize distributed training

    Args:
        rank (int): Rank of the current process
        world_size (int): Total number of processes
        logger (logging.logger): Logging instances
    """
    try:
        os.environ['MASTER_ADDR'] = 'localhost'
        os.environ['MASTER_PORT'] = '12355'
        dist.init_process_group(
            backend="nccl", 
            rank=rank, 
            world_size=world_size,
            timeout=timedelta(seconds=timeout)
        )
        logger.info(f"Distributed process group initialized with rank {rank}/{world_size}")
        torch.cuda.set_device(rank)
        
    except Exception as e:
        logger.error(f"Failed to initialize distributed process group: {e}")
        raise e

def cleanup(logger: logging.Logger)->None:
    """Clean up the distributed training

    Args:
        logger (logging.logger): Logger instance
    """
    dist.destroy_process_group()
    logger.info("Destroyed distributed process group")



# =======================
# Feature Extraction Class
# =======================
class FeatureExtractor:
    """
    Handles feature extraction 
    """

    def __init__(
        self,
        model_type: str,
        model_config: ModelConfig,
        rank: int,
        config: Config,
        logger: logging.Logger
    ):
        """
        Initialize the FeatureExtractor with models and wrap them with DDP if necessary.

        Args:
            model_type (ModelType): Type of model to use
            model_config (ModelConfig): Configuration for the model
            rank (int): Rank of the current process.
            config (Config): Configuration.
            logger (logging.Logger): Logger instance.
        """
        self.model_type = model_type

        self.config = config
        self.logger = logger

        self.device = torch.device(f'cuda:{rank}' if torch.cuda.is_available() else 'cpu')
        self.base_model = ModelFactory.create_model(model_type, model_config)
        self.base_model.model = self.base_model.model.to(self.device)

        if self.config.distributed:
            self.base_model.model = DDP(self.base_model.model, device_ids=[rank], output_device=rank)
            self.logger.info("Wrapped models with DistributedDataParallel")
        self.logger.info("FeatureExtractor initialized and models set to eval mode")

    def save_sample_images(self, original_images: torch.Tensor, masked_images: torch.Tensor, num_samples: int = 5):
        """
        Save a sample of original and masked images as separate files in a folder.
        
        Args:
            original_images (torch.Tensor): Batch of original images.
            masked_images (torch.Tensor): Batch of masked images.
            num_samples (int): Number of sample images to save.
        """
        num_samples = min(num_samples, original_images.size(0))
        
        # Create directories to save the images if they don't exist
        save_dir = os.path.join(self.config.output_dir, "sample_images")
        original_dir = os.path.join(save_dir, "original")
        masked_dir = os.path.join(save_dir, "masked")
        os.makedirs(original_dir, exist_ok=True)
        os.makedirs(masked_dir, exist_ok=True)
        
        for i in range(num_samples):
            # Save original image
            save_image(original_images[i], os.path.join(original_dir, f"original_{i}.png"), normalize=True)
            
            # Save masked image
            save_image(masked_images[i], os.path.join(masked_dir, f"masked_{i}.png"), normalize=True)
        
        self.logger.info(f"Saved {num_samples} sample images to {save_dir}")
        self.logger.info(f"Original images saved in: {original_dir}")
        self.logger.info(f"Masked images saved in: {masked_dir}")
    

    def _prepare_binary_mask(self, mask: torch.Tensor, threshold: float = 0.5) -> torch.Tensor:
        """
        Convert mask to binary (0 or 1) values.
        
        Args:
            mask (torch.Tensor): Input mask tensor
            threshold (float): Threshold value for binarization
            
        Returns:
            torch.Tensor: Binary mask tensor
        """
        binary_mask = (mask > threshold).float()
        return binary_mask
    
    @torch.no_grad()
    def extract_features(
        self,
        images: torch.Tensor,
        masks: torch.Tensor
    ) -> np.ndarray:
        """
        Extract features from masked regions of images using CLIP.

        Args:
            images (torch.Tensor): Batch of images (B, C, H, W).
            masks (torch.Tensor): Batch of masks (B,C, H, W).

        Returns:
            np.ndarray: CLIP feature vectors (B, D).
        """
        print("Images dimension: ", images.dim())
        print("Mask dimension: ", masks.dim())
        binary_masks = self._prepare_binary_mask(masks)  
        
        if binary_masks.dim() < 4:
            binary_masks = binary_masks.unsqueeze(1)
        binary_masks = binary_masks.expand(-1, images.size(1), -1, -1) 
        masked_images = images * binary_masks
        
        self.save_sample_images(images, masked_images)
        self.logger.info(f"Starting {self.model_type} feature extraction")
        features = self.base_model.extract_features(masked_images)      
        features_numpy = features.cpu().detach().numpy()
        self.logger.info(f"Extracted features with shape {features_numpy.shape}")
            
        return features_numpy

def process_batch(
    batch: Tuple[torch.tensor, Dict[str, Any]],
    extractor: FeatureExtractor,
    device: torch.device,
    logger: logging.Logger,
    config: Config
) -> Tuple[np.ndarray, List[Dict[str,Any]]]:
    """Process batch of images, extract feature, and collect metadata

    Args:
        batch (Tuple[torch.tensor, Dict[str, Any]]): Batch of images and metadata
        extractor (FeatureExtractor): FeatureExtractor 
        device (torch.device): device
        logger (logging.Logger): logger

    Returns:
        Tuple[np.ndarray, List[Dict[str,Any]]]: Extracted features and corresponding  metadata
    """
    images, metadata = batch
    images = images.to(device)
    
    mask_list = metadata['mask_path']
    mask_list = [mask.to(device) if torch.is_tensor(mask) else mask for mask in mask_list]
    batch_size = images.size(0)

    logger.info(f"Processing batch of size {batch_size}")
    images = images.to(device)
    
    masks = torch.zeros(
        (batch_size, 1, images.size(2), images.size(3)), dtype=torch.float32
    ).to(device)

    for i, mask in enumerate(mask_list):
        try:
            mask = Image.open(mask).convert('L') 
            mask = ImageProcessor.preprocess_image(mask, config.data.target_size[0])
            mask = transforms.ToTensor()(mask)  
            masks[i] = mask
        except Exception as e:
            logger.error(f"Error processing mask {mask}: {e}")
            raise
    features = extractor.extract_features(images, masks)
    return features, metadata
# =======================
# Extraction and Indexing
# =======================

def run_extraction(
    rank:int, config: Config, model_type: str
) -> None:
    """Main extraction process for each GPU/process.

    Args:
        rank (int): Rank of the current process
        config (Config): Config class
    """
    loader_start_time = time.time()
    logger, _ = setup_logging(config.output_dir, rank)
    logger.info(f"Rank {rank}: Starting extraction process")
    if config.distributed:
        setup_distributed(rank, config.world_size, logger)
        torch.cuda.set_device(rank)
        logger.info(f"Rank {rank}: Set CUDA device to {rank}")
    try:
        device = torch.device(f"cuda:{rank}" if torch.cuda.is_available() else 'cpu')
        logger.info(f"Using device: {device}")
        logger.info("Initializing CLIP and DinoV2 model...")
        model_config = config.models[model_type]
        extractor = FeatureExtractor(
            model_type=model_type,
            model_config=model_config,
            rank=rank,
            config=config,
            logger=logger
        )

        # Initialize the dataset and the dataloader
        logging.info("Setting up the dataset and the dataloader")
        transformer = transforms.Compose([
            transforms.ToTensor()
        ])
        dataset = ObjectDataset(
            data_dir = config.data.data_dir,
            transform = transformer,
            target_size=config.data.target_size
        )
        sampler = DistributedSampler(dataset, shuffle=False) if config.distributed else None
        dataloader = DataLoader(
            dataset,
            batch_size=config.data.batch_size,
            num_workers=config.data.num_workers,
            sampler=sampler,
            pin_memory=True,
            shuffle=False,
            drop_last=False
        )
        logger.info(f"Dataset size: {len(dataset)}, Batch size: {config.data.batch_size}")

        if rank == 0:
            logger.info("Initializing Faiss index...")
            faiss_index = FaissIndexStrategy(
                index_type=config.faiss.index_type,
                dimension=config.faiss.dimension,  
                use_gpu=config.faiss.use_gpu,
                device=config.faiss.device,
                metric=config.faiss.metric,
                nlist=config.faiss.nlist,
                M=config.faiss.M,
                nbits=config.faiss.nbits,
                nprobe=config.faiss.nprobe
            )
            
            logger.info('Faiss indexes initialized')
        
        all_features = []
        all_metadata = {}

        logger.info("Starting batch processing")
        for batch_idx, batch in enumerate(
            tqdm(
                dataloader, desc=f"Processing batches (Rank {rank})"
            )
        ):
            try:
                features, metadata = process_batch(batch, extractor, device, logger, config)
                all_features.append(features)

                if all_metadata == {}:
                    all_metadata = metadata
                else:
                    
                    all_metadata['id'] = torch.cat((all_metadata['id'], metadata['id']), dim=0)
                    all_metadata['object_name'].extend(metadata['object_name'])
                    all_metadata['data_dir'].extend(metadata['data_dir'])
                    all_metadata['image_path'].extend(metadata['image_path'])
                    all_metadata['mask_path'].extend(metadata['mask_path'])
                    all_metadata['dataset_type'].extend(metadata['dataset_type'])
                    
                logger.info(f"Completed batch {batch_idx + 1}/{len(dataloader)}")
            except Exception as e:
                logger.error(f"Error processing batch {batch_idx}: {e}")
                raise
        if config.distributed:
            logger.info(f"Rank {rank} reached gather point at {time.time()}")
            dist.barrier()  
            logger.info(f"Rank {rank} passed barrier at {time.time()}")
            gathered_features = [None for _ in range(config.world_size)]
            gathered_metadata = [None for _ in range(config.world_size)]
            dist.all_gather_object(gathered_features, all_features)
            dist.all_gather_object(gathered_metadata, all_metadata)    
            flat_features = []
            flat_metadata = {}
            for proc_features in gathered_features:
                if proc_features is not None:
                    for feat_matrix in proc_features:
                        flat_features.extend(feat_matrix)
            print("Number of individual feature vectors:", len(flat_features))
            for proc_metadata in gathered_metadata:
                if proc_metadata is not None:
                    if flat_metadata == {}:
                        flat_metadata = proc_metadata
                    else:
                        flat_metadata['id'] = torch.cat((proc_metadata['id'], flat_metadata['id']), dim=0)
                        flat_metadata['object_name'].extend(proc_metadata['object_name'])
                        flat_metadata['data_dir'].extend(proc_metadata['data_dir'])
                        flat_metadata['image_path'].extend(proc_metadata['image_path'])
                        flat_metadata['mask_path'].extend(proc_metadata['mask_path'])
                        flat_metadata['dataset_type'].extend(proc_metadata['dataset_type'])

            index_pairs = list(enumerate(flat_metadata['id']))
            sorted_index_pairs = sorted(index_pairs, key=lambda x: x[1])
            sorted_indices = [pair[0] for pair in sorted_index_pairs]
            flat_features_array = np.array(flat_features)
            sorted_features = flat_features_array[sorted_indices]
            
            sorted_metadata = {
              'id': flat_metadata['id'][sorted_indices].tolist(),
              'object_name': [flat_metadata['object_name'][i] for i in sorted_indices],
              'data_dir': [flat_metadata['data_dir'][i] for i in sorted_indices],
              'image_path': [flat_metadata['image_path'][i] for i in sorted_indices],
              'mask_path': [flat_metadata['mask_path'][i] for i in sorted_indices],
              'dataset_type': [flat_metadata['dataset_type'][i] for i in sorted_indices]
            }
            all_features = sorted_features
            all_metadata = sorted_metadata
            print("Shape of all_features:", all_features.shape)
            print("Length of all metadata 'id':", len(all_metadata['id']))
            print("First few sorted IDs:", all_metadata['id'][:5])
        else:
            all_features = np.vstack(all_features)

        if rank == 0:
            
            output_dir = Path(config.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            faiss_index.add(all_features)
            index_path = output_dir / f"feature_index_{model_type.lower()}.index"
            faiss_index.save(str(index_path))
            logger.info(f"Faiss index saved to {index_path}")
            
            # Save metadata
            metadata_path = output_dir / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(all_metadata, f, indent=2)
            logger.info(f"Metadata saved to {metadata_path}")
            summary_path = output_dir / "processing_summary.txt"
            with open(summary_path, "w") as f:
                f.write(f"Processing Summary for {model_type}\n")
                f.write("================================\n\n")
                f.write(f"Processing completed at: {datetime.now()}\n")
                f.write(f"Total processing time: {(time.time() - loader_start_time):.2f} seconds\n")
                f.write(f"Total features processed: {all_features.shape[0]}\n")
                f.write(f"Number of images processed: {len(all_metadata)}\n")
                f.write(f"Model type used: {model_type}\n")
                f.write(f"Batch size used: {config.data.batch_size}\n")
                f.write(f"Number of workers: {config.data.num_workers}\n")
                f.write(f"Distributed processing: {config.distributed}\n")
                if config.distributed:
                    f.write(f"Number of GPUs used: {config.world_size}\n")
                f.write("\nConfiguration used:\n")
                f.write(json.dumps({
                    'models': {k: vars(v) for k, v in config.models.items()},
                    'faiss': vars(config.faiss),
                    'data': vars(config.data),
                    'output_dir': config.output_dir,
                    'distributed': config.distributed,
                    'world_size': config.world_size,
                    'seed': config.seed
                }, indent=2))
            logger.info(f"Processing summary saved to {summary_path}")
    
        logger.info("Extraction process completed successfully")

    except Exception as e:
        logger.error(f"Error in extraction process: {e}", exc_info=True)
        raise e
    finally:
        if config.distributed:
            cleanup(logger)

# =======================
# Main Function
# =======================

def main():
    """
    Main entry point for the extraction and indexing pipeline.
    """
    parser = argparse.ArgumentParser(description="Extraction and Indexing Pipeline")
    parser.add_argument('--config', type=str, required=True, help="Path to the config.json file")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        print(type(config.models['CLIP'].name))
        model_type = "CLIP" if config.models['CLIP'].name != "None" else "DINOV2"
        print(f"Using model: {model_type}")
        logger, _ = setup_logging(config.output_dir)
        logger.info("Starting main process")

        torch.manual_seed(config.seed)
        np.random.seed(config.seed)
        logger.info(f"Random seed set to {config.seed}")

        if config.distributed:
            logger.info(f"Launching distributed processes with world size {config.world_size}")
            torch.multiprocessing.spawn(
                run_extraction,
                args=(config,model_type),
                nprocs=config.world_size,
                join=True
            )
        else:
            logger.info("Running in single-process mode")
            run_extraction(rank=0, config=config, model_type=model_type)
        logger.info("Main process completed successfully")

    except Exception as e:
        logging.error(f"Error in main process: {e}", exc_info=True)
        raise e

    # =======================
    # Entry Point
    # =======================

if __name__ == "__main__":
    main()
