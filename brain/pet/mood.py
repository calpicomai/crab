"""Small, elapsed-time companion state; no random mood or tick-count cycling."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

MOODS = ("calm", "curious", "excited", "playful", "cautious", "startled", "bored", "sleepy")


@dataclass(frozen=True)
class Temperament:
    """Stable interaction preferences. These are never actuator safety limits."""
    hesitation_s: float = 0.6
    listen_s: float = 5.0
    preferred_distance_cm: float = 70.0
    tempo: float = 1.0
    curiosity: float = 0.5
    persistence_s: float = 8.0

    @classmethod
    def from_traits(cls, traits) -> "Temperament":
        traits = set(traits or [])
        reserved = bool(traits & {"shy", "cautious", "skittish", "gentle", "watchful"})
        lively = bool(traits & {"playful", "bold", "energetic", "mischievous", "brave"})
        curious = "curious" in traits
        return cls(hesitation_s=1.0 if reserved else 0.35,
                   listen_s=6.0 if reserved else 4.0,
                   preferred_distance_cm=90.0 if reserved else 65.0,
                   tempo=0.8 if reserved else (1.15 if lively else 1.0),
                   curiosity=0.75 if curious else (0.6 if lively else 0.4),
                   persistence_s=10.0 if curious else (6.0 if reserved else 8.0))


@dataclass(frozen=True)
class MoodParams:
    speed: int
    explore_bias: float
    gesture: str


class Mood:
    def __init__(self, start: str = "calm", *, clock=time.monotonic, traits=None) -> None:
        self.clock = clock
        self.temperament = Temperament.from_traits(traits)
        self.current = start if start in MOODS else "calm"
        self.engagement = 0.0
        self.alertness = 0.0
        self.interactions = 0
        self._last = clock()
        self._last_event = self._last
        self._blocked = False
        self._play_until = 0.0

    def advance(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        dt = max(0.0, now - self._last)
        self._last = max(now, self._last)
        self.engagement *= math.exp(-dt / 35.0)
        self.alertness *= math.exp(-dt / 12.0)
        if self._blocked:
            self.current = "cautious"
        elif self.alertness > 0.7:
            self.current = "startled"
        elif now < self._play_until:
            self.current = "playful"
        elif self.engagement > 0.65:
            self.current = "excited"
        elif self.engagement > 0.2:
            self.current = "curious"
        elif now - self._last_event >= 120.0:
            self.current = "sleepy"
        else:
            self.current = "calm"

    def event(self, event: str) -> None:
        self.advance()
        if event in {"engagement", "praise", "notice", "play"}:
            self._last_event = self.clock()
            self.engagement = min(1.0, self.engagement + (0.45 if event != "notice" else 0.22))
            self.interactions += int(event != "notice")
        if event == "play":
            self._play_until = self.clock() + self.temperament.persistence_s
        elif event == "startle":
            self.alertness = 1.0
        self.advance()

    def nudge(self, hint: str | None) -> None:
        """Generated words cannot manufacture grounded events."""
        self.advance()

    def params(self) -> MoodParams:
        return MoodParams(speed=30, explore_bias=0.0,
                          gesture="rest" if self.current == "sleepy" else "none")

    def update(self, *, saw_person=False, saw_new=False, blocked=False,
               reflex=False, moved_forward=False, chasing=False) -> str:
        self._blocked = bool(blocked)
        if reflex:
            self.event("startle")
        elif saw_person or saw_new:
            self.event("notice")
        self.advance()
        return self.current

    def snapshot(self) -> dict:
        self.advance()
        return {"mood": self.current, "engagement": round(self.engagement, 3),
                "alertness": round(self.alertness, 3), "interactions": self.interactions,
                "temperament": asdict(self.temperament)}
