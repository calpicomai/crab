"""Per-leg movement diagnostic — run this ON THE PI (no network involved).

Use it to isolate a leg that drives to a wrong/extreme position (a stall draws
max current and can brown out the Pi on the shared Robot HAT rail). It moves ONE
leg at a time to the standing coordinate, gently, so only 3 servos are ever
loaded.

Run from the repo root on the Pi:
    robot/.venv/bin/python -m robot.diagnose --all            # step legs 0..3, pausing between
    robot/.venv/bin/python -m robot.diagnose --leg 2          # just leg 2
    robot/.venv/bin/python -m robot.diagnose --all --speed 30 # even gentler

SAFETY: elevate the robot so the legs hang free. Watch each leg. If a leg drives
to an extreme angle or binds/buzzes (a stall), CUT POWER immediately — that leg's
calibration, servo-horn seating, or wiring (PIN_LIST channel) is wrong. Re-run
the SunFounder calibration tool / re-seat the horn, then test that leg again.
"""

from __future__ import annotations

import argparse
import sys
import time

from shared import LEG_COUNT

from . import config
from .gait import STAND_COORD, GaitEngine

BANNER = """\
============================================================
 PiCrawler per-leg diagnostic
 - Elevate the robot; keep legs clear.
 - Watch each leg move to its STANDING position.
 - If a leg goes to a WRONG/EXTREME angle or stalls (buzz/bind),
   CUT POWER and fix that leg (calibration / horn / wiring).
============================================================"""


def _move(engine: GaitEngine, leg: int, speed: int, pause: float) -> None:
    print(f"-> leg {leg}: moving to standing coord {STAND_COORD[leg]} at speed {speed}")
    engine.test_leg(leg, speed)
    time.sleep(pause)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move PiCrawler legs one at a time to isolate a bad leg.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--leg", type=int, choices=range(LEG_COUNT), help="Move a single leg (0-3).")
    group.add_argument("--all", action="store_true", help="Move legs 0..3 in turn, pausing between.")
    parser.add_argument("--speed", type=int, default=config.STAND_SPEED, help=f"Servo speed 1-100 (default {config.STAND_SPEED}).")
    parser.add_argument("--pause", type=float, default=1.5, help="Seconds to pause between legs in --all (default 1.5).")
    args = parser.parse_args(argv)

    if not 1 <= args.speed <= 100:
        parser.error("--speed must be between 1 and 100")

    print(BANNER)
    engine = GaitEngine(simulate=config.SIMULATE)
    if engine.simulate:
        print("(SIMULATE mode — no servos will move; this just prints intended moves.)")

    if args.all:
        for leg in range(LEG_COUNT):
            _move(engine, leg, args.speed, args.pause)
    else:
        _move(engine, args.leg, args.speed, 0.0)

    print("Done. If any leg misbehaved, fix its calibration/horn/wiring and re-run for that leg.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
