"""Runtime configuration for the Pi server.

Values are read from environment variables so the systemd unit (and the Jetson
operator during bring-up) can override them without editing code.
"""

from __future__ import annotations

import os

# Bind address for the FastAPI server. 0.0.0.0 so the Jetson on the LAN can reach it.
HOST: str = os.environ.get("PICRAWLER_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PICRAWLER_PORT", "8000"))

# Force simulate mode (no hardware calls) even when picrawler is importable.
# The GaitEngine also auto-enables simulate when picrawler/robot_hat are missing.
# Accepts 1/true/yes/on (case-insensitive).
SIMULATE: bool = os.environ.get("PICRAWLER_SIMULATE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Movement-safety tunables for the staged stand/sit (see robot/gait.py and the
# "Movement safety / brownout" section in the README). stand/sit move one leg at
# a time at STAND_SPEED with LEG_SETTLE_S between legs, so only ~3 servos draw
# current at once. Lower STAND_SPEED / raise LEG_SETTLE_S to be gentler on a
# marginal battery.
STAND_SPEED: int = int(os.environ.get("PICRAWLER_STAND_SPEED", "40"))
LEG_SETTLE_S: float = float(os.environ.get("PICRAWLER_LEG_SETTLE_S", "0.2"))
