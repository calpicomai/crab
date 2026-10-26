"""Bounded cross-machine capture timing; never command/lease authority.

A correlated request brackets the remote timestamp between local send/receive.
Keep the whole offset interval: midpoint estimates can understate capture age.
Anchors are local transport metadata, scoped to one connection and never learned
from an unsolicited observation. Original producer timestamps remain unchanged.
"""
from __future__ import annotations

import math
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_OFFSET_S = .250
MAX_RTT_S = .200
MAX_ANCHOR_AGE_S = 5.0
WALL_STEP_S = .020
DRIFT_S_PER_S = .001  # conservative 1000 ppm between one-second probes


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class CaptureClock(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    offset_lower_s: float = Field(ge=-MAX_OFFSET_S, le=MAX_OFFSET_S)
    offset_upper_s: float = Field(ge=-MAX_OFFSET_S, le=MAX_OFFSET_S)
    wall_at: float = Field(gt=0)
    mono_at: float = Field(ge=0)

    @model_validator(mode="after")
    def bounded_interval(self):
        if not 0 <= self.offset_upper_s-self.offset_lower_s <= MAX_RTT_S + 1e-6:
            raise ValueError("clock offset uncertainty exceeds bound")
        return self

    def remote_bounds(self, wall=None, mono=None):
        wall = time.time() if wall is None else wall
        mono = time.monotonic() if mono is None else mono
        if not finite_number(wall) or not finite_number(mono):
            raise ValueError("invalid local clock")
        elapsed = mono-self.mono_at
        if not 0 <= elapsed <= MAX_ANCHOR_AGE_S:
            raise ValueError("clock calibration expired")
        if abs(wall-self.wall_at-elapsed) > WALL_STEP_S:
            raise ValueError("local wall clock stepped")
        drift = elapsed*DRIFT_S_PER_S
        remote = self.wall_at+elapsed
        return remote+self.offset_lower_s-drift, remote+self.offset_upper_s+drift

    @classmethod
    def measure(cls, remote, sent_wall, sent_mono, wall, mono, previous=None):
        if not all(finite_number(x) for x in (remote, sent_wall, sent_mono, wall, mono)) or remote <= 0:
            raise ValueError("clock probe timestamp missing or malformed")
        rtt = mono-sent_mono
        if not 0 <= rtt <= MAX_RTT_S or abs(wall-sent_wall-rtt) > WALL_STEP_S:
            raise ValueError("clock probe RTT/uncertainty or wall step exceeds bound")
        # Use monotonic elapsed, so wall-clock adjustments cannot narrow RTT.
        sample = cls(offset_lower_s=remote-(sent_wall+rtt), offset_upper_s=remote-sent_wall,
                     wall_at=sent_wall+rtt, mono_at=mono)
        if previous is not None:
            lower, upper = previous.remote_bounds(wall, mono)
            if sample.wall_at+sample.offset_lower_s > upper or sample.wall_at+sample.offset_upper_s < lower:
                raise ValueError("remote clock stepped or probe intervals disagree")
        return sample


def capture_age(stamp, clock=None, *, wall=None, mono=None):
    """Conservative upper age, or None when timing cannot establish freshness."""
    if not finite_number(stamp) or stamp <= 0:
        return None
    wall = time.time() if wall is None else wall
    try:
        if clock is None:
            age = wall-stamp if finite_number(wall) else -1
        else:
            anchor = clock if isinstance(clock, CaptureClock) else CaptureClock.model_validate(clock)
            age = anchor.remote_bounds(wall, mono)[1]-stamp
        return age if age >= 0 else None
    except (ValueError, TypeError):
        return None


def event_age(event):
    return capture_age(event.get("timestamp"), event.get("_clock"))
