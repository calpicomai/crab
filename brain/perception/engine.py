"""PerceptionEngine — camera + a set of loadable detector backends, fused.

Owns the camera and a registry of loaded backends. detect() grabs one frame,
runs every loaded backend, and concatenates their detections (each tagged with
its source) into one PerceptionSnapshot. Backends load/unload independently so
the Jetson can reclaim a detector's RAM for the LLM/Whisper/Piper later.

Simulation requires explicit configuration. A failed detector/camera remains
unavailable; an empty detection list never asserts free space.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from shared.health import InputHealth, unavailable

from . import backends, config
from .camera import MjpegCamera
from .types import PerceptionSnapshot

logger = logging.getLogger("perception.engine")


class PerceptionEngine:
    def __init__(self, simulate: bool | None = None, backend_names: list[str] | None = None) -> None:
        self.simulate: bool = config.SIMULATE if simulate is None else bool(simulate)
        self._backends: dict[str, backends.DetectorBackend] = {}
        self._frame_id = 0
        self.last_frame: np.ndarray | None = None  # most recent frame (for annotation)
        self._backend_failures: dict[str, str] = {}

        self.camera = MjpegCamera(
            url=config.CAMERA_URL,
            width=config.CAMERA_WIDTH,
            height=config.CAMERA_HEIGHT,
            simulate=self.simulate,
        )
        # The MJPEG reader connects in the background; simulate is re-evaluated per
        # snapshot from camera.simulate (it flips to False once real frames arrive).

        wanted = backend_names if backend_names is not None else config.DETECTOR_BACKENDS
        if self.simulate:
            # Forced/So-fallen simulate: run the dummy detector only.
            self._load_dummy()
        else:
            for name in wanted:
                self.load(name)

    # ----------------------------------------------------------------- #
    # Backend management
    # ----------------------------------------------------------------- #
    def _load_dummy(self) -> None:
        # Load the dummy detector. Does NOT set self.simulate — a dummy detector
        # (no real model) is orthogonal to synthetic frames. "simulate" tracks the
        # camera/forced state; the dummy shows up in the backends list instead.
        dummy = backends.build("dummy")
        dummy.load()
        self._backends = {dummy.name: dummy}

    def load(self, name: str) -> None:
        """Load a backend by name; failure is visible without synthetic replacement."""
        name = name.strip().lower()
        if name in self._backends:
            return
        try:
            backend = backends.build(name)
            backend.load()
            self._backends[name] = backend
            self._backend_failures.pop(name, None)
            logger.info("loaded backend %r", name)
        except Exception as exc:  # noqa: BLE001 - missing libs / engine / weights
            logger.warning("could not load backend %r (%s)", name, exc)
            self._backend_failures[name] = str(exc)[:300]

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
        frame, camera_health, _capture_sequence = self.camera.read_snapshot()
        self.last_frame = frame
        height, width = frame.shape[:2] if frame is not None else (self.camera.height, self.camera.width)

        t0 = time.monotonic()
        detections = []
        successful = []
        failures = dict(self._backend_failures)
        if frame is not None:
            for backend in self._backends.values():
                try:
                    detections.extend(backend.detect(frame))
                    successful.append(backend.name)
                except Exception as exc:
                    failures[backend.name] = str(exc)[:300]
        # TODO: optional cross-backend NMS to merge YOLO/NanoOWL duplicates.
        latency_ms = round((time.monotonic() - t0) * 1000, 1)

        self._frame_id += 1
        if frame is None:
            interpretation = unavailable("no fresh captured image", source="perception")
        elif not successful:
            interpretation = unavailable("no detector output: " + str(failures)[:240], source="perception")
        else:
            simulated = camera_health.provenance == "simulated" or all(name in {"dummy", "simblob"} for name in successful)
            interpretation = InputHealth(available=True, provenance="simulated" if simulated else camera_health.provenance,
                                          captured_at=camera_health.captured_at, received_at=time.time(), max_age_s=2,
                                          source=",".join(successful))
        inputs = {"camera": camera_health, "interpretation": interpretation}
        for name, reason in failures.items():
            inputs[f"detector:{name}"] = unavailable(reason, source=name)
        return PerceptionSnapshot(
            frame_id=self._frame_id,
            width=width,
            height=height,
            detections=detections,
            backends=self.loaded_backends(),
            # Effective: forced simulate, or the camera is still on synthetic frames.
            simulate=self.simulate or camera_health.provenance == "simulated" or interpretation.provenance == "simulated",
            latency_ms=latency_ms,
            prompts=self.prompts,
            inputs=inputs,
        )

    def close(self) -> None:
        for name in list(self._backends):
            self.unload(name)
        self.camera.close()
