"""Reactive wander + obstacle avoidance — autonomy v2 (costmap steering, no LLM).

The robot's autonomous fallback: a model-free control loop on the Jetson that
fuses its two obstacle senses into a **local occupancy costmap** and steers
toward the clearest gap.

    * ultrasonic  — accurate forward range (a narrow cone; misses thin/off-axis
      things like a pole on its own),
    * camera      — the perception server's detections give a bearing (and coarse
      range) for obstacles across the whole field of view.

Both write into ``brain/costmap.py:LocalCostmap`` (see its docs); each cycle we
ask it for a heading:

    * forward gap wide enough  -> walk,
    * gap off to a side        -> turn toward it (aim at free space),
    * boxed in / no gap        -> rotate-to-scan (sweep the fixed sonar) then re-pick.

This replaces autonomy v1's "sonar OR camera -> turn a fixed amount" (which
couldn't aim at a gap and let a pole slip through). It is the behavior-tree /
reactive fallback from the roadmap and the same ``read sensors -> model -> decide
-> act`` seam the LLM agent loop plugs into next (the agent decides; this is the
fallback). Each step emits an *experience record* (senses + costmap + decision +
response); ``--log FILE`` appends them as JSONL.

Run on the Jetson (uses brain/config.py, default robot picrawler.local:8000 and
perception http://localhost:8100):
    python -m brain.wander
    ROBOT_HOST=10.1.50.13 python -m brain.wander
    python -m brain.wander --no-camera            # ultrasonic only
    python -m brain.wander --max-steps 30

Camera avoidance needs the perception server running; if it's unreachable the
loop logs once and continues on ultrasonic alone. For arbitrary obstacles (a
pole), run perception with NanoOWL — on startup we push obstacle prompts
(config.COSTMAP_OBSTACLE_PROMPTS) so open-vocab detection flags them; YOLO alone
only reports its COCO classes.

SAFETY: elevate the robot / keep the area clear for the first run. Ctrl+C stops
it and sits the robot down.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from . import config
from .client import RobotClient
from .costmap import LocalCostmap


def _pose(resp) -> str:
    return resp.status.pose.value if resp.status else "?"


def _fetch_snapshot(http: httpx.Client) -> tuple[dict | None, bool]:
    """GET /snapshot. Returns (snapshot, reachable); reachable is False when the
    perception server didn't answer (caller falls back to ultrasonic only)."""
    try:
        resp = http.get("/snapshot", timeout=1.0)
        resp.raise_for_status()
        return resp.json(), True
    except Exception:  # noqa: BLE001 - unreachable / bad response -> ultrasonic only
        return None, False


def _push_obstacle_prompts(http: httpx.Client, prompts: list[str]) -> None:
    """If the perception server has NanoOWL loaded, steer it at obstacle prompts
    so the camera flags arbitrary obstacles (poles) YOLO's COCO classes miss.
    Best-effort: a YOLO-only or unreachable server is fine (graceful subset)."""
    if not prompts:
        return
    try:
        health = http.get("/health", timeout=1.0).json()
        if "nanoowl" not in (health.get("backends") or []):
            return  # no open-vocab backend -> nothing to steer
        http.post("/prompts", json={"prompts": prompts}, timeout=2.0).raise_for_status()
        print(f"  (perception: set NanoOWL obstacle prompts: {', '.join(prompts)})")
    except Exception:  # noqa: BLE001 - perception optional
        pass


def _read_distance(client: RobotClient) -> float | None:
    return (lambda s: s.distance_cm if s else None)(client.get_status().status)


