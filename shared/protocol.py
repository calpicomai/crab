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
# speeds are roughly 0-100 (higher = faster); 80 is a stable default.
DEFAULT_SPEED: int = 80


class Action(str, Enum):
    """Canonical action names. Used as the wire identifier for every command."""

    WALK = "walk"
    TURN = "turn"
    STAND = "stand"
    SIT = "sit"
    GET_STATUS = "get_status"


# Canonical HTTP path for each action. Both server and client read this map so
# neither hardcodes a path string. A future WebSocket transport can key its
# message dispatch off Action directly and ignore these paths.
ACTION_PATHS: dict[Action, str] = {
    Action.WALK: "/walk",
    Action.TURN: "/turn",
    Action.STAND: "/stand",
    Action.SIT: "/sit",
    Action.GET_STATUS: "/status",
}

# Liveness endpoint (not an Action — no robot movement, used by systemd/monitoring).
HEALTH_PATH: str = "/health"


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


class CommandResponse(BaseModel):
    """Uniform envelope returned by every endpoint."""

    ok: bool
    action: Action
    detail: str = ""
    status: RobotStatus | None = None
