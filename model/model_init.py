"""
Initialization for model initialization, including
- Zero-shot Feature extraction: CLIP, DinoV2
- Zero-shot GroundingDino
- Zero-shot segmentation: SAM2
"""
import torch
from PIL import Image
from typing import List, Tuple, Dict, Any, Optional, Union
from abc import ABC, abstractmethod
import torchvision
import sys
sys.path.append('.')
import numpy as np

from GroundingDINO.groundingdino.models import build_model
from GroundingDINO.groundingdino.util.box_ops import box_cxcywh_to_xyxy
from GroundingDINO.groundingdino.util.slconfig import SLConfig
from GroundingDINO.groundingdino.util.utils import clean_state_dict, get_phrases_from_posmap
from sam2_repo.sam2.build_sam import build_sam2
from sam2_repo.sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

import open_clip
from transformers import AutoImageProcessor, AutoModel
NMS_THRESHOLD = 0.5


class FeatExtractInterace(ABC):
    @abstractmethod
    def extract_features(self, images: List[Union[torch.Tensor, Image.Image]]) -> torch.Tensor:
        pass


class DinoV2Model(FeatExtractInterace):
    def __init__(self, model_name: str = 'facebook/dinov2-large'):
        print(f"[DinoV2] Initializing with model: {model_name}")
        self.model = AutoModel.from_pretrained(model_name)
        self.processor = AutoImageProcessor.from_pretrained(model_name)
        self.model.eval()
        print("[DinoV2] Model initialized and set to eval mode")
    
    def extract_features(self, images: List[Union[torch.Tensor, Image.Image]]) -> torch.Tensor:
        print(f"[DinoV2] Extracting features from {len(images)} images")
        inputs = self.procesor(images=images, return_tensors='pt')
        print(f"[DinoV2] Input shape: {inputs['pixel_values'].shape}")
        with torch.no_grad():
            outputs = self.model(**inputs)
        features = outputs.last_hidden_state[:, 0, :]
        print(f"[DinoV2] Extracted features shape: {features.shape}")
        return features


class CLIPModel(FeatExtractInterace):
    def __init__(self, model_name: str) -> None:
        print(f"[CLIP] Initializing with model: {model_name}")
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model_name)
        self.model.eval()
        print("[CLIP] Model initialized and set to eval mode")
    
    def extract_features(self, images: List[Union[torch.Tensor, Image.Image]]) -> torch.Tensor:
        print(f"[CLIP] Extracting features from {len(images)} images")
        if isinstance(images[0], Image.Image):
            print("[CLIP] Converting PIL images to tensors")
            images = [self.preprocess(image) for image in images]
        
        stacked_images = torch.stack(images)
        print(f"[CLIP] Input tensor shape: {stacked_images.shape}")
        
        with torch.no_grad():
            features = self.model.encode_image(stacked_images)
        print(f"[CLIP] Extracted features shape: {features.shape}")
        return features


