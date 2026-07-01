"""Perception configuration — all env-overridable (mirrors robot/config.py style)."""

from __future__ import annotations

import os

# --- HTTP server (distinct port from the robot command server on :8000) ------
HOST: str = os.environ.get("PERCEPTION_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PERCEPTION_PORT", "8100"))

# --- Force simulate (synthetic frames + dummy detector) even on hardware ------
SIMULATE: bool = os.environ.get("PERCEPTION_SIMULATE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# --- CSI camera (nvarguscamerasrc via GStreamer) ------------------------------
CAMERA_SENSOR_ID: int = int(os.environ.get("PERCEPTION_CAMERA_SENSOR_ID", "0"))
CAMERA_WIDTH: int = int(os.environ.get("PERCEPTION_CAMERA_WIDTH", "1280"))
CAMERA_HEIGHT: int = int(os.environ.get("PERCEPTION_CAMERA_HEIGHT", "720"))
CAMERA_FPS: int = int(os.environ.get("PERCEPTION_CAMERA_FPS", "30"))
# nvvidconv flip-method: 0=none, 2=180°, etc. Set 2 if the camera is mounted upside down.
CAMERA_FLIP: int = int(os.environ.get("PERCEPTION_CAMERA_FLIP", "0"))


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


# --- Detector backends --------------------------------------------------------
# Comma list, auto-loaded at startup. YOLO (fixed COCO classes) is a safe default;
# add "nanoowl" for open-vocabulary. Both can be loaded at once for a perception-
# only session, but not alongside the future LLM+Whisper+Piper — unload on demand.
DETECTOR_BACKENDS: list[str] = _split(os.environ.get("PERCEPTION_BACKENDS", "yolo"))

# YOLO
YOLO_WEIGHTS: str = os.environ.get("PERCEPTION_YOLO_WEIGHTS", "yolov8n.pt")
YOLO_CONF: float = float(os.environ.get("PERCEPTION_YOLO_CONF", "0.25"))

# NanoOWL (open-vocabulary). Build the engine with:
#   python3 -m nanoowl.build_image_encoder_engine data/owl_image_encoder_patch32.engine
NANOOWL_MODEL: str = os.environ.get("PERCEPTION_NANOOWL_MODEL", "google/owlvit-base-patch32")
NANOOWL_ENGINE: str = os.environ.get(
    "PERCEPTION_NANOOWL_ENGINE", "data/owl_image_encoder_patch32.engine"
)
NANOOWL_THRESHOLD: float = float(os.environ.get("PERCEPTION_NANOOWL_THRESHOLD", "0.1"))
# Initial open-vocab prompts; the future LLM steers these at runtime via /prompts.
NANOOWL_PROMPTS: list[str] = _split(os.environ.get("PERCEPTION_NANOOWL_PROMPTS", "a person"))

# --- Output -------------------------------------------------------------------
# Where test_perception.py writes annotated JPEGs (headless Jetson friendly).
OUTPUT_DIR: str = os.environ.get("PERCEPTION_OUTPUT_DIR", "perception_out")
