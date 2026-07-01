"""Body language — the dog-like part. The pet shouldn't just walk; it should
*emote* with its whole body, and do it often.

Every gesture is built from the robot's existing high-level abilities via
RobotClient (small in-place turns, short reflex-protected steps, stand/sit at
varying speed/amplitude), so nothing here bypasses the safety layer. The crawler
has no tail or reverse gait, so a "wag" is a fast little body shimmy and "back
away" / "cower" are turn-away + hunker-down approximations.

`signature(mood)` is the big reaction to do when a mood first hits; `idle(mood)`
is a smaller fidget to sprinkle in between steps so it always reads as a living
creature. `express()` performs any gesture by name.
"""

from __future__ import annotations

import random
import time

from ..client import RobotClient

GESTURES = (
    "none", "wag", "spin", "playbow", "perk", "sniff", "cower",
    "pounce", "hop", "tilt", "shake", "rest", "approach", "backaway",
)

# Which gestures are safe to fire freely (in-place or a tiny guarded nudge) vs.
# those that translate the body (left to navigation / used only as reactions).
_INPLACE = {"wag", "spin", "perk", "cower", "tilt", "shake", "rest", "playbow", "none"}


def express(gesture: str, client: RobotClient, *, speed: int = 80, reflex_cm: float = 20.0) -> None:
    """Perform one expressive gesture. Best-effort and safe — never raises."""
    try:
        if gesture == "wag":
            # Excited full-body wag: fast little alternating turns.
            for deg in (10, -10, 10, -10, 8, -8):
                client.turn(deg, speed=max(60, speed))
        elif gesture == "spin":
            # Zoomies: a quick happy spin in place.
            client.turn(random.choice((360.0, -360.0)), speed=max(70, speed))
        elif gesture == "playbow":
            # Play-bow invitation: dip down and pop back up.
            client.sit()
            time.sleep(0.25)
            client.stand()
        elif gesture == "perk":
            # Ears-up alert: stand tall and hold, with a tiny look.
            client.stand()
            client.turn(8, speed=max(1, speed - 30))
            time.sleep(0.3)
        elif gesture == "sniff":
            # Investigate: little nose-down nudges forward with pauses.
            for _ in range(2):
                client.walk(1, speed=max(1, speed - 30), min_clearance_cm=reflex_cm)
                time.sleep(0.2)
        elif gesture == "cower":
            # Scared: hunker down and freeze a beat.
            client.sit()
            time.sleep(0.4)
        elif gesture == "pounce":
            # A quick eager hop toward something (reflex-protected).
            client.walk(1, speed=max(80, speed), min_clearance_cm=reflex_cm)
        elif gesture == "hop":
            # Happy bounce: a quick step + a little wiggle.
            client.walk(1, speed=max(80, speed), min_clearance_cm=reflex_cm)
            client.turn(12, speed=max(70, speed))
            client.turn(-12, speed=max(70, speed))
        elif gesture == "tilt":
            # Curious head-tilt: a small turn and a beat of stillness.
            client.turn(12, speed=max(1, speed - 25))
            time.sleep(0.4)
        elif gesture == "shake":
            # Shake it off: rapid small wags.
            for deg in (14, -14, 14, -14):
                client.turn(deg, speed=max(70, speed))
        elif gesture == "approach":
            client.walk(1, speed=speed, min_clearance_cm=reflex_cm)
        elif gesture == "backaway":
            client.turn(random.choice((110.0, -110.0)), speed=speed)
        elif gesture == "rest":
            client.sit()
            time.sleep(0.3)
        # "none" / unknown: nothing.
    except Exception:  # noqa: BLE001 - a gesture must never crash the pet
        pass


# The big reaction when a mood first takes hold — its signature dog move.
_SIGNATURE = {
    "excited":  ["wag", "spin", "hop"],
    "playful":  ["playbow", "spin", "pounce"],
    "curious":  ["tilt", "perk", "sniff"],
    "cautious": ["perk"],
    "startled": ["cower", "backaway"],
    "bored":    ["sniff", "tilt"],
    "sleepy":   ["rest"],
}

# Smaller fidgets to sprinkle between steps so it's never "just walking".
_IDLE = {
    "excited":  ["wag", "hop"],
    "playful":  ["wag", "spin"],
    "curious":  ["tilt", "sniff"],
    "cautious": ["perk"],
    "startled": ["shake"],
    "bored":    ["tilt", "sniff"],
    "sleepy":   ["rest"],
}


def signature(mood: str, rng: random.Random | None = None) -> str:
    r = rng or random
    return r.choice(_SIGNATURE.get(mood, ["tilt"]))


def idle(mood: str, rng: random.Random | None = None) -> str:
    r = rng or random
    return r.choice(_IDLE.get(mood, ["tilt"]))


def is_inplace(gesture: str) -> bool:
    return gesture in _INPLACE
