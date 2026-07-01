"""Tune the custom trot gait — run this ON THE PI (no network involved).

The trot's feel depends on stride / lift / neutral reach / speed, which can only
be judged on the physical robot. This runs a few trot cycles so you can watch and
adjust, without waiting on the brain or touching the wander loop.

Run from the repo root on the Pi, with the robot ELEVATED (feet off the ground)
for the first passes:
    robot/.venv/bin/python -m robot.gait_tune --cycles 3 --speed 40
    # then tune via env and re-run:
    PICRAWLER_GAIT_STRIDE=24 PICRAWLER_GAIT_LIFT_Z=-30 \
      robot/.venv/bin/python -m robot.gait_tune --cycles 3 --speed 60

Knobs (env): PICRAWLER_GAIT_X_NEUTRAL, PICRAWLER_GAIT_STRIDE,
PICRAWLER_GAIT_LIFT_Z, PICRAWLER_GAIT_DOWN_Z. Once it looks smooth and stable,
set PICRAWLER_GAIT_MODE=trot for the server (or tell me your values and I'll make
trot the default).

SAFETY: start elevated and slow (low --speed). Watch for a leg driving to an
extreme or the body lurching; cut power if so and lower the stride / raise the
neutral. Ctrl+C stops and sits.
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .gait import GaitEngine

BANNER = """\
============================================================
 PiCrawler trot-gait tuning
 - Elevate the robot for the first passes; keep --speed low.
 - Watch each leg; if one drives to an extreme or the body
   lurches, CUT POWER and reduce stride / raise neutral.
 x_neutral=%(xn)s stride=%(stride)s lift_z=%(lift)s down_z=%(down)s
============================================================"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the custom trot gait for tuning (Pi-local).")
    parser.add_argument("--cycles", type=int, default=3, help="Number of trot cycles to run.")
    parser.add_argument("--speed", type=int, default=config.STAND_SPEED, help="Gait speed 1-100 (start low).")
    args = parser.parse_args(argv)
    if not 1 <= args.speed <= 100:
        parser.error("--speed must be between 1 and 100")

    print(BANNER % {
        "xn": config.GAIT_X_NEUTRAL, "stride": config.GAIT_STRIDE,
        "lift": config.GAIT_LIFT_Z, "down": config.GAIT_DOWN_Z,
    })
    engine = GaitEngine(simulate=config.SIMULATE)
    if engine.simulate:
        print("(SIMULATE mode — printing intended do_step frames; no servos move.)")

    try:
        engine.stand()
        print(f"Running {args.cycles} trot cycle(s) at speed {args.speed} ...")
        engine.walk(args.cycles, speed=args.speed, mode="trot")
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        engine.sit()

    print("Done. Adjust the PICRAWLER_GAIT_* env knobs and re-run to tune.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
