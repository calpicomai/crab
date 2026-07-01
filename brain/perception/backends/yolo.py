"""YoloBackend — Ultralytics YOLO (fixed COCO classes).

Fast, lightweight, and simple to stand up. Loads lazily so importing the package
never pulls in torch/ultralytics on machines that don't have them.
"""

from __future__ import annotations

import logging

import numpy as np

from .. import config
from ..types import Detection
from .base import DetectorBackend

logger = logging.getLogger("perception.yolo")


class YoloBackend(DetectorBackend):
    name = "yolo"

    def __init__(self, weights: str | None = None, conf: float | None = None) -> None:
        super().__init__()
        self.weights = weights or config.YOLO_WEIGHTS
        self.conf = conf if conf is not None else config.YOLO_CONF
        self._model = None

    def load(self) -> None:
        from ultralytics import YOLO  # heavy: torch + ultralytics

        logger.info("Loading YOLO weights %s", self.weights)
        self._model = YOLO(self.weights)
        self._loaded = True

    def unload(self) -> None:
        self._model = None
        self._loaded = False
        try:  # best-effort GPU free
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if self._model is None:
            return []
        results = self._model.predict(frame, conf=self.conf, verbose=False)
        if not results:
            return []
        result = results[0]
        names = result.names  # {class_id: label}
        detections: list[Detection] = []
        for box in result.boxes:
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
            cls = int(box.cls[0])
            detections.append(
                Detection(
                    label=str(names.get(cls, cls)),
                    score=float(box.conf[0]),
                    box=[x1, y1, x2, y2],
                    source=self.name,
                )
            )
        return detections
