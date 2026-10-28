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
import json
import math
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import numpy as np

from shared.health import InputHealth, unavailable
from shared.clock import CaptureClock, capture_age, MAX_RTT_S
from shared.local import require_local_url

logger = logging.getLogger("perception.camera")

# JPEG start-of-image / end-of-image markers, used to carve frames out of the
# multipart MJPEG byte stream without parsing multipart headers.
_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"
_MAX_BUFFER_BYTES = 8 * 1024 * 1024
_MAX_CLOCK_BYTES = 4096
_MAX_CLOCK_CHUNKS = 4096
_CLOCK_INTERVAL_S = 1.0


@dataclass(frozen=True)
class _EncodedFrame:
    data: bytes
    captured_at: float | None
    provenance: str
    sequence: int | None
    generation: int
    clock: CaptureClock
    boot_id: str
    received_at: float


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
        self._clock = None
        self._clock_boot = None
        self._stream_boot = None
        self._ordering_boot = None
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
    def _accept_clock(self, probe, sent_wall, sent_mono, wall, mono):
        """Bind only correlated bounded measurements, never frame timestamps."""
        boot = probe.get("server_boot_id") if isinstance(probe, dict) else None
        with self._lock:
            if self._stop.is_set():
                raise ValueError("camera reader stopped")
            try:
                if not isinstance(boot, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", boot):
                    raise ValueError("camera clock probe boot identity missing or malformed")
                if self._stream_boot is not None and boot != self._stream_boot:
                    raise ValueError("camera stream and clock probe boot disagree")
                previous = self._clock if boot == self._clock_boot else None
                clock = CaptureClock.measure(probe.get("server_at"), sent_wall, sent_mono,
                                             wall, mono, previous)
                self._clock, self._clock_boot = clock, boot
            except Exception:
                self._invalidate_locked("camera clock calibration failed", reset_connection=True)
                raise

    def _bind_stream(self, headers):
        boot = headers.get("X-Crab-Boot-Id")
        with self._lock:
            if not boot or boot != self._clock_boot or self._clock is None:
                self._invalidate_locked("camera stream boot missing or incompatible", reset_connection=True)
                raise ValueError("camera stream boot missing or incompatible with clock probe; update Pi and brain together")
            self._stream_boot = boot
            if boot != self._ordering_boot:
                self._last_capture_key = None
                self._ordering_boot = boot
            provenance = headers.get("X-Crab-Provenance", "unknown")
            self._stream_provenance = provenance if provenance in {"real", "simulated"} else "unknown"

    def _probe_clock(self, client):
        """At most one small GET per second on the existing stream owner.

        The GET neither acquires control nor opens another camera consumer.
        The socket timeout is an inactivity bound; elapsed checks run between
        raw chunks and the final correlated RTT must satisfy shared bounds.
        DNS, header parsing and an in-progress read can exceed the elapsed
        budget. A late/failed response cannot establish usable capture timing.
        """
        origin = urlsplit(self.url)
        url = urlunsplit((origin.scheme, origin.netloc, "/sim/clock", "", ""))
        sent_wall, sent_mono = time.time(), time.monotonic()
        with client.stream("GET", url, timeout=MAX_RTT_S,
                           headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"}) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("compressed camera clock response rejected")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > _MAX_CLOCK_BYTES):
                raise ValueError("camera clock response exceeds bound")
            data = bytearray()
            # Iterate the raw stream itself: iter_raw(chunk_size=N) coalesces
            # small chunks before yielding, while even iter_raw() hides empty
            # chunks. Both can defer deadline/cancellation checks indefinitely.
            # The response context always closes the raw stream on exit.
            chunks = (response.content,) if response.is_stream_consumed else response.stream
            for count, chunk in enumerate(chunks, 1):
                if self._stop.is_set() or time.monotonic()-sent_mono > MAX_RTT_S:
                    raise ValueError("camera clock request deadline exceeded or reader stopped")
                if count > _MAX_CLOCK_CHUNKS:
                    raise ValueError("camera clock response chunk limit exceeded")
                if len(data)+len(chunk) > _MAX_CLOCK_BYTES:
                    raise ValueError("camera clock response exceeds bound")
                data.extend(chunk)
        probe = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite camera clock JSON")))
        self._accept_clock(probe, sent_wall, sent_mono, time.time(), time.monotonic())

    def _run_stream(self) -> None:
        """Connect to the MJPEG stream and decode frames until stopped."""
        try:
            import httpx
        except Exception as exc:  # noqa: BLE001
            self._invalidate(f"camera dependencies unavailable: {exc}")
            return

        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False,
                                  limits=httpx.Limits(max_connections=2, max_keepalive_connections=1)) as client:
                    self._probe_clock(client)
                    next_probe = time.monotonic()+_CLOCK_INTERVAL_S
                    with client.stream("GET", self.url) as resp:
                        resp.raise_for_status()
                        self._bind_stream(resp.headers)
                        with self._lock:
                            self._response = resp
                        buf = b""
                        for chunk in resp.iter_bytes():
                            if self._stop.is_set():
                                return
                            if time.monotonic() >= next_probe:
                                self._probe_clock(client)
                                next_probe = time.monotonic()+_CLOCK_INTERVAL_S
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
            provenance_header = re.findall(rb"X-Crab-Provenance:\s*([^\r\n]+)", headers, re.I)
            provenance = provenance_header[-1].decode().strip() if provenance_header else self._stream_provenance
            provenance = provenance if provenance in {"real", "simulated"} else "unknown"
            wire_sequence = re.findall(rb"X-Crab-Sequence:\s*([^\r\n]+)", headers, re.I)
            sequence = int(wire_sequence[-1]) if wire_sequence else None
            if sequence is None or not 0 < sequence < 2**63:
                raise ValueError("invalid camera sequence")
            key = (captured_at, sequence)
            with self._decode_cond:
                if self._stop.is_set():
                    return remainder
                clock, boot = self._clock, self._stream_boot
                if clock is None or boot is None or boot != self._clock_boot:
                    raise ValueError("camera clock calibration unavailable")
                received_at = time.time()
                age = capture_age(captured_at, clock, wall=received_at, mono=time.monotonic())
                if age is None:
                    raise ValueError("camera timestamp is future or clock is inconsistent")
                if age > 2:
                    return remainder  # discard stale backlog before decoding
                if self._last_capture_key is not None and (
                        captured_at <= self._last_capture_key[0] or sequence <= self._last_capture_key[1]):
                    return remainder  # duplicate/out-of-order captures never refresh health
                self._last_capture_key = key
                packet = _EncodedFrame(jpeg, captured_at, provenance, sequence, self._generation,
                                       clock, boot, received_at)
                if enqueue:
                    self._pending = packet
                    self._decode_cond.notify()
            if not enqueue:
                if Image is None:
                    from PIL import Image
                self._decode(packet, Image)
        except Exception as exc:
            self._invalidate(f"invalid camera frame or capture metadata: {exc}", reset_connection=False)
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

            with self._lock:
                if (self._stop.is_set() or packet.generation != self._generation
                        or packet.boot_id != self._stream_boot):
                    return
            age = capture_age(packet.captured_at, packet.clock)
            if age is None or age > 2:
                return
            rgb = np.asarray(Image.open(io.BytesIO(packet.data)).convert("RGB"))
            self._store(rgb[:, :, ::-1], captured_at=packet.captured_at,
                        provenance=packet.provenance, generation=packet.generation,
                        clock=packet.clock, boot_id=packet.boot_id, received_at=packet.received_at)
        except Exception as exc:
            self._invalidate(f"invalid camera JPEG: {exc}", generation=packet.generation, reset_connection=False)

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
               provenance: str = "unknown", generation: int | None = None,
               clock: CaptureClock | None = None, boot_id: str | None = None,
               received_at: float | None = None) -> None:
        with self._lock:
            if self._stop.is_set() or (generation is not None and generation != self._generation):
                return
            if not self.simulate:
                age = capture_age(captured_at, clock) if clock is not None else None
                if (boot_id is None or boot_id != self._stream_boot or boot_id != self._clock_boot
                        or age is None or age > 2):
                    return
            self._latest = frame
            self._sequence += 1
            self._health = InputHealth(available=True, provenance=provenance,
                                       captured_at=captured_at, clock=clock,
                                       received_at=time.time() if received_at is None else received_at,
                                       source="pi.camera.mjpeg", max_age_s=2,
                                       failure_reason=None if captured_at is not None else "capture timestamp missing")

    def _invalidate(self, reason: str, *, generation: int | None = None, reset_connection=True) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._invalidate_locked(reason, reset_connection=reset_connection)

    def _invalidate_locked(self, reason, *, reset_connection):
        self._generation += 1
        self._pending = None
        if reset_connection:
            self._clock, self._clock_boot, self._stream_boot = None, None, None
        # Retain the capture watermark across reconnects to the same boot. A
        # newly verified boot resets it in _bind_stream, never an old packet.
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
