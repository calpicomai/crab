"""Camera capture — CSI camera via GStreamer, with an off-hardware fallback.

On the Jetson, frames come from a CSI camera through an ``nvarguscamerasrc``
GStreamer pipeline opened by OpenCV. If OpenCV / the camera are unavailable
(dev laptop, CI) or ``simulate`` is forced, ``read()`` returns a deterministic
synthetic frame so the whole perception pipeline runs off-hardware — mirroring
the GaitEngine simulate philosophy on the robot side.

Frames are numpy arrays in BGR order (H, W, 3), matching OpenCV/ultralytics.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger("perception.camera")


class CsiCamera:
    """CSI camera capture with a synthetic-frame fallback."""

    def __init__(
        self,
        sensor_id: int = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        flip: int = 0,
        simulate: bool = False,
    ) -> None:
        self.width = width
        self.height = height
        self.simulate = bool(simulate)
        self._cap = None
        self._synthetic_tick = 0

        if self.simulate:
            logger.warning("CsiCamera in SIMULATE mode — synthetic frames only")
            return

        # Try to open the real CSI pipeline; any failure -> simulate.
        try:
            import cv2  # lazy: not present on dev laptops / CI

            pipeline = self._gst_pipeline(sensor_id, width, height, fps, flip)
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if not cap.isOpened():
                raise RuntimeError("cv2.VideoCapture did not open the GStreamer pipeline")
            self._cap = cap
            logger.info("CSI camera opened (sensor %d, %dx%d@%d)", sensor_id, width, height, fps)
        except Exception as exc:  # noqa: BLE001 - any failure -> synthetic frames
            logger.warning("CSI camera unavailable (%s); falling back to synthetic frames", exc)
            self.simulate = True

    @staticmethod
    def _gst_pipeline(sensor_id: int, width: int, height: int, fps: int, flip: int) -> str:
        return (
            f"nvarguscamerasrc sensor-id={sensor_id} ! "
            f"video/x-raw(memory:NVMM),width={width},height={height},framerate={fps}/1 ! "
            f"nvvidconv flip-method={flip} ! "
            f"video/x-raw,format=BGRx ! videoconvert ! "
            f"video/x-raw,format=BGR ! appsink drop=true max-buffers=1"
        )

    def _synthetic_frame(self) -> np.ndarray:
        """Deterministic placeholder frame: a gradient with a moving block."""
        h, w = self.height, self.width
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        # Vertical gradient so the image isn't blank.
        frame[:, :, 1] = np.linspace(0, 255, h, dtype=np.uint8)[:, None]
        # A block that shifts each call, so successive frames differ.
        x0 = (self._synthetic_tick * 20) % max(1, w - w // 4)
        frame[h // 4 : h * 3 // 4, x0 : x0 + w // 4, 2] = 200
        self._synthetic_tick += 1
        return frame

    def read(self) -> np.ndarray:
        """Return one BGR frame (H, W, 3). Never blocks indefinitely on hardware."""
        if self.simulate or self._cap is None:
            return self._synthetic_frame()
        ok, frame = self._cap.read()
        if not ok or frame is None:
            logger.warning("camera read failed; returning a synthetic frame this cycle")
            return self._synthetic_frame()
        return frame

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
