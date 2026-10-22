"""Declarative body language, scheduled by the sole behavior coordinator.

Stock PiCrawler supports staged stand/sit, not a measured head tilt or tail wag.
No expression calls the network, sleeps, imposes a minimum speed, or translates.
"""

from dataclasses import dataclass

GESTURES = ("none", "wag", "perk", "rest")


@dataclass(frozen=True)
class Expression:
    gesture: str
    primitive: str | None = None
    sound: str | None = None
    reason: str = ""


def plan(gesture: str) -> Expression:
    if gesture == "wiggle":
        gesture = "wag"  # explicit compatibility migration, no turn macro
    plans = {
        "none": Expression("none"),
        "wag": Expression("wag", sound="acknowledge",
                          reason="acknowledgement sound; stock crawler has no tail"),
        "perk": Expression("perk", primitive="stand", sound="acknowledge"),
        "rest": Expression("rest", primitive="sit"),
    }
    return plans.get(gesture, Expression("none", reason=f"unsupported gesture: {gesture}"))


def express(gesture, client=None, *, speed=30, reflex_cm=20.0) -> Expression:
    """Compatibility seam returning a plan. Only Coordinator may execute it."""
    return plan(gesture)


def signature(mood, rng=None) -> str:
    return {"excited": "wag", "playful": "wag", "sleepy": "rest"}.get(mood, "none")


def idle(mood, rng=None) -> str:
    return "none"


def is_inplace(gesture: str) -> bool:
    return gesture in GESTURES
