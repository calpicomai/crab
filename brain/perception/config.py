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

# --- Camera source (the camera is on the robot/Pi, streamed as MJPEG) ---------
# Default to the robot's MJPEG endpoint, derived from brain/config.py so the Pi's
# address isn't hardcoded twice. Override with PERCEPTION_CAMERA_URL.
from .. import config as _robot_link  # brain/config.py: ROBOT_HOST/PORT/BASE_URL
from shared import CAMERA_STREAM_PATH

CAMERA_URL: str = os.environ.get(
    "PERCEPTION_CAMERA_URL", f"{_robot_link.BASE_URL}{CAMERA_STREAM_PATH}"
)
# Size of the synthetic fallback frame (real frame size comes from the stream).
CAMERA_WIDTH: int = int(os.environ.get("PERCEPTION_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT: int = int(os.environ.get("PERCEPTION_CAMERA_HEIGHT", "480"))


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


# --- Detector backends --------------------------------------------------------
# Comma list, auto-loaded at startup. YOLO (fixed COCO classes) is a safe default;
# add "nanoowl" for open-vocabulary. Both can be loaded at once for a perception-
# only session, but not alongside the future LLM+Whisper+Piper — unload on demand.
DETECTOR_BACKENDS: list[str] = _split(os.environ.get("PERCEPTION_BACKENDS", "yolo"))

# YOLO. Default to the copy setup_perception.sh pre-fetches into the repo's data/
# dir if it's there (so no env is needed); else the bare name, which ultralytics
# downloads on first use.
def _default_yolo_weights() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cached = os.path.join(repo_root, "data", "yolov8n.pt")
    return cached if os.path.exists(cached) else "yolov8n.pt"


YOLO_WEIGHTS: str = os.environ.get("PERCEPTION_YOLO_WEIGHTS", _default_yolo_weights())
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
