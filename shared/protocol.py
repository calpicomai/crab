"""Command protocol — the single source of truth for robot <-> brain messaging.

Design rules (see CLAUDE.md):
  * Defined ONCE here. Both the Pi server (robot/server.py) and the Jetson
    client (brain/client.py) import these models so they can never drift.
  * Transport-free. These are plain Pydantic models plus a table of canonical
    paths. Stage 1 speaks HTTP, but a WebSocket transport can be added later
    by reusing these exact models WITHOUT changing the protocol.
  * Never carries per-servo timing. High-level intent only (walk N steps, turn
    D degrees, ...). Real-time gait timing stays entirely on the Pi.

Roadmap note: a command paired with its CommandResponse is a natural
"experience record". The future learning layer can log that stream without any
change here — that is why responses always echo the action and carry status.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# Default gait speed handed to picrawler when a command omits one. picrawler
# speeds are roughly 0-100 (higher = faster); 50 is picrawler's own default and
# is gentler on the shared servo/Pi power rail than a higher value (a faster
# move draws more instantaneous current — see the brownout notes in the README).
DEFAULT_SPEED: int = 50


class Action(str, Enum):
    """Canonical action names. Used as the wire identifier for every command."""

    WALK = "walk"
    TURN = "turn"
    STAND = "stand"
    SIT = "sit"
    GET_STATUS = "get_status"
    # Diagnostic/maintenance op: move ONE leg to the standing pose to isolate a
    # miscalibrated / mis-wired leg. Only a leg index + speed cross the network;
    # the Pi owns the coordinate, so the "no low-level control over the network"
    # constraint still holds.
    TEST_LEG = "test_leg"


# Canonical HTTP path for each action. Both server and client read this map so
# neither hardcodes a path string. A future WebSocket transport can key its
# message dispatch off Action directly and ignore these paths.
ACTION_PATHS: dict[Action, str] = {
    Action.WALK: "/walk",
    Action.TURN: "/turn",
    Action.STAND: "/stand",
    Action.SIT: "/sit",
    Action.GET_STATUS: "/status",
    Action.TEST_LEG: "/diagnostics/leg",
}

# Number of legs (each with 3 servos). Bounds the diagnostic leg index.
LEG_COUNT: int = 4

# Liveness endpoint (not an Action — no robot movement, used by systemd/monitoring).
HEALTH_PATH: str = "/health"

# Camera lives on the robot (Pi). It serves frames over the LAN; the Jetson brain
# pulls them for perception. These are not Actions (binary image data, not the
# command/response envelope) — just canonical paths both sides agree on.
CAMERA_STREAM_PATH: str = "/camera/stream"  # multipart/x-mixed-replace MJPEG
CAMERA_FRAME_PATH: str = "/camera/frame"  # single image/jpeg

# Audio lives on the robot (Pi) too — a mic and a speaker — while the heavy STT
# (Whisper) and TTS (Piper) run on the Jetson brain. Same split as the camera:
# the Pi is the device, the Jetson is the compute backend. Not Actions (raw audio
# bytes, not the command envelope) — just canonical paths both sides agree on.
AUDIO_STREAM_PATH: str = "/audio/stream"  # Pi mic -> Jetson: raw 16-bit PCM
AUDIO_PLAY_PATH: str = "/audio/play"  # Jetson TTS -> Pi speaker: POST a WAV to play


class Pose(str, Enum):
    """Coarse body pose the robot tracks between commands."""

    UNKNOWN = "unknown"
    STANDING = "standing"
    SITTING = "sitting"


# --------------------------------------------------------------------------- #
# Command models (brain -> robot)
# --------------------------------------------------------------------------- #


class WalkCommand(BaseModel):
    """Walk forward a whole number of gait cycles."""

    steps: int = Field(default=1, ge=0, le=100, description="Number of gait cycles.")
    speed: int = Field(default=DEFAULT_SPEED, ge=1, le=100, description="Gait speed 1-100.")
    # Optional safety margin: the Pi checks forward clearance between gait cycles
    # and aborts the walk early if it drops below this (a fast on-robot reflex, so
    # motion can't blindly ram an obstacle mid-stride). None -> use the Pi's
    # configured default. This is high-level intent (a margin), not per-servo
    # timing, so it respects the "no low-level control over the network" rule.
    min_clearance_cm: float | None = Field(
        default=None, ge=0.0, description="Reflex-stop clearance; None = Pi default."
    )


class TurnCommand(BaseModel):
    """Turn in place by a signed angle (positive = right/clockwise)."""

    degrees: float = Field(..., ge=-360.0, le=360.0, description="Signed turn angle.")
    speed: int = Field(default=DEFAULT_SPEED, ge=1, le=100, description="Gait speed 1-100.")


class StandCommand(BaseModel):
    """Move to the neutral standing pose."""


class SitCommand(BaseModel):
    """Lower the body to the resting/sitting pose."""


class GetStatusCommand(BaseModel):
    """Request the current robot status. Carries no parameters."""


class TestLegCommand(BaseModel):
    """Diagnostic: move a single leg to the standing pose, gently.

    Used to isolate a leg that drives to a wrong/extreme position (a stall risks
    a power brownout). The target coordinate is chosen on the Pi; only the leg
    index and speed travel over the network.
    """

    leg: int = Field(..., ge=0, le=LEG_COUNT - 1, description="Leg index 0-3.")
    speed: int = Field(default=DEFAULT_SPEED, ge=1, le=100, description="Servo speed 1-100.")


# --------------------------------------------------------------------------- #
# Response models (robot -> brain)
# --------------------------------------------------------------------------- #


class RobotStatus(BaseModel):
    """A snapshot of robot state, returned with every response."""

    pose: Pose = Pose.UNKNOWN
    is_moving: bool = False
    servo_count: int = 12
    simulate: bool = False
    uptime_s: float | None = None
    # Forward ultrasonic clearance in cm (None = no sensor / no echo). The brain's
    # wander/avoid loop reads this from get_status to decide when to turn away.
    distance_cm: float | None = None
    # True when the most recent walk aborted early because the Pi's reflex saw the
    # forward clearance drop below the reflex distance mid-stride. The brain treats
    # this as an authoritative close-range obstacle and steers away.
    reflex_stopped: bool = False


class CommandResponse(BaseModel):
    """Uniform envelope returned by every endpoint."""

    ok: bool
    action: Action
    detail: str = ""
    status: RobotStatus | None = None
