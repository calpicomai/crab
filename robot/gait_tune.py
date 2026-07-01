"""Tune the custom walk gait — run this ON THE PI (no network involved).

The custom gait plays picrawler's real forward keyframes via do_step (so it
actually walks, unlike a hand-invented cycle) with a tunable stride scale. How
long a stride stays stable can only be judged on the physical robot — this runs a
few gait cycles so you can watch and adjust, without the brain or the wander loop.

Run from the repo root on the Pi, with the robot ELEVATED (feet off the ground)
for the first passes:
    robot/.venv/bin/python -m robot.gait_tune --cycles 3 --speed 40
    # scale 1.0 == picrawler's stock step; lengthen the stride and re-run:
    PICRAWLER_GAIT_STRIDE_SCALE=1.4 robot/.venv/bin/python -m robot.gait_tune --cycles 3 --speed 60

Then set it on the floor and confirm it moves forward (not in place). Once a scale
looks smooth and covers ground, run the server with PICRAWLER_GAIT_MODE=custom
(and PICRAWLER_GAIT_STRIDE_SCALE=...) so walk + wander use it — or tell me the
values and I'll make them the default.

SAFETY: start elevated and slow (low --speed, scale near 1.0). If a leg drives to
an extreme or the body lurches, cut power and lower the scale. Ctrl+C stops and sits.
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .gait import GaitEngine

BANNER = """\
============================================================
 PiCrawler custom-gait tuning
 - Elevate the robot for the first passes; keep --speed low.
 - scale 1.0 = picrawler's stock step; raise for a longer stride.
 - If a leg over-reaches or the body lurches, CUT POWER + lower scale.
 stride_scale=%(scale)s
============================================================"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the custom walk gait for tuning (Pi-local).")
    parser.add_argument("--cycles", type=int, default=3, help="Number of gait cycles to run.")
    parser.add_argument("--speed", type=int, default=config.STAND_SPEED, help="Gait speed 1-100 (start low).")
    args = parser.parse_args(argv)
    if not 1 <= args.speed <= 100:
        parser.error("--speed must be between 1 and 100")

    print(BANNER % {"scale": config.GAIT_STRIDE_SCALE})
    engine = GaitEngine(simulate=config.SIMULATE)
    if engine.simulate:
        print("(SIMULATE mode — printing intended do_step frames; no servos move.)")

    try:
        engine.stand()
        print(f"Running {args.cycles} gait cycle(s) at speed {args.speed}, stride_scale {config.GAIT_STRIDE_SCALE} ...")
        engine.walk(args.cycles, speed=args.speed, mode="custom")
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        engine.sit()

    print("Done. Adjust PICRAWLER_GAIT_STRIDE_SCALE (and --speed) and re-run to tune.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
