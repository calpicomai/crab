"""Camera capture on the Pi — serves MJPEG frames over the LAN to the Jetson.

The camera is physically on the robot (Pi Camera, CSI ribbon). The Pi 4B can't
run the detectors, so it just captures and streams; the Jetson brain pulls the
stream and does detection. Capture uses picamera2 (the libcamera stack on
Raspberry Pi OS Bookworm).

Synthetic frames require explicit simulation. Missing libraries, hardware
failures and stalled capture stay unavailable and never manufacture observations.
"""

from __future__ import annotations

import io
import logging
import math
from collections import deque
from dataclasses import dataclass
import threading
import time

from shared.health import InputHealth

logger = logging.getLogger("picrawler.camera")

# Guarded hardware imports. Missing off-Pi -> unavailable unless explicitly simulated.
try:  # pragma: no cover - depends on deploy target
    from picamera2 import Picamera2
    from picamera2.encoders import JpegEncoder
    from picamera2.outputs import FileOutput

    _PICAMERA2_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - any failure means "no camera hardware"
    Picamera2 = None  # type: ignore[assignment,misc]
    _PICAMERA2_AVAILABLE = False
    logger.info("picamera2 unavailable (%s); camera unavailable without explicit simulation", exc)


@dataclass(frozen=True)
class CameraFrame:
    """One immutable encoded frame and its matching capture metadata."""

    data: bytes
    captured_at: float
    sequence: int
    provenance: str


