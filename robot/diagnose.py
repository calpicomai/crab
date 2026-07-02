"""Pi-local hardware diagnostics — run this ON THE PI (no network involved).

Two things it isolates, without the brain in the loop:

* **A bad leg** (`--leg` / `--all`): a leg that drives to a wrong/extreme
  position stalls, drawing max current, and can brown out the Pi on the shared
  Robot HAT rail. Moves ONE leg at a time to the standing coordinate, gently, so
  only ~3 servos are ever loaded.
* **A silent ultrasonic** (`--sonar` / `--sonar-scan`): the sensor reads nothing,
  so the brain thinks it's always clear (or, with the costmap, never gets a real
  range). Reads the sensor directly and prints each value, and can scan common
  trig/echo pin pairs to find where it's actually wired.

Run from the repo root on the Pi:
    robot/.venv/bin/python -m robot.diagnose --all              # step legs 0..3, pausing
    robot/.venv/bin/python -m robot.diagnose --leg 2            # just leg 2
    robot/.venv/bin/python -m robot.diagnose --sonar            # read the ultrasonic 10x
    robot/.venv/bin/python -m robot.diagnose --sonar 30 --pings 8
    robot/.venv/bin/python -m robot.diagnose --sonar-scan       # find the trig/echo pins

SAFETY (legs only): elevate the robot so the legs hang free. Watch each leg. If a
leg drives to an extreme angle or binds/buzzes (a stall), CUT POWER immediately —
that leg's calibration, servo-horn seating, or wiring (PIN_LIST channel) is wrong.
Re-run the SunFounder calibration tool / re-seat the horn, then test again. (The
sonar reads move no servos and are safe to run anytime.)
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

# Trig/echo pairs to try when scanning for the ultrasonic. D2/D3 is the SunFounder
# PiCrawler default; the reversed and neighbouring pairs cover a mis-plugged or
# differently-wired module.
_SCAN_PAIRS = [("D2", "D3"), ("D3", "D2"), ("D0", "D1"), ("D1", "D0"),
               ("D4", "D5"), ("D5", "D4")]


def _move(engine: GaitEngine, leg: int, speed: int, pause: float) -> None:
    print(f"-> leg {leg}: moving to standing coord {STAND_COORD[leg]} at speed {speed}")
    engine.test_leg(leg, speed)
    time.sleep(pause)


def _sonar(trig: str, echo: str, pings: int, count: int) -> int:
    """Read the ultrasonic `count` times on the given pins and print each value."""
    from .sensors import DistanceSensor

    config.ULTRASONIC_PINGS = pings  # DistanceSensor.read_cm reads this at call time
    sensor = DistanceSensor(trig=trig, echo=echo, simulate=config.SIMULATE)
    if sensor.simulate:
        print("!! robot_hat Ultrasonic is NOT available here (or PICRAWLER_SIMULATE=1),")
        print("   so these are SIMULATED readings, not the real sensor. On the Pi make")
        print("   sure robot/.venv was built with --system-site-packages (re-run")
        print("   robot/setup.sh) so it can import robot_hat.\n")
    print(f"Reading ultrasonic on trig={trig} echo={echo} ({pings} pings/read, {count} reads):")
    valid = 0
    for i in range(count):
        d = sensor.read_cm()
        if d is None:
            print(f"  [{i + 1:>2}] no echo (None)")
        else:
            valid += 1
            print(f"  [{i + 1:>2}] {d:6.1f} cm")
        time.sleep(0.15)
    print(f"\n{valid}/{count} reads returned a distance.")
    if valid == 0 and not sensor.simulate:
        print("No echoes at all. Likely causes: the sensor is on different pins than "
              f"trig={trig}/echo={echo}, a loose/wrong connector, or a dead sensor.")
        print("  * Find the pins:  robot/.venv/bin/python -m robot.diagnose --sonar-scan")
        print("  * Or set them:    PICRAWLER_ULTRASONIC_TRIG=Dx PICRAWLER_ULTRASONIC_ECHO=Dy "
              "bash robot/run.sh")
    elif 0 < valid < count and not sensor.simulate:
        print("Intermittent — try more pings (e.g. --pings 8) and check the connector "
              "is fully seated.")
    return 0


def _sonar_scan(pings: int, count: int) -> int:
    """Try common trig/echo pin pairs and report which one echoes."""
    from .sensors import DistanceSensor

    config.ULTRASONIC_PINGS = pings
    print("Scanning common trig/echo pin pairs (a working pair returns distances).")
    print("Point the sensor at a wall ~30-100cm away for a clear signal.\n")
    best: tuple[str, str, int, float | None] | None = None
    for trig, echo in _SCAN_PAIRS:
        try:
            sensor = DistanceSensor(trig=trig, echo=echo, simulate=config.SIMULATE)
        except Exception as exc:  # noqa: BLE001 - bad pin name / busy pin
            print(f"  trig={trig} echo={echo}: init failed ({exc})")
            continue
        if sensor.simulate:
            print("robot_hat is unavailable here — run this ON THE PI (real sensor needed).")
            return 1
        valid = 0
        last: float | None = None
        for _ in range(count):
            d = sensor.read_cm()
            if d is not None:
                valid += 1
                last = d
            time.sleep(0.05)
        tag = f"{last:.1f} cm" if last is not None else "-"
        print(f"  trig={trig} echo={echo}: {valid}/{count} valid   {tag}")
        if best is None or valid > best[2]:
            best = (trig, echo, valid, last)
    if best and best[2] > 0:
        print(f"\nBest pair: trig={best[0]} echo={best[1]} ({best[2]}/{count} valid).")
        print("Use it (defaults are D2/D3, so only set these if they differ):")
        print(f"  PICRAWLER_ULTRASONIC_TRIG={best[0]} PICRAWLER_ULTRASONIC_ECHO={best[1]} "
              "bash robot/run.sh")
    else:
        print("\nNo pair returned echoes. Check the sensor's connector and power, and "
              "that it's an HC-SR04-style trig/echo module (not an I2C/analog one).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolate a bad leg or a silent ultrasonic (Pi-local).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--leg", type=int, choices=range(LEG_COUNT), help="Move a single leg (0-3).")
    group.add_argument("--all", action="store_true", help="Move legs 0..3 in turn, pausing between.")
    group.add_argument("--sonar", type=int, nargs="?", const=10, metavar="N",
                       help="Read the ultrasonic N times (default 10) and print each value.")
    group.add_argument("--sonar-scan", action="store_true",
                       help="Try common trig/echo pin pairs to find where the sensor is wired.")
    parser.add_argument("--speed", type=int, default=config.STAND_SPEED,
                        help=f"Servo speed 1-100 for --leg/--all (default {config.STAND_SPEED}).")
    parser.add_argument("--pause", type=float, default=1.5, help="Seconds between legs in --all (default 1.5).")
    parser.add_argument("--pings", type=int, default=config.ULTRASONIC_PINGS,
                        help=f"Ping attempts per sonar read (default {config.ULTRASONIC_PINGS}).")
    parser.add_argument("--trig", default=config.ULTRASONIC_TRIG, help=f"Sonar trig pin (default {config.ULTRASONIC_TRIG}).")
    parser.add_argument("--echo", default=config.ULTRASONIC_ECHO, help=f"Sonar echo pin (default {config.ULTRASONIC_ECHO}).")
    args = parser.parse_args(argv)

    # --- Ultrasonic paths (no servo motion; safe to run anytime) ---
    if args.sonar_scan:
        return _sonar_scan(args.pings, count=6)
    if args.sonar is not None:
        return _sonar(args.trig, args.echo, args.pings, args.sonar)

    # --- Leg paths (move servos — mind the safety banner) ---
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
