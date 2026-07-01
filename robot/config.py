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

# Ultrasonic distance sensor (robot_hat), used by the brain's wander/avoid loop.
# Default pins are the SunFounder PiCrawler wiring (trig=D2, echo=D3). Disable
# with PICRAWLER_ULTRASONIC_ENABLED=0. Missing robot_hat -> DistanceSensor simulates.
ULTRASONIC_ENABLED: bool = os.environ.get("PICRAWLER_ULTRASONIC_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
ULTRASONIC_TRIG: str = os.environ.get("PICRAWLER_ULTRASONIC_TRIG", "D2")
ULTRASONIC_ECHO: str = os.environ.get("PICRAWLER_ULTRASONIC_ECHO", "D3")

# Walk gait selection (see robot/gait.py). "canned" uses picrawler's built-in
# do_action('forward') — the proven default. "custom" plays picrawler's real
# forward keyframes via do_step with a tunable stride scale — same motion at
# scale 1.0, longer step above. Tune on the robot (elevated) via robot/gait_tune.py,
# then flip the default.
GAIT_MODE: str = os.environ.get("PICRAWLER_GAIT_MODE", "canned").strip().lower()
# Stride length multiplier for the custom gait: 1.0 = picrawler's exact step
# (known to walk); >1.0 reaches/sweeps further per step (longer stride). Push it
# up on hardware until the step is as long as stays stable.
GAIT_STRIDE_SCALE: float = float(os.environ.get("PICRAWLER_GAIT_STRIDE_SCALE", "1.0"))

# Pose to gently home into when the server starts, instead of leaving the legs
# in picrawler's splayed power-on pose. One of "stand", "sit", or "none". Uses
# the same staged, low-speed motion as the stand/sit commands. "stand" only
# makes sense once every leg is calibrated (an uncalibrated leg could stall).
HOME_ON_START: str = os.environ.get("PICRAWLER_HOME_ON_START", "stand").strip().lower()

# Camera (on the Pi) served as MJPEG to the Jetson. Disable with CAMERA_ENABLED=0.
# picamera2 (system package, visible to the --system-site-packages venv) captures;
# if it's missing the camera serves synthetic frames so the video link still runs.
CAMERA_ENABLED: bool = os.environ.get("PICRAWLER_CAMERA_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
CAMERA_WIDTH: int = int(os.environ.get("PICRAWLER_CAMERA_WIDTH", "640"))
CAMERA_HEIGHT: int = int(os.environ.get("PICRAWLER_CAMERA_HEIGHT", "480"))
CAMERA_FPS: int = int(os.environ.get("PICRAWLER_CAMERA_FPS", "15"))
CAMERA_QUALITY: int = int(os.environ.get("PICRAWLER_CAMERA_QUALITY", "80"))
