"""Stage 1 one-shot test — run this from the Jetson to prove the movement link.

Exercises the whole protocol end-to-end across the network:
    get_status -> stand -> walk(1) -> sit
printing each CommandResponse.

Usage:
    python -m brain.test_movement                 # uses brain/config.py (picrawler.local)
    ROBOT_HOST=192.168.1.42 python -m brain.test_movement
    python -m brain.test_movement --base-url http://localhost:8000

SAFETY: on the first real run, elevate the robot / keep the legs clear in case
calibration needs a tweak.
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
    args = parser.parse_args(argv)

    client = RobotClient(base_url=args.base_url)
    print(f"Connecting to robot at {client.base_url} ...")

    try:
        health = client.health()
        print(f"  health       {health}")
        _show("get_status", client.get_status())
        _show("stand", client.stand())
        _show("walk", client.walk(steps=args.steps))
        _show("sit", client.sit())
    except httpx.HTTPError as exc:
        print(f"\nERROR: could not reach the robot: {exc}", file=sys.stderr)
        print("Check that the Pi server is running and ROBOT_HOST resolves.", file=sys.stderr)
        return 1
    finally:
        client.close()

    print("\nMovement link OK — the robot completed the full sequence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
