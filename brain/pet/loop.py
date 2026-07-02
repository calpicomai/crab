"""The robot pet — run with ``python -m brain.pet``.

Two layers at their own pace so the pet always moves yet stays smart:

* **Body** (main thread, fast): a continuous reactive control loop — costmap +
  Pi reflex — that always moves and avoids, with steering *hysteresis* so it no
  longer stops and pans side to side. It's the only thing that commands motion.
* **Mind** (background thread, slow): looks through the camera every few seconds,
  reacts *in character* (its own evolving personality + mood + memory), sets a
  gentle heading bias / gesture for the body, remembers what happened, and
  periodically re-condenses who it's becoming.

With no llama-server the mind uses a canned in-character voice, so the pet still
feels alive on mood + memory alone; when a VLM is up it becomes its real inner
voice. Narration is printed (a real voice via Piper TTS is the next stage).

    python -m brain.pet --name Nibbles
    python -m brain.pet --sim --duration 30      # off-GPU, canned voice
    python -m brain.pet --no-llm                 # pure reactive + mood + memory
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import queue
import random
import sys
import threading
import time

import httpx

from shared import AUDIO_PLAY_PATH, AUDIO_STREAM_PATH, CAMERA_FRAME_PATH

from .. import config as brain_config
from ..client import RobotClient
from ..costmap import LocalCostmap
from ..dashboard import Dashboard
from ..hearing import build_ears
from . import commands
from . import config as pet_config
from . import expressions
from .brain import build_pet_brain
from .identity import PetIdentity
from .memory import MemoryStore
from .mood import Mood
from .voice import Voice

# Short canned exclamations the body "says" on a mood change when there's no LLM
# mind narrating — so a voice-enabled pet still barks/whines on its own.
_BARKS = {
    "excited": "woof woof! a friend!",
    "playful": "let's play!",
    "curious": "hm? what's that over there?",
    "cautious": "ooh, careful now...",
    "startled": "yipe!",
    "bored": "*whine* ...anything happening?",
    "sleepy": "*yawn*",
}


class _Shared:
    """Thread-shared state between the body (reader) and mind (writer)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thought = None          # latest PetThought from the mind
        self.thought_id = 0
        self.status = {"pose": "?", "distance_cm": None, "reflex_stopped": False}
        self.mood_name = "curious"   # body publishes; mind reads for its prompt
        self.heard: str | None = None  # last spoken utterance; mind reads + clears
        self.stop = False


def _status_dict(client: RobotClient) -> dict:
    st = client.get_status().status
    if st is None:
        return {"pose": "?", "distance_cm": None, "reflex_stopped": False}
    return {"pose": st.pose.value, "distance_cm": st.distance_cm, "reflex_stopped": st.reflex_stopped}


def _probe_perception(perc: "httpx.Client | None") -> bool:
    """Print one line about the perception server and return whether its camera
    will actually feed the costmap. A ``dummy``/``simulate`` backend emits fake
    boxes we deliberately don't fuse (see costmap.integrate_camera), so say so —
    the pet then navigates on sonar + reflex, which is correct, not broken."""
    if perc is None:
        print("   perception: off (--no-camera) — navigating on sonar + reflex.")
        return False
    try:
        health = perc.get("/health", timeout=2.0).json() or {}
    except Exception:  # noqa: BLE001 - perception optional
        print("   perception: unreachable — navigating on sonar + reflex.")
        return False
    backends = health.get("backends") or []
    real = [b for b in backends if b != "dummy"]
    if health.get("simulate") or not real:
        print(f"   perception: {backends or 'none'} (fake/simulate) — camera NOT "
              "fused; navigating on sonar + reflex.")
        return False
    print(f"   perception: {', '.join(real)} — camera fused into the costmap.")
    return True


def _grab_frame_b64(url: str) -> str | None:
    try:
        r = httpx.get(url, timeout=3.0)
        r.raise_for_status()
        return base64.b64encode(r.content).decode("ascii")
    except Exception:  # noqa: BLE001 - no camera -> text-only reflection
        return None


