"""Bounded movement acceptance test; simulation is the development default.

Exercises the whole protocol end-to-end across the network:
    get_status -> stand -> walk(1) -> sit
printing each CommandResponse.

Usage:
    python -m brain.test_movement                 # uses brain/config.py (picrawler.local)
    ROBOT_HOST=192.168.1.42 python -m brain.test_movement
    python -m brain.test_movement --base-url http://localhost:8000

Real motion requires --allow-hardware and separate Pi actuator enablement.
Use that option only after explicit approval of the hardware acceptance gate.
This checks command results, not physical displacement or collision avoidance.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from shared import CommandResponse

from .client import RobotClient


def _show(label: str, resp: CommandResponse) -> None:
    status = resp.status
    pose = status.pose.value if status else "?"
    sim = " [SIMULATE]" if (status and status.simulate) else ""
    print(f"  {label:<12} ok={resp.ok} pose={pose}{sim} :: {resp.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PiCrawler Stage 1 movement test.")
    parser.add_argument("--base-url", default=None, help="Override robot base URL.")
    parser.add_argument("--steps", type=int, default=1, help="Steps to walk (default 1).")
    parser.add_argument("--allow-hardware", action="store_true",
                        help="Permit real movement ONLY after hardware-test approval.")
    args = parser.parse_args(argv)

    client = RobotClient(base_url=args.base_url)
    print(f"Connecting to robot at {client.base_url} ...")

    try:
        health = client.health()
        print(f"  health       simulate={health.get('simulate')} "
              f"hardware_verified={health.get('hardware_verified', False)} "
              f"motion={health.get('motion', {})}")
        status = client.get_status()
        _show("get_status", status)
        if not status.ok or status.status is None:
            print("ERROR: robot status unavailable; no motion sent.", file=sys.stderr)
            return 1
        if not status.status.simulate and not args.allow_hardware:
            print("Hardware actuator test disabled; read-only preflight complete.")
            print("Use the staged acceptance checklist before --allow-hardware.")
            return 2
        for label, action in (("stand", client.stand),
                              ("walk", lambda: client.walk(steps=args.steps)),
                              ("turn_steps", lambda: client.turn_steps("right", cycles=1)),
                              ("sit", client.sit)):
            result = action()
            _show(label, result)
            if not result.ok:
                print(f"ERROR: {label} was blocked or failed; sequence incomplete.", file=sys.stderr)
                return 1
        precise = client.turn(45)
        _show("precise turn", precise)
        if precise.ok:
            print("ERROR: stock hardware cannot promise a precise turn.", file=sys.stderr)
            return 1
    except httpx.HTTPError as exc:
        print(f"\nERROR: could not reach the robot: {exc}", file=sys.stderr)
        print("Check that the Pi server is running and ROBOT_HOST resolves.", file=sys.stderr)
        return 1
    finally:
        client.close()

    print("\nMovement command results passed; physical execution remains unverified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
