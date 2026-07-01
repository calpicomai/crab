"""Reactive wander + ultrasonic obstacle avoidance — autonomy v1 (no LLM).

The first time the robot moves on its own. A simple, always-on control loop on
the Jetson: read the forward clearance from the robot's ultrasonic sensor (via
get_status), and

    * if something is within WANDER_MIN_CM  -> turn away (rotate until clear),
    * otherwise                             -> take one step forward.

This is the behavior-tree / reactive fallback from the roadmap — safe and
model-free, and the same ``read sensors -> decide -> act`` seam the Ollama agent
loop plugs into next. Each step emits an *experience record* (distance +
decision + response), which is what the future learning stack consumes; pass
``--log FILE`` to append them as JSONL.

Run on the Jetson (uses brain/config.py, default target picrawler.local:8000):
    python -m brain.wander
    python -m brain.wander --max-steps 30 --min-cm 25 --turn-deg 45
    ROBOT_HOST=10.1.50.13 python -m brain.wander

SAFETY: elevate the robot / keep the area clear for the first run. Ctrl+C stops
it and sits the robot down.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time

import httpx

from . import config
from .client import RobotClient


def _pose(resp) -> str:
    return resp.status.pose.value if resp.status else "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PiCrawler reactive wander + obstacle avoidance.")
    parser.add_argument("--base-url", default=None, help="Robot base URL (default: brain/config).")
    parser.add_argument("--max-steps", type=int, default=0, help="Stop after N steps (0 = until Ctrl+C).")
    parser.add_argument("--min-cm", type=float, default=config.WANDER_MIN_CM, help="Turn away below this clearance.")
    parser.add_argument("--turn-deg", type=float, default=config.WANDER_TURN_DEG, help="Turn amount when blocked.")
    parser.add_argument("--speed", type=int, default=config.WANDER_SPEED, help="Gait speed 1-100 for walk/turn.")
    parser.add_argument("--steps", type=int, default=config.WANDER_STEPS, help="Steps per clear decision.")
    parser.add_argument("--delay", type=float, default=config.WANDER_STEP_DELAY_S, help="Seconds between decisions.")
    parser.add_argument("--log", default=None, help="Append per-step experience records as JSONL to this file.")
    args = parser.parse_args(argv)

    client = RobotClient(base_url=args.base_url)
    print(f"Wandering via {client.base_url}  (min_cm={args.min_cm}, turn_deg={args.turn_deg}, "
          f"speed={args.speed}, steps={args.steps})")
    print("Elevate the robot for the first run. Ctrl+C to stop.\n")

    log_fh = open(args.log, "a") if args.log else None
    turn_dirs = itertools.cycle([+1.0, -1.0])  # alternate which way we turn per obstacle
    current_dir = 0.0
    was_blocked = False
    step = 0

    try:
        client.stand()
        while args.max_steps == 0 or step < args.max_steps:
            step += 1
            status = client.get_status().status
            dist = status.distance_cm if status else None
            blocked = dist is not None and dist < args.min_cm

            if blocked:
                if not was_blocked:  # new obstacle: pick a side and commit to it until clear
                    current_dir = next(turn_dirs)
                resp = client.turn(args.turn_deg * current_dir, speed=args.speed)
                action, detail = "turn", f"avoid: turn {args.turn_deg * current_dir:+.0f}deg (clear {dist:.0f}cm)"
            else:
                resp = client.walk(args.steps, speed=args.speed)
                shown = f"{dist:.0f}cm" if dist is not None else "no echo"
                action, detail = "walk", f"forward x{args.steps} (clear {shown})"
            was_blocked = blocked

            print(f"[{step:>3}] {detail:<34} -> ok={resp.ok} pose={_pose(resp)}")
            if log_fh:  # experience record — the seam the learning stack consumes
                log_fh.write(json.dumps({
                    "step": step, "distance_cm": dist, "blocked": blocked,
                    "action": resp.action.value, "detail": resp.detail, "ok": resp.ok,
                }) + "\n")
                log_fh.flush()
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nStopping.")
    except httpx.HTTPError as exc:
        print(f"\nERROR reaching the robot: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            client.sit()
        except Exception:  # noqa: BLE001 - best-effort settle on exit
            pass
        if log_fh:
            log_fh.close()
        client.close()

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