def _mind_thread(shared: _Shared, brain, identity: PetIdentity, memory: MemoryStore,
                 frame_url: str, reflect_s: float, evolve_every: int, voice: Voice) -> None:
    """Slow loop: look, react in character, speak, remember, and grow."""
    reflections = 0
    while not shared.stop:
        with shared.lock:
            status = dict(shared.status)
            mood_name = shared.mood_name
            status["heard"] = shared.heard   # let the mind react to speech in character
            shared.heard = None              # consume it
        image = _grab_frame_b64(frame_url)
        try:
            thought = brain.reflect(image, status, identity, mood_name, memory.summary())
        except Exception as exc:  # noqa: BLE001 - LLM hiccup -> stay quiet this beat
            thought = None
            print(f"  ({identity.name}'s mind wandered: {exc})", file=sys.stderr)
        if thought is not None:
            with shared.lock:
                shared.thought = thought
                shared.thought_id += 1
            print(f"🐾 {thought.say}   [{mood_name}]")
            voice.say(thought.say)
            if thought.observation:
                identity.note_seen([thought.observation])
                memory.remember(mood=mood_name, pose=status.get("pose"),
                                distance_cm=status.get("distance_cm"),
                                observation=thought.observation, note=thought.say)
            reflections += 1
            if reflections % evolve_every == 0:
                try:
                    identity.evolve(brain.evolve(identity, memory.summary()))
                    print(f"   … {identity.name} feels a little more like itself: {identity.character}")
                except Exception:  # noqa: BLE001
                    pass
        # Sleep in small slices so --duration / Ctrl+C stop promptly.
        slept = 0.0
        while slept < reflect_s and not shared.stop:
            time.sleep(0.1)
            slept += 0.1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A robot pet that roams, feels, remembers, and grows.")
    parser.add_argument("--name", default=pet_config.PET_NAME, help="Name a new pet (existing keeps its own).")
    parser.add_argument("--base-url", default=None, help="Robot base URL (default: brain/config).")
    parser.add_argument("--perception-url", default=brain_config.PERCEPTION_BASE_URL, help="Perception URL.")
    parser.add_argument("--duration", type=float, default=0.0, help="Stop after N seconds (0 = until Ctrl+C).")
    parser.add_argument("--max-ticks", type=int, default=0, help="Stop after N body cycles (0 = unlimited).")
    parser.add_argument("--sim", action="store_true", help="Canned inner voice (no LLM/GPU).")
    parser.add_argument("--no-llm", action="store_true", help="No mind at all — pure reactive + mood + memory.")
    parser.add_argument("--no-camera", action="store_true", help="Don't fuse the perception camera into the costmap.")
    parser.add_argument("--no-emote", action="store_true", help="Disable dog-like expressive gestures.")
    parser.add_argument("--dashboard", action="store_true", help="Push telemetry to the robot's /sim dashboard.")
    parser.add_argument("--voice", action="store_true", help="Speak aloud via Piper TTS (needs piper + a model).")
    parser.add_argument("--no-voice", action="store_true", help="Force voice off.")
    parser.add_argument("--no-stt", action="store_true", help="Disable spoken-command listening (Whisper).")
    parser.add_argument("--memory-db", default=pet_config.PET_MEMORY_DB, help="Episodic memory SQLite path.")
    parser.add_argument("--identity-file", default=pet_config.PET_IDENTITY_FILE, help="Identity JSON path.")
    parser.add_argument("--log", default=None, help="Append per-cycle experience records as JSONL.")
    args = parser.parse_args(argv)

    simulate = args.sim
    client = RobotClient(base_url=args.base_url)
    frame_url = client.base_url.rstrip("/") + CAMERA_FRAME_PATH
    identity = PetIdentity(args.identity_file, name=args.name)
    memory = MemoryStore(args.memory_db)
    costmap = LocalCostmap()
    mood = Mood()
    shared = _Shared()
    rng = random.Random()
    use_camera = not args.no_camera
    emote = pet_config.PET_EMOTE and not args.no_emote
    voice = Voice(
        enabled=(pet_config.PET_VOICE or args.voice) and not args.no_voice,
        model=pet_config.PET_VOICE_MODEL,
        player=pet_config.PET_VOICE_PLAYER,
        sink=pet_config.PET_AUDIO_SINK,
        play_url=client.base_url.rstrip("/") + AUDIO_PLAY_PATH,
    )
    perc = httpx.Client(base_url=args.perception_url.rstrip("/")) if use_camera else None
    log_fh = open(args.log, "a") if args.log else None
    dash = Dashboard(client.base_url, args.dashboard)
    reflex_cm = pet_config.PET_REFLEX_CM
    turn_cap = brain_config.WANDER_TURN_DEG

    # Spoken commands: the Pi's mic streams here; Whisper (on the Jetson) turns it
    # into text on the ears' own thread, which we queue and drain on the body
    # thread (so only the body ever commands motion).
    heard_q: "queue.Queue[str]" = queue.Queue()
    ears = None
    if pet_config.PET_STT and not args.no_stt:
        ears = build_ears(
            client.base_url.rstrip("/") + AUDIO_STREAM_PATH,
            pet_config.WHISPER_MODEL, heard_q.put,
            wake_word=pet_config.PET_WAKE_WORD or None,
            device=pet_config.WHISPER_DEVICE, compute_type=pet_config.WHISPER_COMPUTE,
        )

    print(f"🐾 Meet {identity.name} — {identity.age_str()}, {', '.join(identity.seed_traits) or 'a mystery'}.")
    print(f"   {identity.character}")
    print(f"   (robot {client.base_url}; mind: {'canned' if simulate else ('off' if args.no_llm else 'LLM')}; "
          f"memories so far: {memory.count()})  Elevate the robot for the first run. Ctrl+C to stop.")
    camera_fused = _probe_perception(perc if use_camera else None)
    print()

    mind = None
    if not args.no_llm:
        brain = build_pet_brain(simulate)
        mind = threading.Thread(
            target=_mind_thread,
            args=(shared, brain, identity, memory, frame_url, pet_config.PET_REFLECT_S,
                  pet_config.PET_EVOLVE_EVERY, voice),
            daemon=True,
        )

    commit_dir = 0.0
    commit_ttl = 0
    turn_only_streak = 0
    last_thought_id = 0
    prev_mood = mood.current
    last_heard: str | None = None
    tick = 0
    started = time.monotonic()
    try:
        client.stand()
        if mind is not None:
            mind.start()
        if ears is not None:
            ears.start()
        while not shared.stop:
            if args.max_ticks and tick >= args.max_ticks:
                break
            if args.duration and (time.monotonic() - started) >= args.duration:
                break
            tick += 1

            # Spoken commands (drained here so motion stays on the body thread).
            obeyed = False
            while True:
                try:
                    text = heard_q.get_nowait()
                except queue.Empty:
                    break
                last_heard = text
                with shared.lock:
                    shared.heard = text          # the mind reacts to it in character
                cmd = commands.interpret(text)
                print(f"🗣  heard: {text!r}" + (f" -> {cmd.name}" if cmd else " (free-form)"))
                if cmd is None:
                    continue
                voice.say(cmd.reply)
                if cmd.mood:
                    mood.nudge(cmd.mood)
                if cmd.gesture:
                    expressions.express(cmd.gesture, client, speed=mood.params().speed, reflex_cm=reflex_cm)
                if cmd.pose == "sit":
                    client.sit(); obeyed = True
                elif cmd.pose == "stand":
                    client.stand()
            if obeyed:
                # Obeyed a sit/stay — hold this beat instead of wandering off.
                time.sleep(0.5)
                continue

            status = _status_dict(client)
            dist = status["distance_cm"]
            with shared.lock:
                shared.status = status
                shared.mood_name = mood.current
                thought = shared.thought
                tid = shared.thought_id

            costmap.decay()
            costmap.integrate_sonar(dist)
            if use_camera and perc is not None:
                try:
                    snap = perc.get("/snapshot", timeout=1.0)
                    if snap.status_code == 200:
                        costmap.integrate_camera(snap.json())
                except Exception:  # noqa: BLE001 - perception optional
                    pass
            heading, forward_clear = costmap.best_heading()

            # The mind's gentle influence: a heading bias + mood hint + a gesture.
            new_thought = thought is not None and tid != last_thought_id
            bias = thought.heading_bias_deg if thought else 0.0
            saw_person = bool(thought and "person" in (thought.observation or "").lower())
            # A fresh in-character observation counts as novelty — keeps a lively
            # dog engaged instead of getting bored between the slow reflections.
            saw_new = bool(new_thought and thought and thought.observation)
            if new_thought:
                mood.nudge(thought.mood_hint)
                last_thought_id = tid
            target = max(-costmap.fov, min(costmap.fov, heading + bias))

            want_walk = forward_clear and abs(target) < turn_cap
            # Anti-spin: if we've only been turning for too long (boxed in, or a
            # mis-sensing forward sonar), probe forward once anyway — the Pi reflex
            # aborts the stride if it's truly blocked, so this can't ram anything.
            antispin_probe = (
                not want_walk
                and pet_config.PET_ANTISPIN_TICKS
                and turn_only_streak >= pet_config.PET_ANTISPIN_TICKS
            )

            if want_walk or antispin_probe:
                m = mood.update(moved_forward=not antispin_probe, saw_person=saw_person, saw_new=saw_new)
                resp = client.walk(1, speed=mood.params().speed, min_clearance_cm=reflex_cm)
                reflex = bool(resp.status and resp.status.reflex_stopped)
                if reflex:
                    m = mood.update(reflex=True)
                    d = resp.status.distance_cm if resp.status else None
                    costmap.integrate_sonar(min(d, reflex_cm) if d is not None else reflex_cm)
                else:
                    costmap.apply_motion(walked=True)
                commit_ttl = 0
                turn_only_streak = 0
                if antispin_probe and reflex:
                    # A real wall really is ahead — explore the other way next.
                    commit_dir = -commit_dir if commit_dir else 1.0
                    commit_ttl = pet_config.PET_HYSTERESIS_TICKS
                action = "reflex-stop" if reflex else ("probe" if antispin_probe else "walk")
            else:
                # Turn toward the gap, committing to a side (hysteresis) to avoid rocking.
                if commit_ttl > 0 and commit_dir != 0.0:
                    direction = commit_dir
                else:
                    direction = math.copysign(1.0, target) if abs(target) > 1e-6 else 1.0
                    commit_dir = direction
                    commit_ttl = pet_config.PET_HYSTERESIS_TICKS
                turn = direction * min(turn_cap, max(15.0, abs(target) or turn_cap))
                m = mood.update(blocked=True, saw_person=saw_person, saw_new=saw_new)
                resp = client.turn(turn, speed=mood.params().speed)
                costmap.apply_motion(turn_deg=turn)
                commit_ttl -= 1
                turn_only_streak += 1
                action = f"turn {turn:+.0f}"

            # Emote like a dog: a big signature move the instant the mood changes,
            # a smaller fidget sprinkled in otherwise — so it's never just walking.
            gesture = "none"
            mood_changed = m != prev_mood
            if emote:
                if new_thought and thought and thought.gesture in expressions.GESTURES \
                        and thought.gesture != "none":
                    gesture = thought.gesture           # the mind asked for a specific move
                elif mood_changed:
                    gesture = expressions.signature(m, rng)   # react to the new feeling
                elif forward_clear and rng.random() < pet_config.PET_EMOTE_CHANCE:
                    idle = expressions.idle(m, rng)
                    gesture = idle if expressions.is_inplace(idle) else "none"  # don't drift while idling
                if gesture != "none":
                    expressions.express(gesture, client, speed=mood.params().speed, reflex_cm=reflex_cm)

            # A little bark/whine on a mood change when no LLM mind is narrating.
            if mood_changed and mind is None and m in _BARKS:
                print(f"🐾 {identity.name}: {_BARKS[m]}   [{m}]")
                voice.say(_BARKS[m])
            prev_mood = m

            emote_s = f" {gesture}" if gesture != "none" else ""
            dist_s = f"{dist:.0f}cm" if dist is not None else "--"
            print(f"[{tick:>3}] {action:<12}{emote_s:<9} mood={m:<9} "
                  f"clear={'y' if forward_clear else 'n'} head={heading:+.0f} "
                  f"bias={bias:+.0f} dist={dist_s} cam={'y' if camera_fused else 'n'} "
                  f"-> pose={resp.status.pose.value if resp.status else '?'}")
            if log_fh:
                log_fh.write(json.dumps({
                    "tick": tick, "action": action, "gesture": gesture, "mood": m,
                    "distance_cm": dist, "heading": heading, "bias": bias,
                    "forward_clear": forward_clear, "camera_fused": camera_fused,
                    "say": thought.say if (new_thought and thought) else None,
                }) + "\n")
                log_fh.flush()
            if dash.enabled:
                dash.push({
                    "name": identity.name, "mode": "canned" if simulate else "llm",
                    "mood": m, "gesture": gesture, "character": identity.character,
                    "memory": memory.count(), "tick": tick, "action": action,
                    "heading": heading, "forward_clear": forward_clear,
                    "distance_cm": dist, "camera_fused": camera_fused,
                    "reflex": bool(resp.status and resp.status.reflex_stopped),
                    "say": thought.say if thought else None,
                    "heard": last_heard,
                    "costmap": costmap.snapshot(),
                })
    except KeyboardInterrupt:
        print(f"\n{identity.name} is tuckered out. Stopping.")
    except httpx.HTTPError as exc:
        print(f"\nERROR reaching the robot: {exc}", file=sys.stderr)
        shared.stop = True
        return 1
    finally:
        shared.stop = True
        if ears is not None:
            ears.stop()
        if mind is not None:
            mind.join(timeout=2.0)
        try:
            client.sit()
        except Exception:  # noqa: BLE001
            pass
        # Capture the farewell stats before closing the DB (the mind thread has
        # joined, so no more writes land after this).
        farewell = f"{identity.name} curls up. ({memory.count()} memories, {identity.reflections} reflections.)"
        if log_fh:
            log_fh.close()
        if perc is not None:
            perc.close()
        memory.close()
        client.close()

    print(farewell)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
