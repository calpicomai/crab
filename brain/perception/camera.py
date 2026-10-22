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
from dataclasses import dataclass
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
_MAX_BUFFER_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class _EncodedFrame:
    data: bytes
    captured_at: float | None
    provenance: str
    sequence: int | None
    generation: int


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
        self._decode_cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._synthetic_tick = 0
        self._thread: threading.Thread | None = None
        self._decoder_thread: threading.Thread | None = None
        self._pending: _EncodedFrame | None = None
        self._sequence = 0
        self._last_capture_key = None
        self._generation = 0
        self._response = None
        self._health = unavailable("no captured frame", source="pi.camera.mjpeg",
                                   provenance="simulated" if self.simulate else "unknown")
        self._stream_provenance = "unknown"

        target = self._run_synthetic if self.simulate else self._run_stream
        if self.simulate:
            logger.warning("MjpegCamera in SIMULATE mode — synthetic frames only")
            self._store(self._synthetic_frame(), captured_at=time.time(), provenance="simulated")
        else:
            logger.info("MjpegCamera reading %s", url)
            self._decoder_thread = threading.Thread(target=self._run_decoder, daemon=True)
            self._decoder_thread.start()
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    # ----------------------------------------------------------------- #
    # Background readers
    # ----------------------------------------------------------------- #
    def _run_stream(self) -> None:
        """Connect to the MJPEG stream and decode frames until stopped."""
        try:
            import httpx
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
                    with self._lock:
                        self._response = resp
                    buf = b""
                    for chunk in resp.iter_bytes():
                        if self._stop.is_set():
                            return
                        buf += chunk
                        if len(buf) > _MAX_BUFFER_BYTES:
                            raise ValueError("MJPEG frame exceeds 8 MiB limit")
                        buf = self._extract_frames(buf, enqueue=True)
                    raise ConnectionError("camera stream ended")
            except Exception as exc:  # noqa: BLE001 - reconnect/backoff on any error
                if self._stop.is_set():
                    return
                logger.warning("MJPEG stream unavailable (%s); retrying", exc)
                self._invalidate(f"camera transport failed: {exc}")
                if self._stop.wait(1.0):
                    return
            finally:
                with self._lock:
                    self._response = None

    def _extract_frames(self, buf: bytes, Image=None, *, enqueue: bool = False) -> bytes:
        """Select only the newest complete JPEG and preserve a partial next frame.

        The network reader enqueues into a single replaceable compressed slot,
        so JPEG decoding cannot make it accumulate a frame queue. Direct calls
        may decode synchronously (useful for parser/integration checks).
        """
        if len(buf) > _MAX_BUFFER_BYTES:
            raise ValueError("MJPEG frame exceeds 8 MiB limit")
        newest = None
        offset = 0
        while True:
            start = buf.find(_SOI, offset)
            if start < 0:
                remainder_at = max(offset, len(buf) - 4096)
                break
            end = buf.find(_EOI, start + 2)
            if end < 0:
                remainder_at = offset
                break  # retain capture headers until the JPEG completes
            newest = (offset, start, end + 2)
            offset = end + 2
        remainder = buf[remainder_at:]
        if newest is None:
            return remainder
        header_at, jpeg_at, jpeg_end = newest
        headers, jpeg = buf[header_at:jpeg_at], buf[jpeg_at:jpeg_end]
        try:
            stamp = re.findall(rb"X-Crab-Captured-At:\s*([^\r\n]+)", headers, re.I)
            captured_at = float(stamp[-1]) if stamp else None
            if captured_at is None:
                raise ValueError("capture timestamp missing")
            if not math.isfinite(captured_at):
                raise ValueError("invalid camera timestamp")
            if not 0 <= time.time() - captured_at <= 2:
                # Drain stale network backlog without spending CPU decoding it.
                return remainder
            provenance_header = re.findall(rb"X-Crab-Provenance:\s*([^\r\n]+)", headers, re.I)
            provenance = provenance_header[-1].decode().strip() if provenance_header else self._stream_provenance
            provenance = provenance if provenance in {"real", "simulated"} else "unknown"
            wire_sequence = re.findall(rb"X-Crab-Sequence:\s*([^\r\n]+)", headers, re.I)
            sequence = int(wire_sequence[-1]) if wire_sequence else None
            if sequence is not None and sequence < 0:
                raise ValueError("invalid camera sequence")
            key = (captured_at, sequence)
            with self._decode_cond:
                if self._stop.is_set() or key == self._last_capture_key:
                    return remainder
                self._last_capture_key = key
                packet = _EncodedFrame(jpeg, captured_at, provenance, sequence, self._generation)
                if enqueue:
                    self._pending = packet
                    self._decode_cond.notify()
            if not enqueue:
                if Image is None:
                    from PIL import Image
                self._decode(packet, Image)
        except Exception as exc:
            self._invalidate(f"invalid camera frame or capture metadata: {exc}")
        return remainder

    def _run_decoder(self) -> None:
        try:
            from PIL import Image
        except Exception as exc:
            self._invalidate(f"camera decoder unavailable: {exc}")
            return
        while not self._stop.is_set():
            with self._decode_cond:
                self._decode_cond.wait_for(lambda: self._pending is not None or self._stop.is_set())
                if self._stop.is_set():
                    return
                packet, self._pending = self._pending, None
            self._decode(packet, Image)

    def _decode(self, packet: _EncodedFrame, Image) -> None:
        try:
            import io

            if not 0 <= time.time() - packet.captured_at <= 2:
                return
            rgb = np.asarray(Image.open(io.BytesIO(packet.data)).convert("RGB"))
            self._store(rgb[:, :, ::-1], captured_at=packet.captured_at,
                        provenance=packet.provenance, generation=packet.generation)
        except Exception as exc:
            self._invalidate(f"invalid camera JPEG: {exc}", generation=packet.generation)

    def _run_synthetic(self) -> None:
        period = 1.0 / 15
        while not self._stop.is_set():
            self._store(self._synthetic_frame(), captured_at=time.time(), provenance="simulated")
            self._stop.wait(period)

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
               provenance: str = "unknown", generation: int | None = None) -> None:
        with self._lock:
            if self._stop.is_set() or (generation is not None and generation != self._generation):
                return
            self._latest = frame
            self._sequence += 1
            self._health = InputHealth(available=True, provenance=provenance,
                                       captured_at=captured_at, received_at=time.time(),
                                       source="pi.camera.mjpeg", max_age_s=2,
                                       failure_reason=None if captured_at is not None else "capture timestamp missing")

    def _invalidate(self, reason: str, *, generation: int | None = None) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._generation += 1
            self._pending = None
            self._last_capture_key = None
            self._latest = None
            self._health = unavailable(reason, source="pi.camera.mjpeg", provenance=self._stream_provenance)

    def health(self) -> InputHealth:
        with self._lock:
            return self._health.model_copy(deep=True)

    def read(self) -> np.ndarray | None:
        """Fresh captured frame (BGR); never synthesize a transport fallback."""
        return self.read_snapshot()[0]

    def read_snapshot(self) -> tuple[np.ndarray | None, InputHealth, int]:
        """One frame, its matching health and a local publication sequence."""
        with self._lock:
            frame = self._latest if self._health.fresh() else None
            return frame, self._health.model_copy(deep=True), self._sequence

    def close(self) -> None:
        self._stop.set()
        self._invalidate("camera reader stopped")
        with self._decode_cond:
            response = self._response
            self._decode_cond.notify_all()
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if self._decoder_thread is not None:
            self._decoder_thread.join(timeout=1)
