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
WANDER_MIN_CM: float = float(os.environ.get("WANDER_MIN_CM", "20"))
WANDER_TURN_DEG: float = float(os.environ.get("WANDER_TURN_DEG", "30"))
WANDER_STEP_DELAY_S: float = float(os.environ.get("WANDER_STEP_DELAY_S", "0.5"))
