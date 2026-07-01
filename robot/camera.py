"""Camera capture on the Pi — serves MJPEG frames over the LAN to the Jetson.

The camera is physically on the robot (Pi Camera, CSI ribbon). The Pi 4B can't
run the detectors, so it just captures and streams; the Jetson brain pulls the
stream and does detection. Capture uses picamera2 (the libcamera stack on
Raspberry Pi OS Bookworm).

Mirrors the GaitEngine simulate philosophy: if picamera2 isn't importable (dev
laptop, CI), the camera generates deterministic synthetic JPEG frames instead,
so the whole Pi->Jetson video link runs off-hardware.
"""

from __future__ import annotations

import io
import logging
import threading
import time

logger = logging.getLogger("picrawler.camera")

# Guarded hardware imports. Missing off-Pi -> synthetic frames.
try:  # pragma: no cover - depends on deploy target
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput

    _PICAMERA2_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - any failure means "no camera hardware"
    Picamera2 = None  # type: ignore[assignment,misc]
    _PICAMERA2_AVAILABLE = False
    logger.info("picamera2 unavailable (%s); camera will produce synthetic frames", exc)


class _FrameBuffer:
    """Holds the latest JPEG frame; file-like so picamera2's FileOutput can write it."""

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self._cond = threading.Condition()

    def write(self, buf: bytes) -> int:
        with self._cond:
            self.frame = bytes(buf)
            self._cond.notify_all()
        return len(buf)

    def flush(self) -> None:  # picamera2 FileOutput may call flush()
        return None

    def latest(self, timeout: float = 1.0) -> bytes | None:
        with self._cond:
            if self.frame is None:
                self._cond.wait(timeout)
            return self.frame


class PiCamera:
    """CSI camera capture that exposes the latest JPEG frame + an MJPEG generator."""

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fps: int = 15,
        quality: int = 80,
        simulate: bool = False,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = max(1, fps)
        self.quality = quality
        self.simulate = bool(simulate) or not _PICAMERA2_AVAILABLE
        self._buffer = _FrameBuffer()
        self._picam2 = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def start(self) -> None:
        if self.simulate:
            logger.warning("PiCamera in SIMULATE mode — serving synthetic frames")
            self._thread = threading.Thread(target=self._run_synthetic, daemon=True)
            self._thread.start()
            return
        try:  # pragma: no cover - requires hardware
            self._picam2 = Picamera2()
            cfg = self._picam2.create_video_configuration(main={"size": (self.width, self.height)})
            self._picam2.configure(cfg)
            self._picam2.start_recording(MJPEGEncoder(q=self.quality), FileOutput(self._buffer))
            logger.info("PiCamera started (%dx%d@%d)", self.width, self.height, self.fps)
        except Exception as exc:  # noqa: BLE001 - fall back to synthetic
            logger.warning("PiCamera hardware start failed (%s); using synthetic frames", exc)
            self.simulate = True
            self._thread = threading.Thread(target=self._run_synthetic, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._picam2 is not None:  # pragma: no cover - hardware
            try:
                self._picam2.stop_recording()
            except Exception:  # noqa: BLE001
                pass
            self._picam2 = None

    # ----------------------------------------------------------------- #
    # Synthetic generator (off-hardware)
    # ----------------------------------------------------------------- #
    def _run_synthetic(self) -> None:
        import numpy as np
        from PIL import Image

        tick = 0
        period = 1.0 / self.fps
        while not self._stop.is_set():
            arr = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            arr[:, :, 1] = np.linspace(0, 255, self.height, dtype=np.uint8)[:, None]
            x0 = (tick * 16) % max(1, self.width - self.width // 4)
            arr[self.height // 4 : self.height * 3 // 4, x0 : x0 + self.width // 4, 0] = 220
            buf = io.BytesIO()
            Image.fromarray(arr).save(buf, format="JPEG", quality=self.quality)
            self._buffer.write(buf.getvalue())
            tick += 1
            time.sleep(period)

    # ----------------------------------------------------------------- #
    # Frame access
    # ----------------------------------------------------------------- #
    def get_frame(self, timeout: float = 1.0) -> bytes | None:
        """Latest JPEG bytes (or None if no frame yet)."""
        return self._buffer.latest(timeout)

    def mjpeg_frames(self):
        """Yield multipart/x-mixed-replace chunks for a streaming HTTP response."""
        boundary = b"--frame\r\n"
        period = 1.0 / self.fps
        while not self._stop.is_set():
            frame = self.get_frame()
            if frame is None:
                time.sleep(0.05)
                continue
            yield boundary + b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(period)
