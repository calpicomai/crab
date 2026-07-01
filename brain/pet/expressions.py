"""Body language — short, safe gestures that make the robot read as a pet rather
than a Roomba. Each is built from the existing high-level abilities via
RobotClient (small in-place turns, a short reflex-protected step, sit), so
nothing here bypasses the safety layer.

Note: the crawler has no reverse gait, so "back away" is expressed as turning to
face away from whatever startled it.
"""

from __future__ import annotations

import time

from ..client import RobotClient

GESTURES = ("none", "wiggle", "tilt", "approach", "backaway", "rest")


def express(gesture: str, client: RobotClient, *, speed: int = 70, reflex_cm: float = 20.0) -> None:
    """Perform a small expressive gesture. Best-effort and safe; swallows errors
    so a gesture can never crash the pet loop."""
    try:
        if gesture == "wiggle":
            # A happy little shimmy: quick small alternating in-place turns.
            for deg in (14, -28, 28, -14):
                client.turn(deg, speed=speed)
        elif gesture == "tilt":
            # A curious head-tilt: a small turn and a beat of stillness.
            client.turn(12, speed=max(1, speed - 20))
            time.sleep(0.4)
        elif gesture == "approach":
            # Lean in toward something interesting — one reflex-protected step.
            client.walk(1, speed=speed, min_clearance_cm=reflex_cm)
        elif gesture == "backaway":
            # No reverse gait, so turn sharply to face away from the scare.
            client.turn(120, speed=speed)
        elif gesture == "rest":
            client.sit()
            time.sleep(0.3)
        # "none" (and anything unknown): do nothing.
    except Exception:  # noqa: BLE001 - a gesture must never crash the pet
        pass