class _FrameBuffer(io.BufferedIOBase):
    """Holds the latest JPEG frame. Subclasses io.BufferedIOBase because
    picamera2's FileOutput requires the output to be one (this is the same
    pattern as picamera2's own MJPEG-streaming StreamingOutput example)."""

    def __init__(self, provenance: str = "unknown") -> None:
        super().__init__()
        self._latest: CameraFrame | None = None
        self._sequence = 0
        self._provenance = provenance
        self._stopped = False
        self._cond = threading.Condition()
        self._encoded_times = deque(maxlen=64)

    def writable(self) -> bool:
        return True

    def write(self, buf) -> int:
        data = bytes(buf)
        with self._cond:
            if self._stopped:
                return len(data)
            self._sequence += 1
            self._latest = CameraFrame(data, time.time(), self._sequence, self._provenance)
            self._encoded_times.append(time.monotonic())
            self._cond.notify_all()
        return len(data)

    def snapshot(self, timeout: float = 0.0, after_sequence: int | None = None) -> CameraFrame | None:
        with self._cond:
            ready = lambda: self._stopped or (self._latest is not None and (
                after_sequence is None or self._latest.sequence > after_sequence))
            self._cond.wait_for(ready, timeout=max(0.0, timeout))
            return self._latest if ready() and not self._stopped else None

    def reset(self) -> None:
        with self._cond:
            self._latest = None
            self._encoded_times.clear()
            self._stopped = False

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def stats(self):
        with self._cond:
            times = list(self._encoded_times)
            age = time.monotonic()-times[-1] if times else None
            fps = ((len(times)-1)/(times[-1]-times[0]) if len(times)>1 and times[-1]>times[0]
                   and age is not None and 0 <= age <= 2 else None)
            return {"encoded_fps": None if fps is None else round(fps, 1),
                    "jpeg_bytes": len(self._latest.data) if self._latest else None,
                    "sequence": self._latest.sequence if self._latest else None}


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
        self.simulate = bool(simulate)
        self._failure: str | None = "not started"
        self._buffer = _FrameBuffer("simulated" if self.simulate else "real")
        self.capture_settings = {"requested_fps": self.fps, "rate_control": "not started",
                                 "achieved_fps_verified": False, "requested_width": width,
                                 "requested_height": height, "requested_quality": quality,
                                 "encoder": "Pillow synthetic" if simulate else "Picamera2 JpegEncoder (software JPEG)",
                                 "timestamp_kind": "encoded-output arrival; excludes exposure/encode latency",
                                 "sensor_model": None}
        self._capture_lock = threading.Lock()
        self._sensor_times = deque(maxlen=64)
        self._last_sensor_mono = None
        self._picam2 = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Optional 2D sim world (robot/simworld.py). When set, synthetic frames
        # render the world's first-person view instead of the placeholder block.
        self.world = None

    # ----------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------- #
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._picam2 is not None:
            return
        self._stop.clear()
        self._buffer.reset()
        with self._capture_lock:
            self._sensor_times.clear()
            self._last_sensor_mono = None
        if self.simulate:
            self._failure = None
            self.capture_settings["rate_control"] = "simulated timer"
            logger.warning("PiCamera in SIMULATE mode — serving synthetic frames")
            self._thread = threading.Thread(target=self._run_synthetic, daemon=True)
            self._thread.start()
            return
        if not _PICAMERA2_AVAILABLE:
            self._failure = "picamera2 unavailable"
            return
        try:  # pragma: no cover - requires hardware
            self._picam2 = Picamera2()
            model = getattr(self._picam2, "camera_properties", {}).get("Model")
            self.capture_settings["sensor_model"] = model if isinstance(model, str) else None
            controls = {}
            if "FrameDurationLimits" in self._picam2.camera_controls:
                # Official Picamera2 controls.py maps FrameRate to these equal
                # microsecond bounds. Only request advertised libcamera controls.
                period_us = round(1_000_000 / self.fps)
                controls["FrameDurationLimits"] = (period_us, period_us)
                self.capture_settings["rate_control"] = "FrameDurationLimits requested; achieved rate unverified"
            else:
                self.capture_settings["rate_control"] = "camera does not advertise FrameDurationLimits"
                logger.warning("%s", self.capture_settings["rate_control"])
            cfg = self._picam2.create_video_configuration(
                main={"size": (self.width, self.height)}, controls=controls, queue=False)
            self._picam2.configure(cfg)
            if hasattr(self._picam2, "post_callback") and self._picam2.post_callback is None:
                self._picam2.post_callback = self._capture_metadata
            # JpegEncoder is picamera2's MJPEG-streaming encoder. `q` (JPEG quality)
            # is accepted on current versions; fall back if an older one differs.
            try:
                encoder = JpegEncoder(q=self.quality)
                self.capture_settings["quality_applied"] = True
            except TypeError:
                encoder = JpegEncoder()
                self.capture_settings["quality_applied"] = False
            self._picam2.start_recording(encoder, FileOutput(self._buffer))
            self._failure = None
            logger.info("PiCamera started (%dx%d@%d)", self.width, self.height, self.fps)
        except Exception as exc:  # noqa: BLE001
            self._failure = f"camera start failed: {exc}"[:300]
            logger.warning("%s", self._failure)
            if self._picam2 is not None:
                try:
                    self._picam2.close()
                except Exception:
                    pass
                self._picam2 = None

    def stop(self) -> None:
        self._stop.set()
        self._buffer.stop()
        self._failure = "camera stopped"
        if self._picam2 is not None:  # pragma: no cover - hardware
            try:
                self._picam2.stop_recording()
            except Exception:  # noqa: BLE001
                pass
            self._picam2 = None
        if self._thread is not None:
            self._thread.join(timeout=1)

    # ----------------------------------------------------------------- #
    # Synthetic generator (off-hardware)
    # ----------------------------------------------------------------- #
    def _run_synthetic(self) -> None:
        # Pure Pillow (no numpy) so the Pi node stays minimal — the real capture
        # path uses picamera2's own encoder and never touches this.
        from PIL import Image, ImageDraw

        tick = 0
        period = 1.0 / self.fps
        block_w = max(1, self.width // 4)
        while not self._stop.is_set():
            if self.world is not None:
                # First-person view of the sim world (obstacles as colored boxes).
                img = self.world.render_camera(self.width, self.height)
            else:
                img = Image.new("RGB", (self.width, self.height), (0, 80, 0))
                x0 = (tick * 16) % max(1, self.width - block_w)
                ImageDraw.Draw(img).rectangle(
                    [x0, self.height // 4, x0 + block_w, self.height * 3 // 4], fill=(220, 0, 0)
                )
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self.quality)
            self._buffer.write(buf.getvalue())
            tick += 1
            self._stop.wait(period)

    # ----------------------------------------------------------------- #
    # Frame access
    # ----------------------------------------------------------------- #
    def get_frame(self, timeout: float = 1.0) -> bytes | None:
        """Latest fresh JPEG bytes, or None on missing/stale/failed capture."""
        frame = self.get_snapshot(timeout)
        return frame.data if frame is not None else None

    def get_snapshot(self, timeout: float = 0.0, after_sequence: int | None = None) -> CameraFrame | None:
        """Return matching bytes/timestamp/sequence atomically, without a frame queue."""
        frame = self._buffer.snapshot(timeout, after_sequence)
        return frame if frame is not None and self._frame_health(frame).fresh() else None

    def health(self) -> InputHealth:
        return self._frame_health(self._buffer.snapshot())

    def _capture_metadata(self, request):
        """Observe the existing request callback; never request another frame."""
        try:
            stamp = request.get_metadata().get("SensorTimestamp")
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
                return
            with self._capture_lock:
                if self._sensor_times and stamp <= self._sensor_times[-1]:
                    self._sensor_times.clear()
                self._sensor_times.append(stamp)
                self._last_sensor_mono = time.monotonic()
        except Exception:
            # Optional instrumentation must not interrupt capture or invent FPS.
            return

    def stats(self):
        with self._capture_lock:
            times = list(self._sensor_times)
            recent = self._last_sensor_mono is not None and 0 <= time.monotonic()-self._last_sensor_mono <= 2
        fps = (1e9*(len(times)-1)/(times[-1]-times[0]) if recent and len(times)>1 and times[-1]>times[0] else None)
        health = self.health()
        return {**self.capture_settings, **self._buffer.stats(), "available": health.fresh(),
                "reason": health.failure_reason, "capture_fps": None if fps is None else round(fps, 1),
                "capture_fps_source": "SensorTimestamp cadence" if fps is not None else "unmeasured",
                "provenance": health.provenance}

    def _frame_health(self, frame: CameraFrame | None) -> InputHealth:
        stamp = frame.captured_at if frame is not None else None
        return InputHealth(available=stamp is not None and self._failure is None,
                           provenance="simulated" if self.simulate else "real",
                           captured_at=stamp, received_at=stamp, source="pi.camera",
                           max_age_s=2, failure_reason=self._failure)

    def mjpeg_frames(self):
        """Send each selected capture once; slow consumers skip intermediate frames.

        This bounds the application queue, not bytes already buffered by HTTP,
        the OS or a browser. Dashboard clients should use serial JPEG pulls.
        """
        boundary = b"--frame\r\n"
        period = 1.0 / self.fps
        sequence = 0
        next_send = 0.0
        while not self._stop.is_set():
            if self._stop.wait(max(0.0, next_send - time.monotonic())):
                return
            frame = self._buffer.snapshot(timeout=1.0, after_sequence=sequence)
            if frame is None:
                continue
            sequence = frame.sequence
            if not self._frame_health(frame).fresh() or self._stop.is_set():
                continue
            metadata = (f"Content-Length: {len(frame.data)}\r\n"
                        f"X-Crab-Captured-At: {frame.captured_at}\r\n"
                        f"X-Crab-Provenance: {frame.provenance}\r\n"
                        f"X-Crab-Sequence: {frame.sequence}\r\n").encode()
            next_send = time.monotonic() + period
            yield boundary + b"Content-Type: image/jpeg\r\n" + metadata + b"\r\n" + frame.data + b"\r\n"
