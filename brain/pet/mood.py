"""The pet's mood — a small emotional state that shifts with what happens and
colors both how it talks and how it moves.

Kept deliberately simple and legible: a handful of moods, event-driven
transitions, and a mapping from mood to motion params + a default gesture. The
LLM (when present) can nudge the mood via a ``mood_hint``; without it, these
rules alone give the pet a believable inner life.
"""

from __future__ import annotations

from dataclasses import dataclass

# Recognized moods. `mood_hint`s from the LLM are snapped to the nearest of these.
MOODS = ("curious", "excited", "playful", "cautious", "startled", "bored", "sleepy")


@dataclass
class MoodParams:
    """How a mood shapes the body."""

    speed: int          # gait speed 1-100
    explore_bias: float # 0 = timid/slow to commit, 1 = eager to push forward
    gesture: str        # default expressive gesture for this mood


_PARAMS: dict[str, MoodParams] = {
    "curious":  MoodParams(speed=75, explore_bias=0.8, gesture="tilt"),
    "excited":  MoodParams(speed=95, explore_bias=1.0, gesture="wiggle"),
    "playful":  MoodParams(speed=90, explore_bias=0.9, gesture="wiggle"),
    "cautious": MoodParams(speed=55, explore_bias=0.4, gesture="none"),
    "startled": MoodParams(speed=60, explore_bias=0.2, gesture="backaway"),
    "bored":    MoodParams(speed=65, explore_bias=0.6, gesture="none"),
    "sleepy":   MoodParams(speed=45, explore_bias=0.3, gesture="rest"),
}


class Mood:
    """Current mood + event-driven transitions."""

    def __init__(self, start: str = "curious") -> None:
        self.current: str = start if start in _PARAMS else "curious"
        self._idle_ticks = 0

    def params(self) -> MoodParams:
        return _PARAMS[self.current]

    def nudge(self, hint: str | None) -> None:
        """Let the LLM steer the mood, snapped to a known mood."""
        if hint:
            h = hint.strip().lower()
            if h in _PARAMS:
                self.current = h

    def update(self, *, saw_person: bool = False, saw_new: bool = False,
               blocked: bool = False, reflex: bool = False, moved_forward: bool = False) -> str:
        """Transition from sensed events. Returns the (possibly new) mood.

        Priority: a scare/close call wins, then delight at a person/new thing,
        then boredom creeps in when nothing happens. Idle long enough -> sleepy.
        """
        if reflex or blocked:
            self.current = "startled" if reflex else "cautious"
            self._idle_ticks = 0
        elif saw_person:
            self.current = "excited"
            self._idle_ticks = 0
        elif saw_new:
            self.current = "curious"
            self._idle_ticks = 0
        else:
            # Nothing notable this tick.
            self._idle_ticks += 1
            if self._idle_ticks >= 12:
                self.current = "sleepy"
            elif self._idle_ticks >= 6:
                self.current = "bored"
            elif self.current in ("startled", "cautious") and moved_forward:
                # Calm back down to playful once it's moving freely again.
                self.current = "playful"
        return self.current
