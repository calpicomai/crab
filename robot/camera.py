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
from dataclasses import dataclass
import threading
import time

from shared.health import InputHealth
from .camera_encoder import hardware_mjpeg

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

    def writable(self) -> bool:
        return True

    def write(self, buf) -> int:
        data = bytes(buf)
        with self._cond:
            if self._stopped:
                return len(data)
            self._sequence += 1
            self._latest = CameraFrame(data, time.time(), self._sequence, self._provenance)
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
            self._stopped = False

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()


class PiCamera:
    """CSI camera capture that exposes the latest JPEG frame + an MJPEG generator."""

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fps: int = 15,
        quality: int = 80,
        simulate: bool = False,
        encoder: str = "jpeg",
        mjpeg_bitrate: int | str = 4_000_000,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = max(1, fps)
        self.quality = quality
        self.simulate = bool(simulate)
        self.encoder_mode = encoder
        self.mjpeg_bitrate = mjpeg_bitrate
        self._failure: str | None = "not started"
        self._buffer = _FrameBuffer("simulated" if self.simulate else "real")
        self.capture_settings = {"requested_fps": self.fps, "rate_control": "not started",
                                 "achieved_fps_verified": False, "encoder_requested": encoder,
                                 "encoder": "not started", "quality_applied": False,
                                 "hardware_benchmark_verified": False}
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
        self.capture_settings.update(encoder="not started", quality_applied=False)
        if self.encoder_mode not in {"jpeg", "mjpeg_hw"}:
            self._failure = "camera encoder must be jpeg or mjpeg_hw"
            return
        if self.simulate:
            self._failure = None
            self.capture_settings["rate_control"] = "simulated timer"
            self.capture_settings["encoder"] = "Pillow synthetic; hardware profile not applied"
            self.capture_settings["quality_applied"] = True
            logger.warning("PiCamera in SIMULATE mode — serving synthetic frames")
            self._thread = threading.Thread(target=self._run_synthetic, daemon=True)
            self._thread.start()
            return
        if not _PICAMERA2_AVAILABLE:
            self._failure = "picamera2 unavailable"
            return
        try:  # pragma: no cover - requires hardware
            main = {"size": (self.width, self.height)}
            quality_applied = False
            if self.encoder_mode == "mjpeg_hw":
                if (type(self.width) is not int or type(self.height) is not int or
                    not 2 <= self.width <= 1920 or not 2 <= self.height <= 1080 or
                    self.width % 2 or self.height % 2 or not 1 <= self.fps <= 30):
                    raise ValueError("hardware MJPEG profile requires even dimensions up to 1920x1080 and 1..30 FPS")
                encoder, bitrate = hardware_mjpeg(self.mjpeg_bitrate)
                main["format"] = "YUV420"
                encoder_label = "Picamera2 V4L2 MJPEG (hardware requested; benchmark unverified)"
                self.capture_settings["requested_bitrate_bps"] = bitrate
            self._picam2 = Picamera2()
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
                main=main, controls=controls, queue=False)
            self._picam2.configure(cfg)
            if self.encoder_mode == "jpeg":
                # Preserve default initialization/error order and quality API.
                try:
                    encoder = JpegEncoder(q=self.quality)
                    quality_applied = True
                except TypeError:
                    encoder = JpegEncoder()
                encoder_label = "Picamera2 JpegEncoder (software JPEG)"
            self._picam2.start_recording(encoder, FileOutput(self._buffer))
            self.capture_settings.update(encoder=encoder_label, quality_applied=quality_applied)
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
