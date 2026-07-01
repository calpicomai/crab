"""Configuration for the LLM agent brain (brain/agent/).

Backend-agnostic by design: the agent talks the OpenAI-compatible chat API, so it
runs against a local ``llama-server`` (default) or Ollama or any compatible server
by changing ``LLM_BASE_URL`` — no code change, no cloud. Everything is
env-overridable, matching the rest of brain/config.py.
"""

from __future__ import annotations

import os

from shared import CAMERA_FRAME_PATH

from .. import config as brain_config

# --- LLM backend (OpenAI-compatible; default a local llama-server) -------------
# Point this at any OpenAI-compatible server. llama.cpp's llama-server is the
# default; set to an Ollama endpoint (http://localhost:11434/v1) or elsewhere to
# swap backends with no code change. It is still fully local — no cloud.
LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "http://localhost:8080/v1")
# A dummy key: local servers ignore it, but the OpenAI SDK requires one to be set.
LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "sk-local")
# Model name as the server advertises it. Default targets a small multimodal VLM.
LLM_MODEL: str = os.environ.get("LLM_MODEL", "qwen2.5-vl-3b-instruct")
# Multimodal: send the camera frame as an image. Set 0 for a text-only model (the
# agent then sends a text scene summary instead — the swappable text-LLM path).
LLM_MULTIMODAL: bool = os.environ.get("LLM_MULTIMODAL", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
AGENT_TEMPERATURE: float = float(os.environ.get("AGENT_TEMPERATURE", "0.3"))
AGENT_MAX_TOKENS: int = int(os.environ.get("AGENT_MAX_TOKENS", "300"))
# Per-request timeout (s). VLM inference on an Orin Nano is slow — be generous.
LLM_TIMEOUT_S: float = float(os.environ.get("LLM_TIMEOUT_S", "60"))

# --- Agent loop ----------------------------------------------------------------
# Seconds between decisions. The VLM is slow and the reactive layer owns real-time
# safety, so a deliberative tick every couple of seconds is fine.
AGENT_TICK_S: float = float(os.environ.get("AGENT_TICK_S", "2.0"))
# Reflex clearance handed to each walk the agent issues (the Pi aborts a stride
# below it) — same safety margin the wander loop uses.
AGENT_REFLEX_CM: float = float(os.environ.get("AGENT_REFLEX_CM", str(brain_config.WANDER_REFLEX_CM)))
# Free the detectors' RAM for the VLM on startup (the VLM does the seeing now):
# POST /unload yolo,nanoowl to the perception server. Disable to keep them loaded.
AGENT_FREE_PERCEPTION_RAM: bool = os.environ.get("AGENT_FREE_PERCEPTION_RAM", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
# Use the built-in canned policy instead of a real LLM, so the whole loop runs
# off-GPU/off-hardware (CI, dev laptop). Also toggled by the loop's --sim flag.
AGENT_SIMULATE: bool = os.environ.get("AGENT_SIMULATE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Where to pull the current camera frame (a single JPEG) — on the robot (Pi).
CAMERA_FRAME_URL: str = brain_config.BASE_URL.rstrip("/") + CAMERA_FRAME_PATH

# System prompt: a free-roaming explorer that narrates. It sees ONE camera frame
# per tick and picks ONE high-level action; a fast reflex on the robot handles
# real-time collision safety, so the agent reasons at the "where do I want to go"
# level, not servo timing.
SYSTEM_PROMPT: str = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    (
        "You are the brain of a small four-legged robot (a quadruped 'crawler'). "
        "You see the world through one forward camera image, given to you each turn "
        "along with a short status line (pose, forward clearance in cm). "
        "You decide ONE high-level action per turn by calling exactly one tool: "
        "walk (forward), turn (degrees; negative=left, positive=right), stand, sit, "
        "or get_status. A separate fast reflex on the robot prevents collisions, so "
        "you only choose intent — do not micromanage. Explore curiously and keep "
        "moving; if a goal is given, pursue it; if the way ahead looks blocked or "
        "clearance is low, turn toward open space. Always briefly narrate what you "
        "see and why you chose the action, in one short friendly sentence."
    ),
)