def _rotate_to_scan(client: RobotClient, costmap: LocalCostmap, speed: int) -> None:
    """Sweep the fixed forward sonar by turning the body in increments, writing
    each reading into the costmap at the accumulated heading, then return to the
    start heading. Fills the off-center bins the fixed beam can't reach."""
    step = config.SCAN_STEP_DEG
    reach = config.SCAN_RANGE_DEG / 2.0
    # Sweep from -reach to +reach, tracking net rotation so we can undo it.
    turned = 0.0
    # Go to the left extreme first.
    client.turn(-reach, speed=speed)
    costmap.apply_motion(turn_deg=-reach)
    turned -= reach
    heading = -reach
    while heading <= reach + 1e-6:
        dist = _read_distance(client)
        costmap.integrate_sonar(dist)
        if heading + step <= reach + 1e-6:
            client.turn(step, speed=speed)
            costmap.apply_motion(turn_deg=step)
            turned += step
        heading += step
    # Return roughly to the original heading.
    client.turn(-turned, speed=speed)
    costmap.apply_motion(turn_deg=-turned)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PiCrawler reactive wander + costmap obstacle avoidance.")
    parser.add_argument("--base-url", default=None, help="Robot base URL (default: brain/config).")
    parser.add_argument("--max-steps", type=int, default=0, help="Stop after N steps (0 = until Ctrl+C).")
    parser.add_argument("--turn-deg", type=float, default=config.WANDER_TURN_DEG, help="Max turn per step toward a gap.")
    parser.add_argument("--speed", type=int, default=config.WANDER_SPEED, help="Gait speed 1-100 for walk/turn.")
    parser.add_argument("--steps", type=int, default=config.WANDER_STEPS, help="Steps per forward decision.")
    parser.add_argument("--delay", type=float, default=config.WANDER_STEP_DELAY_S, help="Seconds between decisions.")
    parser.add_argument("--no-camera", action="store_true", help="Disable camera-assisted avoidance.")
    parser.add_argument("--no-scan", action="store_true", help="Disable rotate-to-scan when boxed in.")
    parser.add_argument("--perception-url", default=config.PERCEPTION_BASE_URL, help="Perception server base URL.")
    parser.add_argument("--log", default=None, help="Append per-step experience records as JSONL to this file.")
    args = parser.parse_args(argv)

    use_camera = config.WANDER_USE_CAMERA and not args.no_camera
    client = RobotClient(base_url=args.base_url)
    perc = httpx.Client(base_url=args.perception_url.rstrip("/")) if use_camera else None
    costmap = LocalCostmap()
    print(f"Wandering via {client.base_url}  (turn_deg={args.turn_deg}, speed={args.speed}, "
          f"steps={args.steps}, camera={'on' if use_camera else 'off'}, "
          f"scan={'off' if args.no_scan else 'on'})")
    print(f"Costmap: {costmap.bins} bins over +/-{costmap.fov:.0f}deg, min_gap={costmap.min_gap:.0f}deg, "
          f"footprint={costmap.footprint:.0f}cm")
    print("Elevate the robot for the first run. Ctrl+C to stop.\n")

    log_fh = open(args.log, "a") if args.log else None
    camera_ok = use_camera
    if perc is not None:
        _push_obstacle_prompts(perc, config.COSTMAP_OBSTACLE_PROMPTS)
    step = 0

    try:
        client.stand()
        while args.max_steps == 0 or step < args.max_steps:
            step += 1
            costmap.decay()

            dist = _read_distance(client)
            costmap.integrate_sonar(dist)

            if camera_ok:
                snap, reachable = _fetch_snapshot(perc)
                if not reachable:
                    print("  (perception unreachable — ultrasonic only)")
                    camera_ok = False
                elif snap is not None:
                    costmap.integrate_camera(snap)

            heading, forward_clear = costmap.best_heading()

            # Periodic rotate-to-scan keeps the off-center bins fresh even when
            # forward looks clear (fixed sonar sees only straight ahead).
            do_scan = (
                not args.no_scan
                and (not forward_clear)
                and abs(heading) >= costmap.fov - 1e-6  # boxed in: no passable gap
            )
            if not args.no_scan and config.SCAN_EVERY and step % config.SCAN_EVERY == 0:
                do_scan = True

            if do_scan:
                _rotate_to_scan(client, costmap, args.speed)
                heading, forward_clear = costmap.best_heading()

            if forward_clear:
                resp = client.walk(args.steps, speed=args.speed)
                costmap.apply_motion(walked=True)
                shown = f"{dist:.0f}cm" if dist is not None else "no echo"
                action, detail = "walk", f"forward x{args.steps} (clear ahead, sonar {shown})"
            else:
                # Aim at the gap, but clamp the per-step turn so motion stays gentle.
                turn = max(-args.turn_deg, min(args.turn_deg, heading))
                resp = client.turn(turn, speed=args.speed)
                costmap.apply_motion(turn_deg=turn)
                action, detail = "turn", f"steer to gap: turn {turn:+.0f}deg (want {heading:+.0f})"

            print(f"[{step:>3}] {detail:<44} -> ok={resp.ok} pose={_pose(resp)}")
            print(f"      {costmap.render_ascii()}")
            if log_fh:  # experience record — the seam the learning stack consumes
                log_fh.write(json.dumps({
                    "step": step, "distance_cm": dist, "heading": heading,
                    "forward_clear": forward_clear, "scanned": do_scan,
                    "action": resp.action.value, "ok": resp.ok,
                    "costmap": costmap.render_ascii(),
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
