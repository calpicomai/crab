"""Agent loop — the free-roaming multimodal brain.

Each tick: pull a camera frame + status from the robot, ask the LLM (a local VLM
by default) for ONE narrated high-level action, execute it via RobotClient, and
repeat. The robot's fast reflex + the costmap remain the real-time safety layer
underneath; if the LLM is unreachable or errors, the tick falls back to a single
reactive costmap step so the robot still behaves safely.

There is no voice input yet, so the agent free-roams and narrates by default; pass
``--goal "explore the kitchen"`` to steer it. Narration is printed (TTS is a later
stage). Each tick emits an experience record (``--log`` -> JSONL) — the same
learning seam the wander loop uses.

Run on the Jetson (needs a llama-server with a VLM; see brain/setup_agent.sh):
    python -m brain.agent.loop                       # free-roam
    python -m brain.agent.loop --goal "find a person"
    python -m brain.agent.loop --sim --max-ticks 5   # canned policy, off-GPU

SAFETY: elevate the robot / keep the area clear for the first run. Ctrl+C stops
and sits the robot.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time

import httpx

from shared import CAMERA_FRAME_PATH

from .. import config as brain_config
from ..client import RobotClient
from ..costmap import LocalCostmap
from . import config, tools
from .llm import build_brain


def _grab_frame_b64(url: str) -> str | None:
    """Fetch the robot's latest camera JPEG as base64, or None if unavailable."""
    try:
        resp = httpx.get(url, timeout=3.0)
        resp.raise_for_status()
        return base64.b64encode(resp.content).decode("ascii")
    except Exception:  # noqa: BLE001 - no camera / unreachable -> text-only decision
        return None


def _free_perception_ram(perception_url: str) -> None:
    """Unload the detectors so the VLM has RAM (the VLM does the seeing now)."""
    try:
        client = httpx.Client(base_url=perception_url.rstrip("/"), timeout=5.0)
        for backend in ("yolo", "nanoowl"):
            try:
                client.post("/unload", json={"backend": backend})
            except Exception:  # noqa: BLE001
                pass
        client.close()
        print("  (freed perception RAM: unloaded yolo/nanoowl for the VLM)")
    except Exception:  # noqa: BLE001 - perception optional
        pass


def _status_dict(client: RobotClient) -> dict:
    st = client.get_status().status
    if st is None:
        return {"pose": "?", "distance_cm": None, "reflex_stopped": False}
    return {"pose": st.pose.value, "distance_cm": st.distance_cm, "reflex_stopped": st.reflex_stopped}


def _reactive_fallback(client: RobotClient, costmap: LocalCostmap, status: dict) -> tuple[str, dict, object]:
    """One safe reactive step when the LLM can't decide: steer by the costmap
    (sonar only — detectors are unloaded in agent mode) + the Pi reflex."""
    costmap.decay()
    costmap.integrate_sonar(status.get("distance_cm"))
    heading, forward_clear = costmap.best_heading()
    if forward_clear:
        resp = client.walk(1, min_clearance_cm=config.AGENT_REFLEX_CM)
        costmap.apply_motion(walked=True)
        return "walk", {"steps": 1}, resp
    turn = max(-brain_config.WANDER_TURN_DEG, min(brain_config.WANDER_TURN_DEG, heading))
    resp = client.turn(turn)
    costmap.apply_motion(turn_deg=turn)
    return "turn", {"degrees": turn}, resp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PiCrawler multimodal LLM agent (free-roam + narrate).")
    parser.add_argument("--goal", default=None, help="Optional natural-language goal to pursue.")
    parser.add_argument("--base-url", default=None, help="Robot base URL (default: brain/config).")
    parser.add_argument("--perception-url", default=brain_config.PERCEPTION_BASE_URL, help="Perception base URL.")
    parser.add_argument("--max-ticks", type=int, default=0, help="Stop after N ticks (0 = until Ctrl+C).")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    parser.add_argument("--tick", type=float, default=config.AGENT_TICK_S, help="Seconds between decisions.")
    parser.add_argument("--sim", action="store_true", help="Use the canned policy (no LLM) — off-GPU testing.")
    parser.add_argument("--keep-perception", action="store_true", help="Don't unload the detectors on startup.")
    parser.add_argument("--log", default=None, help="Append per-tick experience records as JSONL to this file.")
    args = parser.parse_args(argv)

    simulate = args.sim or config.AGENT_SIMULATE
    max_ticks = 1 if args.once else args.max_ticks

    client = RobotClient(base_url=args.base_url)
    # Pull frames from the SAME robot we command (honors --base-url), not the
    # static config default.
    frame_url = client.base_url.rstrip("/") + CAMERA_FRAME_PATH
    brain = build_brain(simulate)
    costmap = LocalCostmap()  # fallback reactive layer
    log_fh = open(args.log, "a") if args.log else None

    print(f"Agent brain via {config.LLM_BASE_URL} (model={config.LLM_MODEL}, "
          f"multimodal={'on' if config.LLM_MULTIMODAL else 'off'}, "
          f"{'CANNED POLICY' if simulate else 'LLM'}); robot {client.base_url}")
    print(f"Goal: {args.goal or 'free exploration'}.  Elevate the robot for the first run. Ctrl+C to stop.\n")

    if config.AGENT_FREE_PERCEPTION_RAM and not args.keep_perception and not simulate:
        _free_perception_ram(args.perception_url)

    tick = 0
    last_action: str | None = None
    try:
        client.stand()
        while max_ticks == 0 or tick < max_ticks:
            tick += 1
            status = _status_dict(client)
            image_b64 = _grab_frame_b64(frame_url)
            context = _context(status, args.goal, last_action)

            fell_back = False
            try:
                decision = brain.decide(image_b64, context, args.goal, status)
                resp = tools.dispatch(decision.tool_name, decision.tool_args, client)
                say, action, tool_args = decision.say, decision.tool_name, decision.tool_args
            except Exception as exc:  # noqa: BLE001 - LLM unreachable/error -> reactive safety step
                fell_back = True
                action, tool_args, resp = _reactive_fallback(client, costmap, status)
                say = f"(LLM unavailable: {exc}) reactive {action}"

            pose = resp.status.pose.value if resp and resp.status else "?"
            reflex = bool(resp and resp.status and resp.status.reflex_stopped)
            flag = " [fallback]" if fell_back else (" [REFLEX-STOP]" if reflex else "")
            print(f"[{tick:>3}] 🤖 {say}{flag}")
            print(f"      → {action}({tool_args}) -> ok={resp.ok} pose={pose}")
            last_action = f"{action}({tool_args})"

            if log_fh:  # experience record — same seam the learning stack consumes
                log_fh.write(json.dumps({
                    "tick": tick, "goal": args.goal, "have_image": image_b64 is not None,
                    "pose": status["pose"], "distance_cm": status["distance_cm"],
                    "say": say, "action": action, "args": tool_args,
                    "fell_back": fell_back, "reflex_stopped": reflex, "ok": resp.ok,
                }) + "\n")
                log_fh.flush()
            time.sleep(args.tick)
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


# Kept as a module function so the (potentially long) prompt context assembly is
# testable and out of the hot loop body.
def _context(status: dict, goal: str | None, last_action: str | None) -> str:
    from .llm import _context_line

    return _context_line(status, goal, last_action)


if __name__ == "__main__":
    raise SystemExit(main())
