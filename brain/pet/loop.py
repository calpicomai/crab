"""Stationary companion runtime: one coordinator, optional local mind and ears.

Sensor/network/model workers never issue movement. The coordinator owns every
body intention; a separate control connection can stop an in-flight primitive.
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import threading
import time

from shared import AUDIO_PLAY_PATH, AUDIO_STOP_PATH, AUDIO_STREAM_PATH
from shared.clock import event_age
from .. import config as brain_config
from ..client import RobotClient
from ..dashboard import Dashboard
from ..hearing import build_ears
from ..realtime import RealtimeClient
from . import commands, config
from .brain import build_pet_brain
from .coordinator import Coordinator
from .identity import PetIdentity
from .inputs import InputWorker
from .memory import MemoryStore
from .voice import Voice


def _latest(q: queue.Queue, item) -> None:
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(item)


class EventInbox:
    """A recognized stop cannot be evicted by a burst of ordinary engagement."""
    def __init__(self):
        self.normal = queue.Queue(maxsize=32)
        self.urgent = None
        self.stop_after = float("-inf")
        self.control_epoch = -1
        self.lock = threading.Lock()

    def put_nowait(self, event):
        command = commands.interpret(event.get("text", ""))
        stamp = event.get("timestamp")
        valid_stamp = isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp)
        if event.get("_clock") is not None:
            age = event_age(event)
            valid_stamp = age is not None
            stamp = time.time()-age if valid_stamp else None
        with self.lock:
            epoch = event.get("control_epoch")
            if type(epoch) is int and epoch >= 0:
                if epoch < self.control_epoch:
                    return
                if epoch > self.control_epoch:
                    self._discard_before_epoch_locked(epoch)
            if command and command.name in {"stop", "stay", "scold"}:
                # The conservative mapped capture time is the EARLIEST possible
                # local time. A stop barrier must fence the whole uncertainty
                # interval, including buffered local microphone resumes.
                barrier = time.time() if event.get("_clock") is not None or not valid_stamp else stamp
                self.stop_after = max(self.stop_after, barrier)
                self.urgent = event
                # Old resume/social input must not undo a newer stop.
                while not self.normal.empty():
                    self.normal.get_nowait()
                return
            if not valid_stamp or stamp <= self.stop_after:
                return  # a delayed pre-stop resume cannot become new engagement
            try:
                self.normal.put_nowait(event)
            except queue.Full:
                self.normal.get_nowait()
                self.normal.put_nowait(event)

    def get_nowait(self):
        with self.lock:
            if self.urgent is not None:
                event, self.urgent = self.urgent, None
                return event
        return self.normal.get_nowait()

    def discard_pending(self):
        """Discard old resume/social input at a transport-loss boundary."""
        with self.lock:
            self.stop_after = max(self.stop_after, time.time())
            while not self.normal.empty():
                self.normal.get_nowait()

    def _discard_before_epoch_locked(self, epoch):
        self.control_epoch = epoch
        retained = []
        while not self.normal.empty():
            event = self.normal.get_nowait()
            original = event.get("control_epoch")
            if type(original) is int and original >= epoch:
                retained.append(event)
        for event in retained:
            self.normal.put_nowait(event)

    def discard_before_epoch(self, epoch):
        """A new Pi stop barrier cannot upgrade already queued user intent."""
        with self.lock:
            if epoch > self.control_epoch:
                self._discard_before_epoch_locked(epoch)

    def reset_control_epoch(self):
        """A different Pi boot invalidates ordinary input and starts a new epoch."""
        with self.lock:
            self.control_epoch = -1
            while not self.normal.empty():
                self.normal.get_nowait()


class MindWorker:
    """One bounded local inference at a time. Cancellation discards its result."""
    def __init__(self, brain, identity):
        self.brain, self.identity = brain, identity
        self.requests = queue.Queue(maxsize=1)
        self.results = queue.Queue(maxsize=1)
        self.busy = False
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="companion-mind")
        self.thread.start()

    def submit(self, generation, status, mood, memory_summary):
        if self.busy or self.closed.is_set():
            return False
        self.busy = True
        self.requests.put_nowait((generation, time.monotonic(), status, mood, memory_summary))
        return True

    def run(self):
        try:
            while not self.closed.is_set():
                try:
                    request = self.requests.get(timeout=0.2)
                except queue.Empty:
                    continue
                generation, started, status, mood, summary = request
                thought, error = None, None
                try:
                    thought = self.brain.reflect(None, status, self.identity, mood, summary, "")
                except Exception as exc:
                    error = str(exc)[:240]
                if not self.closed.is_set():
                    _latest(self.results, (generation, started, thought, error, (time.monotonic()-started)*1000))
        finally:
            if hasattr(self.brain, "close"):
                self.brain.close()

    def close(self):
        self.closed.set()
        # Inference has its own finite timeout; discard late output after shutdown.
        self.thread.join(timeout=0.3)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Crab: listen, respond and settle nearby.")
    parser.add_argument("--name", default=config.PET_NAME)
    parser.add_argument("--base-url", default=brain_config.BASE_URL)
    parser.add_argument("--perception-url", default=brain_config.PERCEPTION_BASE_URL)
    parser.add_argument("--mode", choices=("quiet", "interaction"), default="interaction")
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--max-ticks", type=int, default=0)
    parser.add_argument("--sim", action="store_true", help="Use deterministic local mock interpretation; does not simulate Pi hardware.")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-camera", action="store_true")
    parser.add_argument("--no-emote", action="store_true")
    parser.add_argument("--body-expressions", action="store_true", help="Request staged poses; Pi must independently be enabled after hardware acceptance.")
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--no-voice", action="store_true")
    parser.add_argument("--no-stt", action="store_true")
    parser.add_argument("--memory-db", default=config.PET_MEMORY_DB)
    parser.add_argument("--identity-file", default=config.PET_IDENTITY_FILE)
    parser.add_argument("--world-db", default=config.PET_WORLD_DB, help="Legacy file preserved; not used for live companion control.")
    parser.add_argument("--log")
    args = parser.parse_args(argv)

    identity = PetIdentity(args.identity_file, name=args.name)
    memory = MemoryStore(args.memory_db)
    realtime = RealtimeClient(args.base_url)
    client = RobotClient(args.base_url, timeout=5, realtime=realtime)
    control = RobotClient(args.base_url, timeout=1, realtime=realtime)
    voice = Voice(enabled=(config.PET_VOICE or args.voice) and not args.no_voice,
                  model=config.PET_VOICE_MODEL, player=config.PET_VOICE_PLAYER,
                  sink=config.PET_AUDIO_SINK, play_url=client.base_url+AUDIO_PLAY_PATH,
                  stop_url=client.base_url+AUDIO_STOP_PATH)
    coordinator = Coordinator(client, voice, identity, control_client=control, mode=args.mode,
                              motion_enabled=args.body_expressions and not args.no_emote,
                              continuity=memory.interaction_history(), transport_source="robot WebSocket")
    heard = EventInbox()
    inputs = InputWorker(client.base_url, args.perception_url, heard,
                         use_camera=not args.no_camera, realtime=realtime)
    mind = None
    mind_error = None
    if not args.no_llm:
        try:
            mind = MindWorker(build_pet_brain(args.sim), identity)
        except Exception as exc:
            mind_error = str(exc)[:240]
    ears = None
    if config.PET_STT and not args.no_stt:
        ears = build_ears(client.base_url+AUDIO_STREAM_PATH, config.WHISPER_MODEL,
                          lambda text: _latest(heard, {"text": text, "timestamp": time.time(), "source": "microphone",
                                                       "control_epoch": realtime.health().get("control_epoch")}),
                          wake_word=config.PET_WAKE_WORD or None,
                          device=config.WHISPER_DEVICE, compute_type=config.WHISPER_COMPUTE,
                          suppress=voice.listening_suppressed,
                          playback_epoch=lambda: (voice.listening_epoch, realtime.health().get("boot_id"),
                                                  realtime.health().get("control_epoch")))
    dashboard = Dashboard(client.base_url, args.dashboard)
    log = open(args.log, "a", encoding="utf-8") if args.log else None
    generation = 0
    latest_status = {}
    last_boot = None
    last_control_epoch = None
    started, last_publish = time.monotonic(), 0.0
    tick, think_ms = 0, None
    print(f"{identity.name}: {args.mode} companionship. Body expressions {'requested' if args.body_expressions else 'disabled'}.")
    print(f"Local controls and readiness: {client.base_url}/sim — say/type hello, stop, resume, or let's play.")
    print("Hardware validation pending. No autonomous translation. Ctrl+C requests a software stop.")
    try:
        inputs.start()
        if ears:
            ears.start()
        while not (args.max_ticks and tick >= args.max_ticks) and not (args.duration and time.monotonic()-started >= args.duration):
            tick += 1
            if hasattr(inputs, "failures"):
                try:
                    loss_reason = inputs.failures.get_nowait()
                    generation += 1
                    latest_status = {}
                    coordinator.connection_lost(loss_reason)
                except queue.Empty:
                    pass
            try:
                status, snapshot, error = inputs.samples.get_nowait()
                if status:
                    boot = status.get("boot_id")
                    if last_boot is not None and boot != last_boot:
                        generation += 1  # no model result survives a robot restart
                    epoch = status.get("control_epoch")
                    if last_control_epoch is not None and epoch != last_control_epoch:
                        generation += 1  # Pi stop cancels interpretation already in flight
                    last_boot = boot
                    last_control_epoch = epoch
                    latest_status = status
                else:
                    generation += 1
                coordinator.observe(status, snapshot)
            except queue.Empty:
                pass
            while True:
                try:
                    event = heard.get_nowait()
                except queue.Empty:
                    break
                age = event_age(event)
                if age is None or age > 5:
                    continue
                # Speaker output is never treated as fresh user engagement.
                if event.get("source") == "microphone" and voice.listening_suppressed():
                    continue
                text = event["text"]
                generation += 1
                coordinator.hear(text, source=event.get("source", "operator"),
                                 control_epoch=event.get("control_epoch"),
                                 event_id=event.get("event_id"),
                                 event_boot_id=event.get("boot_id"),
                                 event_session_id=event.get("session_id"))
                memory.remember(observed_facts={"utterance": text}, source=event.get("source", "user"))
                command = commands.interpret(text)
                if command is None and mind:
                    state = coordinator.snapshot()
                    status_for_mind = {**latest_status, "heard": text,
                                       "observed_labels": state.get("observed_labels", [])}
                    if not mind.submit(generation, status_for_mind, state.get("mood", "calm"), memory.summary()):
                        mind_error = "model already busy; new input handled by companion without queueing stale requests"
            if mind:
                try:
                    result_generation, request_started, thought, error, think_ms = mind.results.get_nowait()
                    mind.busy = False
                    if result_generation != generation or time.monotonic()-request_started > 8:
                        mind_error = "model result expired or cancelled by a newer event"
                    elif error:
                        mind_error = error
                    elif thought and thought.is_fresh():
                        coordinator.accept_thought(thought)
                        memory.remember(inferred_context=thought.observation, narrative=thought.say,
                                        source="mock_model" if args.sim else "local_model")
                        mind_error = None
                    else:
                        mind_error = "model result expired before behavior use"
                except queue.Empty:
                    pass
            coordinator.tick()
            now = time.monotonic()
            if now-last_publish >= 0.1:
                state = coordinator.snapshot()
                state.update(name=identity.name, character=identity.character, tick=tick,
                             pi_clock=latest_status.get("_clock"),
                             simulate=bool(latest_status.get("simulate")), think_ms=think_ms,
                             pipeline=inputs.health() if hasattr(inputs, "health") else {},
                             publisher=dashboard.health() if hasattr(dashboard, "health") else {},
                             audio=voice.health(), hearing=ears.health() if ears else {"available": False, "ignored_reason": "disabled or local Whisper model unavailable"}, mind={"available": mind is not None,
                                 "busy": bool(mind and mind.busy), "failure_reason": mind_error,
                                 "provenance": "simulated" if args.sim else "real"})
                dashboard.push(state)
                if log:
                    log.write(json.dumps(state)+"\n")
                    log.flush()
                last_publish = now
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        coordinator.close()
        inputs.close()
        if ears:
            ears.stop()
        if mind:
            mind.close()
        voice.close()
        dashboard.close()
        client.close()
        control.close()
        memory.close()
        if log:
            log.write(json.dumps(coordinator.snapshot())+"\n")
            log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
