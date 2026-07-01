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
# (Legacy autonomy-v1 OR heuristic; autonomy v2 fuses both senses into a costmap
# — see below — and no longer uses these two, but they're kept for reference.)
WANDER_OBSTACLE_AREA: float = float(os.environ.get("WANDER_OBSTACLE_AREA", "0.15"))
WANDER_CENTER_BAND: float = float(os.environ.get("WANDER_CENTER_BAND", "0.30"))

# --- Local occupancy costmap (brain/costmap.py, autonomy v2) -------------------
# A robot-centered POLAR occupancy histogram (Vector-Field-Histogram style): the
# forward arc is split into COSTMAP_BINS angular bins over +/-COSTMAP_FOV_DEG
# (0 deg = straight ahead). Ultrasonic and camera each write into it where they're
# strong, short-memory decay + open-loop dead-reckoning keep it current, and the
# wander loop steers toward the clearest wide-enough gap. This is a LOCAL,
# ephemeral model of free-vs-blocked *directions* (Roomba-class) — not a metric
# world map (no IMU/odometry/depth on this robot, so a saved 3D map isn't
# reachable). All knobs are env-overridable.
COSTMAP_BINS: int = int(os.environ.get("COSTMAP_BINS", "37"))
# Half-width of the modeled arc in degrees; total span is 2x this (default +/-90).
COSTMAP_FOV_DEG: float = float(os.environ.get("COSTMAP_FOV_DEG", "90"))
# Horizontal field of view of the Pi camera, used to turn a detection's pixel
# x-center into a bearing. ~54 deg for the OV5647 (SunFounder default); measure
# and override for a different lens.
CAMERA_HFOV_DEG: float = float(os.environ.get("CAMERA_HFOV_DEG", "54"))
# The ultrasonic beam isn't a ray — it's a cone ~15-30 deg wide. A reading is
# splatted across this arc centered on straight-ahead.
SONAR_BEAM_DEG: float = float(os.environ.get("SONAR_BEAM_DEG", "20"))
# The robot's own half-width (cm). Obstacles are inflated by the angular size
# this subtends at their range so the robot never aims at a gap its body won't
# fit through ("can sense its own size").
FOOTPRINT_RADIUS_CM: float = float(os.environ.get("FOOTPRINT_RADIUS_CM", "12"))
# Per-cycle confidence multiplier (0-1). Lower = shorter memory = faster to
# forget stale readings, which bounds the open-loop dead-reckoning drift (no IMU).
COSTMAP_DECAY: float = float(os.environ.get("COSTMAP_DECAY", "0.6"))
# A bin at/above this confidence counts as blocked when finding gaps.
COSTMAP_BLOCKED_CONF: float = float(os.environ.get("COSTMAP_BLOCKED_CONF", "0.5"))
# Beyond this range (cm) a sonar reading is treated as "clear" (open space), not
# an obstacle — caps how far ahead we bother modeling.
COSTMAP_MAX_RANGE_CM: float = float(os.environ.get("COSTMAP_MAX_RANGE_CM", "120"))
# A gap must be at least this wide (deg) to be considered passable. Default 0 ->
# derived from FOOTPRINT_RADIUS_CM at COSTMAP_CLEARANCE_CM range; override to force.
MIN_GAP_DEG: float = float(os.environ.get("MIN_GAP_DEG", "0"))
# Range (cm) at which the footprint's angular width sets the default MIN_GAP_DEG.
COSTMAP_CLEARANCE_CM: float = float(os.environ.get("COSTMAP_CLEARANCE_CM", "40"))

# --- Rotate-to-scan (fixed sonar) ---------------------------------------------
# The sonar points straight ahead only, so to fill the off-center bins the robot
# turns its body in increments and reads at each. A sweep fires when the forward
# path is blocked and every SCAN_EVERY steps (0 disables the periodic sweep).
SCAN_EVERY: int = int(os.environ.get("SCAN_EVERY", "12"))
# Total sweep width (deg, split to each side) and per-increment turn.
SCAN_RANGE_DEG: float = float(os.environ.get("SCAN_RANGE_DEG", "60"))
SCAN_STEP_DEG: float = float(os.environ.get("SCAN_STEP_DEG", "20"))

# Open-vocabulary obstacle prompts pushed to the perception server (NanoOWL) on
# wander startup, so the camera flags arbitrary obstacles (a pole/chair leg/wall)
# that YOLO's fixed COCO classes miss. Comma-separated; only sent if perception
# reports a nanoowl backend loaded (YOLO-only stays a graceful subset).
COSTMAP_OBSTACLE_PROMPTS: list[str] = [
    p.strip()
    for p in os.environ.get(
        "COSTMAP_OBSTACLE_PROMPTS",
        "a pole,a chair leg,a table leg,a wall,furniture,an obstacle,a person,a box",
    ).split(",")
    if p.strip()
]
