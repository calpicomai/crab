"""NanoOwlBackend — open-vocabulary detection via NVIDIA NanoOWL (OWL-ViT + TensorRT).

Text-prompted ("a person", "a red ball", ...), so the future LLM can steer what
the robot looks for at runtime via set_prompts() / the /prompts endpoint.

Requires (JetPack): torch, transformers, torch2trt, TensorRT, the `nanoowl`
package from source, and a prebuilt image-encoder engine:
    python3 -m nanoowl.build_image_encoder_engine data/owl_image_encoder_patch32.engine
Everything is imported lazily inside load().
"""

from __future__ import annotations

import logging

import numpy as np

from .. import config
from ..types import Detection
from .base import DetectorBackend

logger = logging.getLogger("perception.nanoowl")


def _to_list(value) -> list:
    """Best-effort convert a torch tensor / ndarray / sequence to a Python list."""
    if value is None:
        return []
    for attr in ("detach", "cpu"):  # torch tensor -> cpu
        if hasattr(value, attr):
            value = getattr(value, attr)()
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


class NanoOwlBackend(DetectorBackend):
    name = "nanoowl"

    def __init__(
        self,
        model: str | None = None,
        engine: str | None = None,
        threshold: float | None = None,
        prompts: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.model = model or config.NANOOWL_MODEL
        self.engine = engine or config.NANOOWL_ENGINE
        self.threshold = threshold if threshold is not None else config.NANOOWL_THRESHOLD
        self.prompts = list(prompts if prompts is not None else config.NANOOWL_PROMPTS)
        self._predictor = None
        self._text_encodings = None

    def load(self) -> None:
        from nanoowl.owl_predictor import OwlPredictor  # heavy: torch2trt + TensorRT

        logger.info("Loading NanoOWL (%s, engine=%s)", self.model, self.engine)
        self._predictor = OwlPredictor(self.model, image_encoder_engine=self.engine)
        self._encode_prompts()
        self._loaded = True

    def unload(self) -> None:
        self._predictor = None
        self._text_encodings = None
        self._loaded = False
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def set_prompts(self, prompts: list[str]) -> None:
        self.prompts = list(prompts)
        if self._loaded:
            self._encode_prompts()

    def _encode_prompts(self) -> None:
        # Precompute text encodings so per-frame predict() only runs the image path.
        if self._predictor is not None and self.prompts:
            self._text_encodings = self._predictor.encode_text(self.prompts)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if self._predictor is None or not self.prompts:
            return []
        from PIL import Image

        # BGR (OpenCV) -> RGB PIL image.
        image = Image.fromarray(frame[:, :, ::-1])
        output = self._predictor.predict(
            image=image,
            text=self.prompts,
            text_encodings=self._text_encodings,
            threshold=self.threshold,
        )

        boxes = _to_list(getattr(output, "boxes", None))
        labels = _to_list(getattr(output, "labels", None))
        scores = _to_list(getattr(output, "scores", None))

        detections: list[Detection] = []
        for i, box in enumerate(boxes):
            x1, y1, x2, y2 = (int(v) for v in box[:4])
            label_idx = int(labels[i]) if i < len(labels) else 0
            label = self.prompts[label_idx] if 0 <= label_idx < len(self.prompts) else str(label_idx)
            score = float(scores[i]) if i < len(scores) else 0.0
            detections.append(
                Detection(label=label, score=score, box=[x1, y1, x2, y2], source=self.name)
            )
        return detections
