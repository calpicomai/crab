"""Configuration for the Jetson brain — chiefly how to reach the Pi.

The Pi's address is NOT hardcoded anywhere else. Override via environment
variables, or edit the defaults below. The default uses the Raspberry Pi's mDNS
hostname, which works without knowing the IP if avahi/Bonjour is available on
the LAN. If mDNS is not available, set ROBOT_HOST to the Pi's IP address.
"""

from __future__ import annotations

import os

ROBOT_HOST: str = os.environ.get("ROBOT_HOST", "picrawler.local")
ROBOT_PORT: int = int(os.environ.get("ROBOT_PORT", "8000"))

# Full base URL for the robot command server.
BASE_URL: str = os.environ.get("ROBOT_BASE_URL", f"http://{ROBOT_HOST}:{ROBOT_PORT}")

# Per-request timeout in seconds. Gait actions can take a few seconds, so this
# is generous; tighten once real gait durations are known.
REQUEST_TIMEOUT_S: float = float(os.environ.get("ROBOT_TIMEOUT_S", "15"))

# --- Wander / avoid loop (brain/wander.py) ------------------------------------
# Turn away when forward clearance drops below WANDER_MIN_CM; otherwise step.
# 35cm (up from 20) gives reaction margin at speed 100.
WANDER_MIN_CM: float = float(os.environ.get("WANDER_MIN_CM", "35"))
WANDER_TURN_DEG: float = float(os.environ.get("WANDER_TURN_DEG", "30"))
# Gait speed for wander's walk/turn (picrawler ~1-100; higher = faster). Set to
# 100 after a hardware sweep confirmed it's stable on this robot; lower it if you
# see any brownout on a marginal battery.
WANDER_SPEED: int = int(os.environ.get("WANDER_SPEED", "100"))
# Steps to walk per "clear" decision. 1 = re-check sensors every stride (shortest
# blind window / fastest reaction); raise for smoother-but-less-reactive motion.
WANDER_STEPS: int = int(os.environ.get("WANDER_STEPS", "1"))
# Idle pause between decisions. The gait itself takes time, so keep this small.
WANDER_STEP_DELAY_S: float = float(os.environ.get("WANDER_STEP_DELAY_S", "0.1"))

# Camera-assisted avoidance: the wander loop also polls the perception server and
# turns away from a detection that's large (close) and roughly ahead — catching
# obstacles the narrow ultrasonic beam misses (e.g. a thin/off-axis pole). Needs
# the perception server running; unreachable -> silently falls back to ultrasonic.
# (For arbitrary obstacles like poles, run perception with NanoOWL + obstacle
# prompts; YOLO only flags its COCO classes.)
PERCEPTION_BASE_URL: str = os.environ.get("PERCEPTION_BASE_URL", "http://localhost:8100")
WANDER_USE_CAMERA: bool = os.environ.get("WANDER_USE_CAMERA", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
# A detection counts as "in the way" if its box covers >= this fraction of the
# frame (close) AND its horizontal center is within WANDER_CENTER_BAND of center.
WANDER_OBSTACLE_AREA: float = float(os.environ.get("WANDER_OBSTACLE_AREA", "0.15"))
WANDER_CENTER_BAND: float = float(os.environ.get("WANDER_CENTER_BAND", "0.30"))
