"""PerceptionEngine — camera + a set of loadable detector backends, fused.

Owns the camera and a registry of loaded backends. detect() grabs one frame,
runs every loaded backend, and concatenates their detections (each tagged with
its source) into one PerceptionSnapshot. Backends load/unload independently so
the Jetson can reclaim a detector's RAM for the LLM/Whisper/Piper later.

Simulate: if PERCEPTION_SIMULATE is set, or a configured real backend fails to
import, or the camera can't open, the engine runs on synthetic frames and/or a
DummyBackend and reports simulate=True (mirrors GaitEngine's fallback).
"""

from __future__ import annotations

import logging
import time

import numpy as np

from . import backends, config
from .camera import CsiCamera
from .types import PerceptionSnapshot

logger = logging.getLogger("perception.engine")


class PerceptionEngine:
    def __init__(self, simulate: bool | None = None, backend_names: list[str] | None = None) -> None:
        self.simulate: bool = config.SIMULATE if simulate is None else bool(simulate)
        self._backends: dict[str, backends.DetectorBackend] = {}
        self._frame_id = 0
        self.last_frame: np.ndarray | None = None  # most recent frame (for annotation)

        self.camera = CsiCamera(
            sensor_id=config.CAMERA_SENSOR_ID,
            width=config.CAMERA_WIDTH,
            height=config.CAMERA_HEIGHT,
            fps=config.CAMERA_FPS,
            flip=config.CAMERA_FLIP,
            simulate=self.simulate,
        )
        # If the camera fell back to synthetic frames, we're effectively simulating.
        self.simulate = self.simulate or self.camera.simulate

        wanted = backend_names if backend_names is not None else config.DETECTOR_BACKENDS
        if self.simulate:
            # Forced/So-fallen simulate: run the dummy detector only.
            self._load_dummy()
        else:
            for name in wanted:
                self.load(name)
            if not self._backends:
                logger.warning("no detector backends loaded; falling back to dummy")
                self._load_dummy()

    # ----------------------------------------------------------------- #
    # Backend management
    # ----------------------------------------------------------------- #
    def _load_dummy(self) -> None:
        self.simulate = True
        dummy = backends.build("dummy")
        dummy.load()
        self._backends = {dummy.name: dummy}

    def load(self, name: str) -> None:
        """Load a backend by name. On failure, log and (if nothing else is loaded) fall back to dummy."""
        name = name.strip().lower()
        if name in self._backends:
            return
        try:
            backend = backends.build(name)
            backend.load()
            self._backends[name] = backend
            logger.info("loaded backend %r", name)
        except Exception as exc:  # noqa: BLE001 - missing libs / engine / weights
            logger.warning("could not load backend %r (%s)", name, exc)
            if not self._backends:
                self._load_dummy()

    def unload(self, name: str) -> None:
        name = name.strip().lower()
        backend = self._backends.pop(name, None)
        if backend is not None:
            backend.unload()
            logger.info("unloaded backend %r", name)

    def loaded_backends(self) -> list[str]:
        return list(self._backends.keys())

    def set_prompts(self, prompts: list[str]) -> None:
        """Set open-vocabulary prompts on every loaded backend that supports them."""
        for backend in self._backends.values():
            backend.set_prompts(prompts)

    @property
    def prompts(self) -> list[str] | None:
        nanoowl = self._backends.get("nanoowl")
        return list(nanoowl.prompts) if nanoowl is not None else None

    # ----------------------------------------------------------------- #
    # Perception
    # ----------------------------------------------------------------- #
    def detect(self) -> PerceptionSnapshot:
        frame = self.camera.read()
        self.last_frame = frame
        height, width = frame.shape[:2]

        t0 = time.monotonic()
        detections = []
        for backend in self._backends.values():
            detections.extend(backend.detect(frame))
        # TODO: optional cross-backend NMS to merge YOLO/NanoOWL duplicates.
        latency_ms = round((time.monotonic() - t0) * 1000, 1)

        self._frame_id += 1
        return PerceptionSnapshot(
            frame_id=self._frame_id,
            width=width,
            height=height,
            detections=detections,
            backends=self.loaded_backends(),
            simulate=self.simulate,
            latency_ms=latency_ms,
            prompts=self.prompts,
        )

    def close(self) -> None:
        for name in list(self._backends):
            self.unload(name)
        self.camera.close()
