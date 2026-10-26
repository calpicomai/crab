"""Health metadata shared by capture, transport and behavior.

Timestamps are Unix seconds. Receipt never refreshes capture time. A measured,
bounded clock anchor may accompany remote capture timestamps.
"""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .clock import CaptureClock, capture_age


class InputHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    available: bool = False
    provenance: Literal["real", "simulated", "unknown"] = "unknown"
    captured_at: float | None = None
    clock: CaptureClock | None = None
    received_at: float | None = None
    max_age_s: float = Field(default=2.0, gt=0, le=300)
    confidence: float | None = Field(default=None, ge=0, le=1)
    failure_reason: str | None = None
    source: str = ""
    used: bool = False
    ignored_reason: str | None = "not consumed by behavior"

    def fresh(self, now: float | None = None, *, mono: float | None = None) -> bool:
        now = time.time() if now is None else now
        age = capture_age(self.captured_at, self.clock, wall=now, mono=mono)
        return bool(self.available and age is not None and age <= self.max_age_s)

    def usable(self, now: float | None = None, *, allow_simulated: bool = False, mono: float | None = None) -> bool:
        return (self.fresh(now, mono=mono) and self.failure_reason is None
                and (self.provenance == "real"
                     or (allow_simulated and self.provenance == "simulated")))

    def snapshot(self, now: float | None = None, *, mono: float | None = None) -> dict:
        now = time.time() if now is None else now
        data = self.model_dump()
        age = capture_age(self.captured_at, self.clock, wall=now, mono=mono)
        data.update(fresh=self.fresh(now, mono=mono),
                    age_s=None if age is None else round(age, 3))
        return data


def unavailable(reason: str, *, source: str = "", provenance: str = "unknown") -> InputHealth:
    return InputHealth(source=source, provenance=provenance, failure_reason=reason[:300],
                       received_at=time.time(), ignored_reason=reason[:300])
