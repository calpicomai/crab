"""SimBlobBackend — a trivial detector for the SIM WORLD only.

The simulated camera (robot/simworld.py:render_camera) draws each obstacle as a
solid, vivid, distinctly-colored box on a muted sky/ground background. This
backend finds those boxes by masking vivid pixels and grouping them by quantized
color into bounding boxes — so the camera->perception->costmap path can be
exercised off-hardware, with no model/torch. It is NOT a real detector; use YOLO/
NanoOWL on the actual robot. Enable with PERCEPTION_BACKENDS=simblob.
"""

from __future__ import annotations

import numpy as np

from ..types import Detection
from .base import DetectorBackend

_VIVID = 110      # max-min channel spread above which a pixel is an obstacle, not background
_MIN_AREA = 40    # ignore tiny specks (px)


class SimBlobBackend(DetectorBackend):
    name = "simblob"

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if frame is None or frame.ndim != 3:
            return []
        arr = frame.astype(np.int16)
        vivid = (arr.max(2) - arr.min(2)) > _VIVID
        if not vivid.any():
            return []
        # Quantize color so each distinct obstacle color becomes one group.
        q = (arr // 64)
        key = q[..., 0] * 100 + q[..., 1] * 10 + q[..., 2]
        dets: list[Detection] = []
        for k in np.unique(key[vivid]):
            m = vivid & (key == k)
            ys, xs = np.where(m)
            if xs.size < _MIN_AREA:
                continue
            box = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
            dets.append(Detection(label="obstacle", score=0.85, box=box, source="simblob"))
        return dets
