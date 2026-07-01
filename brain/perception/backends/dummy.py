"""DummyBackend — deterministic fake detections, no heavy dependencies.

Lets the whole perception pipeline (engine, server, test runner) run and be
tested off-hardware / in CI, and is the fallback the engine loads when a real
backend can't be imported.
"""

from __future__ import annotations

import numpy as np

from ..types import Detection
from .base import DetectorBackend


class DummyBackend(DetectorBackend):
    name = "dummy"

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def detect(self, frame: np.ndarray) -> list[Detection]:
        h, w = frame.shape[:2]
        # A single centered "person" box, scaled to the frame.
        return [
            Detection(
                label="person",
                score=0.9,
                box=[int(w * 0.25), int(h * 0.25), int(w * 0.75), int(h * 0.9)],
                source=self.name,
            )
        ]
