"""Camera source for perception — pulls the robot's MJPEG stream over the LAN.

The camera is on the **robot (Pi)**, which serves an MJPEG stream; this client
runs on the Jetson, keeps the latest decoded frame in a background thread, and
hands it to the PerceptionEngine on demand. Decouples capture rate (the Pi's
stream) from detection rate (however often the engine asks).

Missing libraries or failed/stale transport produces no frame. Synthetic frames
require explicit simulation. Frames are numpy BGR (H, W, 3).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time

import numpy as np

from shared.health import InputHealth, unavailable
from shared.local import require_local_url

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
        self.url = require_local_url(url)
        self.width = width
        self.height = height
        self.timeout = timeout
        self.simulate = bool(simulate)
        self._latest: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._synthetic_tick = 0
        self._thread: threading.Thread | None = None
        self._health = unavailable("no captured frame", source="pi.camera.mjpeg",
                                   provenance="simulated" if self.simulate else "unknown")
        self._stream_provenance = "unknown"

        target = self._run_synthetic if self.simulate else self._run_stream
        if self.simulate:
            logger.warning("MjpegCamera in SIMULATE mode — synthetic frames only")
            self._store(self._synthetic_frame(), captured_at=time.time(), provenance="simulated")
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
            self._invalidate(f"camera dependencies unavailable: {exc}")
            return

        while not self._stop.is_set():
            try:
                with httpx.stream("GET", self.url, timeout=self.timeout,
                                  trust_env=False, follow_redirects=False) as resp:
                    resp.raise_for_status()
                    provenance = resp.headers.get("X-Crab-Provenance", "unknown")
                    self._stream_provenance = provenance if provenance in {"real", "simulated"} else "unknown"
                    buf = b""
                    for chunk in resp.iter_bytes():
                        if self._stop.is_set():
                            return
                        buf += chunk
                        if len(buf) > 8 * 1024 * 1024:
                            raise ValueError("MJPEG frame exceeds 8 MiB limit")
                        buf = self._extract_frames(buf, Image)
                    raise ConnectionError("camera stream ended")
            except Exception as exc:  # noqa: BLE001 - reconnect/backoff on any error
                logger.warning("MJPEG stream unavailable (%s); retrying", exc)
                self._invalidate(f"camera transport failed: {exc}")
                if self._stop.wait(1.0):
                    return

    def _extract_frames(self, buf: bytes, Image) -> bytes:
        """Pull complete JPEGs out of buf, decode the newest, return the remainder."""
        while True:
            start = buf.find(_SOI)
            if start < 0:
                return buf[-4096:]  # preserve multipart metadata across chunks
            end = buf.find(_EOI, start + 2)
            if end < 0:
                return buf  # retain capture headers until the JPEG completes
            headers = buf[:start]
            jpeg = buf[start : end + 2]
            buf = buf[end + 2 :]
            try:
                import io

                rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"))
                stamp = re.findall(rb"X-Crab-Captured-At:\s*([^\r\n]+)", headers, re.I)
                captured_at = float(stamp[-1]) if stamp else None
                if captured_at is not None and not math.isfinite(captured_at):
                    raise ValueError("invalid camera timestamp")
                provenance_header = re.findall(rb"X-Crab-Provenance:\s*([^\r\n]+)", headers, re.I)
                provenance = provenance_header[-1].decode().strip() if provenance_header else self._stream_provenance
                provenance = provenance if provenance in {"real", "simulated"} else "unknown"
                self._store(rgb[:, :, ::-1], captured_at=captured_at, provenance=provenance)
            except Exception:  # noqa: BLE001 - skip a corrupt frame
                self._invalidate("invalid camera frame or capture metadata")
                continue

    def _run_synthetic(self) -> None:
        period = 1.0 / 15
        while not self._stop.is_set():
            self._store(self._synthetic_frame(), captured_at=time.time(), provenance="simulated")
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
    def _store(self, frame: np.ndarray, *, captured_at: float | None = None,
               provenance: str = "unknown") -> None:
        with self._lock:
            self._latest = frame
            self._health = InputHealth(available=True, provenance=provenance,
                                       captured_at=captured_at, received_at=time.time(),
                                       source="pi.camera.mjpeg", max_age_s=2,
                                       failure_reason=None if captured_at is not None else "capture timestamp missing")

    def _invalidate(self, reason: str) -> None:
        with self._lock:
            self._latest = None
            self._health = unavailable(reason, source="pi.camera.mjpeg", provenance=self._stream_provenance)

    def health(self) -> InputHealth:
        with self._lock:
            return self._health.model_copy(deep=True)

    def read(self) -> np.ndarray | None:
        """Fresh captured frame (BGR); never synthesize a transport fallback."""
        with self._lock:
            if self._latest is not None and self._health.fresh():
                return self._latest
        return None

    def close(self) -> None:
        self._stop.set()
        self._invalidate("camera reader stopped")
