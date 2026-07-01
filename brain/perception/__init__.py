"""Perception subsystem — runs on the Jetson brain.

Captures frames from a CSI camera and runs object detection (YOLO and/or
NanoOWL) behind a single PerceptionEngine that fuses their detections. Exposed
both in-process (for the future agent loop) and over HTTP (perception/server.py).

Detector backends are loadable/unloadable so the 8GB Jetson can free a detector's
RAM when the LLM / Whisper / Piper need it. If the heavy libs or the camera are
unavailable (dev laptop, CI), the engine drops into a simulate mode — synthetic
frames + a dummy detector — so the whole thing runs off-hardware.
"""

from .types import Detection, PerceptionSnapshot

__all__ = ["Detection", "PerceptionSnapshot"]
