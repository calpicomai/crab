"""Camera source for perception — pulls the robot's MJPEG stream over the LAN.

The camera is on the **robot (Pi)**, which serves an MJPEG stream; this client
runs on the Jetson, keeps the latest decoded frame in a background thread, and
hands it to the PerceptionEngine on demand. Decouples capture rate (the Pi's
stream) from detection rate (however often the engine asks).

If httpx/Pillow are missing or the stream can't be reached, it falls back to
deterministic synthetic frames (like the robot side) so perception still runs
off-hardware. Frames are numpy BGR (H, W, 3), matching ultralytics.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

logger = logging.getLogger("perception.camera")

# JPEG start-of-image / end-of-image markers, used to carve frames out of the
# multipart MJPEG byte stream without parsing multipart headers.
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


class MjpegCamera:
    """Reads the robot's MJPEG stream in a background thread; exposes latest frame."""

    def __init__(
        self,
        url: str,
        width: int = 640,
        height: int = 480,
        simulate: bool = False,
        timeout: float = 5.0,
    ) -> None:
        self.url = url
        self.width = width
        self.height = height
        self.timeout = timeout
        self.simulate = bool(simulate)
        self._latest: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._synthetic_tick = 0
        self._thread: threading.Thread | None = None

        target = self._run_synthetic if self.simulate else self._run_stream
        if self.simulate:
            logger.warning("MjpegCamera in SIMULATE mode — synthetic frames only")
        else:
            logger.info("MjpegCamera reading %s", url)
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    # ----------------------------------------------------------------- #
    # Background readers
    # ----------------------------------------------------------------- #
    def _run_stream(self) -> None:
        """Connect to the MJPEG stream and decode frames until stopped."""
        try:
            import httpx
            from PIL import Image
        except Exception as exc:  # noqa: BLE001
            logger.warning("httpx/Pillow unavailable (%s); using synthetic frames", exc)
            self.simulate = True
            self._run_synthetic()
            return

        while not self._stop.is_set():
            try:
                with httpx.stream("GET", self.url, timeout=self.timeout) as resp:
                    resp.raise_for_status()
                    buf = b""
                    for chunk in resp.iter_bytes():
                        if self._stop.is_set():
                            return
                        buf += chunk
                        buf = self._extract_frames(buf, Image)
            except Exception as exc:  # noqa: BLE001 - reconnect/backoff on any error
                if not self.simulate:
                    logger.warning("MJPEG stream error (%s); retrying, synthetic meanwhile", exc)
                    self.simulate = True  # report synthetic until frames flow
                self._store(self._synthetic_frame())
                if self._stop.wait(1.0):
                    return

    def _extract_frames(self, buf: bytes, Image) -> bytes:
        """Pull complete JPEGs out of buf, decode the newest, return the remainder."""
        while True:
            start = buf.find(_SOI)
            if start < 0:
                return buf[-1:]  # keep a byte in case a marker straddles chunks
            end = buf.find(_EOI, start + 2)
            if end < 0:
                return buf[start:]  # incomplete frame; wait for more bytes
            jpeg = buf[start : end + 2]
            buf = buf[end + 2 :]
            try:
                import io

                rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
                self._store(rgb[:, :, ::-1])  # RGB -> BGR
                self.simulate = False
            except Exception:  # noqa: BLE001 - skip a corrupt frame
                continue

    def _run_synthetic(self) -> None:
        period = 1.0 / 15
        while not self._stop.is_set():
            self._store(self._synthetic_frame())
            time.sleep(period)

    def _synthetic_frame(self) -> np.ndarray:
        h, w = self.height, self.width
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:, :, 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
        x0 = (self._synthetic_tick * 16) % max(1, w - w // 4)
        frame[h // 4 : h * 3 // 4, x0 : x0 + w // 4, 2] = 220
        self._synthetic_tick += 1
        return frame

    # ----------------------------------------------------------------- #
    # Frame access
    # ----------------------------------------------------------------- #
    def _store(self, frame: np.ndarray) -> None:
        with self._lock:
            self._latest = frame

    def read(self) -> np.ndarray:
        """Latest frame (BGR). Returns a synthetic frame until the first real one."""
        with self._lock:
            if self._latest is not None:
                return self._latest
        return self._synthetic_frame()

    def close(self) -> None:
        self._stop.set()
