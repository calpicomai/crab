"""Shared command protocol for the PiCrawler local-brain robot.

This package is imported by BOTH deploy targets:
  - robot/ (Raspberry Pi) implements the server side.
  - brain/ (Jetson) implements the client side.

Because the protocol is defined exactly once, here, the two nodes cannot drift.
"""

from .protocol import (
    Action,
    ACTION_PATHS,
    HEALTH_PATH,
    DEFAULT_SPEED,
    Pose,
    WalkCommand,
    TurnCommand,
    StandCommand,
    SitCommand,
    GetStatusCommand,
    RobotStatus,
    CommandResponse,
)

__all__ = [
    "Action",
    "ACTION_PATHS",
    "HEALTH_PATH",
    "DEFAULT_SPEED",
    "Pose",
    "WalkCommand",
    "TurnCommand",
    "StandCommand",
    "SitCommand",
    "GetStatusCommand",
    "RobotStatus",
    "CommandResponse",
]
