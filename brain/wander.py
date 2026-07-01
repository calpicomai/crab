"""Reactive wander + obstacle avoidance — autonomy v1 (no LLM).

The first time the robot moves on its own. A model-free control loop on the
Jetson that fuses two obstacle senses:

    * ultrasonic  — forward clearance (fast, but a narrow beam misses thin /
      off-axis things like a pole),
    * camera      — the perception server's detections; anything large (close)
      and roughly ahead counts as an obstacle and we steer away from its side.

Each cycle: if either sense says "blocked" -> turn away; else step forward. This
is the behavior-tree / reactive fallback from the roadmap and the same
``read sensors -> decide -> act`` seam the Ollama agent loop plugs into next. Each
step emits an *experience record* (senses + decision + response); ``--log FILE``
appends them as JSONL.

Run on the Jetson (uses brain/config.py, default robot picrawler.local:8000 and
perception http://localhost:8100):
    python -m brain.wander
    ROBOT_HOST=10.1.50.13 python -m brain.wander
    python -m brain.wander --no-camera            # ultrasonic only
    python -m brain.wander --max-steps 30 --min-cm 40

Camera avoidance needs the perception server running; if it's unreachable the
loop logs once and continues on ultrasonic alone. For arbitrary obstacles (a
pole), run perception with NanoOWL + obstacle prompts — YOLO only flags COCO.

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


def _camera_obstacle(http: httpx.Client, url: str, area_thresh: float, center_band: float):
    """Return (side, reachable): side is 'left'/'right'/None for the nearest
    in-path detection, reachable is False if the perception server didn't answer."""
    try:
        resp = http.get(url, timeout=1.0)
        resp.raise_for_status()
        snap = resp.json()
    except Exception:  # noqa: BLE001 - unreachable / bad response -> ultrasonic only
        return None, False
    width, height = snap.get("width") or 0, snap.get("height") or 0
    if not width or not height:
        return None, True
    frame_area = float(width * height)
    best_cx, best_area = None, area_thresh
    for det in snap.get("detections", []):
        x1, y1, x2, y2 = det["box"]
        area = ((x2 - x1) * (y2 - y1)) / frame_area
        cx = ((x1 + x2) / 2) / width
        if area >= best_area and abs(cx - 0.5) <= center_band:
            best_area, best_cx = area, cx
    if best_cx is None:
        return None, True
    return ("left" if best_cx < 0.5 else "right"), True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PiCrawler reactive wander + obstacle avoidance.")
    parser.add_argument("--base-url", default=None, help="Robot base URL (default: brain/config).")
    parser.add_argument("--max-steps", type=int, default=0, help="Stop after N steps (0 = until Ctrl+C).")
    parser.add_argument("--min-cm", type=float, default=config.WANDER_MIN_CM, help="Turn away below this clearance.")
    parser.add_argument("--turn-deg", type=float, default=config.WANDER_TURN_DEG, help="Turn amount when blocked.")
    parser.add_argument("--speed", type=int, default=config.WANDER_SPEED, help="Gait speed 1-100 for walk/turn.")
    parser.add_argument("--steps", type=int, default=config.WANDER_STEPS, help="Steps per clear decision.")
    parser.add_argument("--delay", type=float, default=config.WANDER_STEP_DELAY_S, help="Seconds between decisions.")
    parser.add_argument("--no-camera", action="store_true", help="Disable camera-assisted avoidance.")
    parser.add_argument("--perception-url", default=config.PERCEPTION_BASE_URL, help="Perception server base URL.")
    parser.add_argument("--log", default=None, help="Append per-step experience records as JSONL to this file.")
    args = parser.parse_args(argv)

    use_camera = config.WANDER_USE_CAMERA and not args.no_camera
    client = RobotClient(base_url=args.base_url)
    perc = httpx.Client(base_url=args.perception_url.rstrip("/")) if use_camera else None
    print(f"Wandering via {client.base_url}  (min_cm={args.min_cm}, turn_deg={args.turn_deg}, "
          f"speed={args.speed}, steps={args.steps}, camera={'on' if use_camera else 'off'})")
    print("Elevate the robot for the first run. Ctrl+C to stop.\n")

    log_fh = open(args.log, "a") if args.log else None
    turn_dirs = itertools.cycle([+1.0, -1.0])  # alternate turn side when the sense gives no direction
    camera_ok = use_camera
    current_dir = 0.0
    was_blocked = False
    step = 0

    try:
        client.stand()
        while args.max_steps == 0 or step < args.max_steps:
            step += 1
            status = client.get_status().status
            dist = status.distance_cm if status else None
            sonar_blocked = dist is not None and dist < args.min_cm

            cam_side = None
            if camera_ok:
                cam_side, reachable = _camera_obstacle(
                    perc, "/snapshot", config.WANDER_OBSTACLE_AREA, config.WANDER_CENTER_BAND
                )
                if not reachable:
                    print("  (perception unreachable — ultrasonic only)")
                    camera_ok = False

            blocked = sonar_blocked or (cam_side is not None)
            if blocked:
                # Steer away from the seen obstacle's side; else commit to one side per episode.
                if cam_side == "left":
                    current_dir = +1.0   # obstacle on the left -> turn right
                elif cam_side == "right":
                    current_dir = -1.0   # obstacle on the right -> turn left
                elif not was_blocked:
                    current_dir = next(turn_dirs)
                resp = client.turn(args.turn_deg * current_dir, speed=args.speed)
                why = f"cam:{cam_side}" if cam_side else f"sonar {dist:.0f}cm"
                action, detail = "turn", f"avoid ({why}): turn {args.turn_deg * current_dir:+.0f}deg"
            else:
                resp = client.walk(args.steps, speed=args.speed)
                shown = f"{dist:.0f}cm" if dist is not None else "no echo"
                action, detail = "walk", f"forward x{args.steps} (clear {shown})"
            was_blocked = blocked

            print(f"[{step:>3}] {detail:<40} -> ok={resp.ok} pose={_pose(resp)}")
            if log_fh:  # experience record — the seam the learning stack consumes
                log_fh.write(json.dumps({
                    "step": step, "distance_cm": dist, "camera_obstacle": cam_side,
                    "blocked": blocked, "action": resp.action.value, "ok": resp.ok,
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
        if perc:
            perc.close()
        client.close()

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
