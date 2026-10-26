"""Canonical companion intentions and bounded local mind output contract.

These are coordinator requests, never motor commands. An attention target does
not authorize approaching, following, or chasing it.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IntentionName(str, Enum):
    STOP = "stop"
    RESUME = "resume"
    ACKNOWLEDGE = "acknowledge"
    LISTEN = "listen"
    RESPOND = "respond"
    SETTLE = "settle"
    PLAY = "play"
    STAND = "stand"
    SIT = "sit"
    ORIENT = "orient"


class IntentionStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    FAILED = "failed"


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: IntentionName
    priority: int = Field(default=20, ge=0, le=100)
    created_at: float = Field(default_factory=time.time)
    expires_at: float
    source: str = "companion"
    reason: str = ""
    payload: dict = Field(default_factory=dict)
    status: IntentionStatus = IntentionStatus.PENDING
    result: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def expiry_after_creation(self):
        if self.expires_at < self.created_at:
            raise ValueError("intention expires before creation")
        return self


GESTURES = ("none", "perk", "rest", "wag")


class MindOutput(BaseModel):
    """One schema drives prompts and validation; safety controls are absent."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    say: str = Field(default="", max_length=240)
    intention: Literal["acknowledge", "listen", "respond", "settle", "play", "orient"] = "respond"
    gesture: Literal["none", "perk", "rest", "wag"] = "none"
    target: str | None = Field(default=None, max_length=64)
    mood_hint: Literal["curious", "calm", "playful", "cautious", "excited", "sleepy"] | None = None
    # A model's visual interpretation is inference, never an observed fact.
    observation: str = Field(default="", max_length=400)


CAPABILITY_SCHEMA = MindOutput.model_json_schema()
