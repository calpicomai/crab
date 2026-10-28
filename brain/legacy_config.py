"""Legacy simulation/navigation settings, evaluated only when requested.

Names remain accessible through brain.config. Importing a RobotClient or the
stationary companion never parses unrelated navigation knobs. Invalid settings
still fail when their consumer requests them; no fallback changes a limit.
"""
from __future__ import annotations

import math
import os

_VALUES = {
    'WANDER_MIN_CM': lambda: float(os.environ.get("WANDER_MIN_CM", "35")),
    'WANDER_TURN_DEG': lambda: float(os.environ.get("WANDER_TURN_DEG", "30")),
    'WANDER_SPEED': lambda: int(os.environ.get("WANDER_SPEED", "100")),
    'WANDER_STEPS': lambda: int(os.environ.get("WANDER_STEPS", "1")),
    'WANDER_STEP_DELAY_S': lambda: float(os.environ.get("WANDER_STEP_DELAY_S", "0.0")),
    'WANDER_REFLEX_CM': lambda: float(os.environ.get("WANDER_REFLEX_CM", "25")),
    'WANDER_USE_CAMERA': lambda: os.environ.get("WANDER_USE_CAMERA", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
},
    'WANDER_OBSTACLE_AREA': lambda: float(os.environ.get("WANDER_OBSTACLE_AREA", "0.15")),
    'WANDER_CENTER_BAND': lambda: float(os.environ.get("WANDER_CENTER_BAND", "0.30")),
    'COSTMAP_BINS': lambda: int(os.environ.get("COSTMAP_BINS", "37")),
    'COSTMAP_FOV_DEG': lambda: float(os.environ.get("COSTMAP_FOV_DEG", "90")),
    'CAMERA_HFOV_DEG': lambda: float(os.environ.get("CAMERA_HFOV_DEG", "54")),
    'SONAR_BEAM_DEG': lambda: float(os.environ.get("SONAR_BEAM_DEG", "20")),
    'FOOTPRINT_RADIUS_CM': lambda: float(os.environ.get("FOOTPRINT_RADIUS_CM", "16")),
    'COSTMAP_DECAY': lambda: float(os.environ.get("COSTMAP_DECAY", "0.6")),
    'COSTMAP_BLOCKED_CONF': lambda: float(os.environ.get("COSTMAP_BLOCKED_CONF", "0.45")),
    'COSTMAP_MAX_RANGE_CM': lambda: float(os.environ.get("COSTMAP_MAX_RANGE_CM", "120")),
    'MIN_GAP_DEG': lambda: float(os.environ.get("MIN_GAP_DEG", "0")),
    'COSTMAP_CLEARANCE_CM': lambda: float(os.environ.get("COSTMAP_CLEARANCE_CM", "40")),
    'SCAN_EVERY': lambda: int(os.environ.get("SCAN_EVERY", "12")),
    'SCAN_RANGE_DEG': lambda: float(os.environ.get("SCAN_RANGE_DEG", "60")),
    'SCAN_STEP_DEG': lambda: float(os.environ.get("SCAN_STEP_DEG", "20")),
    'COSTMAP_OBSTACLE_PROMPTS': lambda: [
    p.strip()
    for p in os.environ.get(
        "COSTMAP_OBSTACLE_PROMPTS",
        "a pole,a chair leg,a table leg,a wall,furniture,an obstacle,a person,a box",
    ).split(",")
    if p.strip()
],
}


def __getattr__(name: str):
    factory = _VALUES.get(name)
    if factory is None:
        raise AttributeError(name)
    try:
        value = factory()
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} has an invalid legacy navigation value") from None
    return value
