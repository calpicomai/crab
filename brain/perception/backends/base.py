"""DetectorBackend — the interface every detector implements.

The seam that lets YOLO and NanoOWL (and a dummy) be fused by one engine and
loaded/unloaded independently for the Jetson RAM budget.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np

from ..types import Detection


class DetectorBackend(ABC):
    """A loadable object detector operating on BGR numpy frames."""

    #: short, stable name used in Detection.source and the /load,/unload API.
    name: str = "base"

    def __init__(self) -> None:
        self._loaded = False

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @abstractmethod
    def load(self) -> None:
        """Load the model into memory. Raise on failure (e.g. missing libs)."""

    @abstractmethod
    def unload(self) -> None:
        """Free the model and any GPU memory. Safe to call when not loaded."""

    @abstractmethod
    def detect(self, frame: np.ndarray) -> list[Detection]:
        """Run detection on one BGR frame and return detections."""

    def set_prompts(self, prompts: list[str]) -> None:
        """Set open-vocabulary prompts. No-op for fixed-class detectors."""
        return None