class GroundingDino:
    def __init__(
        self,
        model_checkpoint_path: str,
        model_config_path='../GroundingDINO/groundingdino/config/GroundingDINO_SwinT_OGC.py',
        device: str = 'cuda'
    ):
        print(f"[GroundingDino] Initializing with config: {model_config_path}")
        print(f"[GroundingDino] Checkpoint path: {model_checkpoint_path}")
        self.model = self._load_model_from_config(
            config_file=model_config_path,
            checkpoint_path=model_checkpoint_path,
            cpu_only=False,
            device=device
        )
        print("[GroundingDino] Initialization complete")

    def _load_model_from_config(
        self,
        config_file: str,
        checkpoint_path: str,
        cpu_only: bool = False,
        device: Optional[str] = None
    ) -> torch.nn.Module:
        print(f"[GroundingDino] Loading model from config: {config_file}")
        args = SLConfig.fromfile(config_file)
        args.device = "cpu" if cpu_only else ("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[GroundingDino] Using device: {args.device}")
        
        model = build_model(args)
        print("[GroundingDino] Model built successfully")
        
        checkpoint = torch.load(checkpoint_path, map_location='cpu', weights_only=True)
        print("[GroundingDino] Checkpoint loaded")
        
        model.load_state_dict(clean_state_dict(checkpoint['model']), strict=False)
        model.eval()
        print("[GroundingDino] Model loaded and set to evaluation mode")
        return model

    def predict(
        self,
        images: torch.Tensor,
        captions: List[str],
        box_threshold: float,
        text_threshold: float,
        device: str = "cuda"
    ) -> Tuple[List[torch.Tensor], List[torch.Tensor], List[List[str]]]:
        print(f"[GroundingDino] Processing batch of {len(captions)} images")
        print(f"[GroundingDino] Input image tensor shape: {images.shape}")
        print(f"[GroundingDino] Box threshold: {box_threshold}, Text threshold: {text_threshold}")
        
        captions = [cap.lower().strip() + "." if not cap.endswith(".") else cap.lower().strip() for cap in captions]
        print(f"[GroundingDino] Processed captions: {captions}")

        self.model = self.model.to(device)
        images = images.to(device)
        print(f"[GroundingDino] Models and images moved to {device}")

        with torch.no_grad():
            outputs = self.model(images, captions=captions)

        logits = outputs["pred_logits"].cpu().sigmoid()
        boxes = outputs["pred_boxes"].cpu()
        print(f"[GroundingDino] Raw output shapes - Logits: {logits.shape}, Boxes: {boxes.shape}")

        all_boxes = []
        all_scores = []
        all_phrases = []

        for i in range(images.size(0)):
            print(f"\n[GroundingDino] Processing image {i+1}/{images.size(0)}")
            logits_i = logits[i]
            boxes_i = boxes[i]
            
            mask = logits_i.max(dim=1)[0] > box_threshold
            logits_i = logits_i[mask]
            boxes_i = boxes_i[mask]
            
            print(f"[GroundingDino] After threshold - Boxes shape: {boxes_i.shape}")
            
            if logits_i.shape[0] == 0:
                print("[GroundingDino] No boxes found above threshold")
                all_boxes.append(torch.empty((0, 4)))
                all_scores.append(torch.empty((0,)))
                all_phrases.append([])
                continue

            tokenizer = self.model.tokenizer
            tokenized = tokenizer(captions[i])
            phrases_i = [
                get_phrases_from_posmap(logit > text_threshold, tokenized, tokenizer).replace('.', '')
                for logit in logits_i
            ]
            scores_i = logits_i.max(dim=1)[0]
            boxes_xyxy = box_cxcywh_to_xyxy(boxes_i)
            
            print(f"[GroundingDino] Before NMS - Boxes: {len(boxes_xyxy)}, Phrases: {len(phrases_i)}")
            boxes_i, scores_i, phrases_i = self.apply_nms(boxes_xyxy, scores_i, phrases_i, NMS_THRESHOLD)
            print(f"[GroundingDino] After NMS - Boxes: {len(boxes_i)}, Phrases: {len(phrases_i)}")
            
            all_boxes.append(boxes_i)
            all_scores.append(scores_i)
            all_phrases.append(phrases_i)

        print("[GroundingDino] Prediction complete")
        return all_boxes, all_scores, all_phrases

    @staticmethod
    def apply_nms(
        boxes: torch.Tensor,
        scores: torch.Tensor,
        phrases: List[str],
        iou_threshold: float = 0.5    
    ) -> Tuple[torch.Tensor, torch.Tensor, List[str]]:
        print(f"[GroundingDino] Applying NMS with IoU threshold: {iou_threshold}")
        print(f"[GroundingDino] Before NMS - Boxes: {boxes.size(0)}")
        
        keep_indices = torchvision.ops.nms(boxes, scores, iou_threshold)
        print(f"[GroundingDino] After NMS - Kept boxes: {len(keep_indices)}")
        return boxes[keep_indices], scores[keep_indices], [phrases[i] for i in keep_indices]


class SegmentModel:
    def __init__(
        self,
        checkpoint: str,
        model_cfg: str
    ):
        print(f"[SegmentModel] Initializing with config: {model_cfg}")
        print(f"[SegmentModel] Checkpoint path: {checkpoint}")
        
        self.model = build_sam2(
            model_cfg,
            checkpoint,
            torch.device('cuda'),
            apply_postprocessing=False
        )
        print("[SegmentModel] Model built successfully")
        
        self.model.to(torch.device('cuda'))
        print("[SegmentModel] Model moved to cuda")
        
        self.mask_generator = SAM2AutomaticMaskGenerator(self.model)
        print("[SegmentModel] Mask generator initialized")

    @torch.no_grad()
    def predict(self, image: np.ndarray) -> List[Dict[str, Any]]:
        print(f"[SegmentModel] Processing image with shape: {image.shape}")
        masks = self.mask_generator.generate(image)
        print(f"[SegmentModel] Generated {len(masks)} masks")
        for i, mask in enumerate(masks):
            print(f"[SegmentModel] Mask {i+1} - Area: {mask['area']}, "
                  f"BBox: {mask['bbox']}, "
                  f"Predicted IoU: {mask['predicted_iou']:.3f}, "
                  f"Stability Score: {mask['stability_score']:.3f}")
        return masks