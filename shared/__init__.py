"""Shared command protocol for the PiCrawler local-brain robot.

This package is imported by BOTH deploy targets:
  - robot/ (Raspberry Pi) implements the server side.
  - brain/ (Jetson) implements the client side.

Because the protocol is defined exactly once, here, the two nodes cannot drift.
"""

from .protocol import (
    Action, ACTION_PATHS, HEALTH_PATH, CAMERA_STREAM_PATH, CAMERA_FRAME_PATH,
    AUDIO_STREAM_PATH, AUDIO_PLAY_PATH, AUDIO_HEALTH_PATH, AUDIO_SELF_TEST_PATH,
    AUDIO_STOP_PATH, AUDIO_SESSION_HEADER, AUDIO_SEQUENCE_HEADER, AUDIO_LEASE_HEADER,
    CONTROL_LEASE_PATH, COMPANION_EVENTS_PATH, DEFAULT_SPEED, LEG_COUNT,
    Pose, WalkCommand, TurnCommand, TurnStepsCommand, StandCommand, SitCommand,
    StopCommand, ResumeCommand, GetStatusCommand, TestLegCommand, RobotStatus,
    CommandResponse, MotionResult, AudioEnvelope,
)

__all__ = [
    "Action", "ACTION_PATHS", "HEALTH_PATH", "CAMERA_STREAM_PATH", "CAMERA_FRAME_PATH",
    "AUDIO_STREAM_PATH", "AUDIO_PLAY_PATH", "AUDIO_HEALTH_PATH", "AUDIO_SELF_TEST_PATH",
    "AUDIO_STOP_PATH", "AUDIO_SESSION_HEADER", "AUDIO_SEQUENCE_HEADER", "AUDIO_LEASE_HEADER",
    "CONTROL_LEASE_PATH", "COMPANION_EVENTS_PATH", "DEFAULT_SPEED", "LEG_COUNT",
    "Pose", "WalkCommand", "TurnCommand", "TurnStepsCommand", "StandCommand", "SitCommand",
    "StopCommand", "ResumeCommand", "GetStatusCommand", "TestLegCommand", "RobotStatus",
    "CommandResponse", "MotionResult", "AudioEnvelope",
]
