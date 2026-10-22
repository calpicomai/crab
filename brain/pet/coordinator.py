"""One owner of companion behavior, including the results of every movement.

The first milestone stays nearby: attention, acknowledgement, listening and
settling. A target is attention only. There is no pursuit or reactive wandering.
Motion and urgent stop use separate single-flight workers so a vendor gait cycle
cannot block receipt of a stop. The Pi determines the safe interruption point.
"""
from __future__ import annotations

import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from shared.companion import Intent, IntentionName, IntentionStatus, MindOutput
from shared.health import InputHealth, unavailable

from . import commands, expressions
from .brain import THOUGHT_TTL_S
from .mood import Mood


def _dict(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


class Coordinator:
    """Tick from one thread. Worker threads only return command results."""

    def __init__(self, client, voice, identity=None, *, clock=time.monotonic,
                 wall_clock=time.time, control_client=None, mode="interaction",
                 motion_enabled=False, continuity=None):
        if mode not in {"interaction", "quiet"}:
            raise ValueError("only interaction and quiet companionship are implemented")
        self.client, self.control_client = client, control_client or client
        self.voice, self.identity = voice, identity
        self.clock, self.wall_clock = clock, wall_clock
        self.mode, self.motion_enabled = mode, motion_enabled
        self.mood = Mood(clock=clock, traits=getattr(identity, "seed_traits", []))
        continuity = continuity or {}
        self.mood.interactions = max(0, int(continuity.get("engagements", 0)))
        self._captured_engagements = self.mood.interactions
        self._greeting_count = max(0, int(continuity.get("greetings", 0)))
        self._episode_reply = ""
        self._motion_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="companion-motion")
        self._control_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="companion-stop")
        self._motion = self._control = None
        self._deferred_stop = None
        self._coalesced_stops = []
        self._pending = []
        self._history = deque(maxlen=60)
        self._movements = deque(maxlen=60)
        self._status = {}
        self._status_at = None
        self._boot_id = None
        self._last_status_observed_at = None
        self.inputs = {}
        self.stopped = False
        self.blocked_reason = "waiting for robot status"
        self.phase = "settled"
        self._phase_until = clock()
        self._target_until = 0.0
        self.target = None
        self._visible_labels = set()
        self._ignored_detections = []
        self._visible_until = 0.0
        self._last_notice = float("-inf")
        self._last_engagement = float("-inf")
        self._interaction_wall = wall_clock()
        self.last_heard = self.last_say = ""
        self._closed = False

    def request(self, kind, *, source="human", reason="", ttl_s=5.0, payload=None):
        """Schedule a bounded intention. Priorities are policy, never model input."""
        now = self.wall_clock()
        kind = IntentionName(kind)
        priority = 100 if kind == IntentionName.STOP else (90 if kind == IntentionName.RESUME
                    else 70 if source == "human" else 20 if source == "mind" else 40)
        intent = Intent(kind=kind, priority=priority, created_at=now,
                        expires_at=now + max(0.01, min(float(ttl_s), 30.0)),
                        source=source, reason=reason, payload=payload or {})
        self._history.append(intent)
        if kind == IntentionName.STOP:
            self._stop(intent)
        elif self.stopped and kind != IntentionName.RESUME:
            self._finish(intent, "blocked", "stop latched; explicit resume required")
        elif len(self._pending) >= 8:
            self._finish(intent, "blocked", "intention queue full")
        else:
            self._pending.append(intent)
        return intent

    def _finish(self, intent, status, reason="", **result):
        intent.status = IntentionStatus(status)
        intent.result.update(reason=reason, **result)
        if status in {"blocked", "failed"}:
            self.blocked_reason = reason

    def _cancel_pending(self, reason):
        for intent in self._pending:
            self._finish(intent, "cancelled", reason)
        self._pending.clear()
        if self._motion:
            self._finish(self._motion[0], "cancelled", reason)

    def _cancel_obsolete(self, predicate, reason):
        for intent in list(self._pending):
            if predicate(intent):
                self._finish(intent, "cancelled", reason)
                self._pending.remove(intent)

    def _stop(self, intent):
        self.stopped = True
        self.blocked_reason = intent.reason or "stop requested"
        self.phase = "stopped"
        self.target = None
        self._interaction_wall = self.wall_clock()
        self._cancel_pending(intent.reason or "stop")
        self.voice.stop()
        intent.status = IntentionStatus.ACTIVE
        if self._control:
            if self._control[0].kind == IntentionName.RESUME:
                self._finish(self._control[0], "cancelled", "stop superseded resume")
                if self._deferred_stop is None:
                    self._deferred_stop = intent
                else:
                    self._coalesced_stops.append(intent)
            else:
                self._coalesced_stops.append(intent)
        else:
            self._control = (intent, self._control_pool.submit(self.control_client.stop))

    def connection_lost(self, reason="robot unavailable"):
        self._status_at = None
        self.target = None
        self.inputs["transport"] = self._missing(reason, source="robot HTTP").snapshot(self.wall_clock())
        self.blocked_reason = reason
        if not self.stopped:
            self.request("stop", source="safety", reason=reason)

    def observe(self, status, snapshot=None):
        if status is None:
            self.connection_lost()
            return
        status = _dict(status)
        observed_at = status.get("observed_at")
        if (status.get("boot_id") == self._boot_id and self._number(observed_at)
                and self._last_status_observed_at is not None
                and observed_at <= self._last_status_observed_at):
            return  # an older poll cannot undo an acknowledged control transition
        self._status = status
        self._status_at = self.clock()
        if self.blocked_reason == "waiting for robot status":
            self.blocked_reason = ""
        boot = self._status.get("boot_id")
        if self._boot_id is not None and boot != self._boot_id:
            self.request("stop", source="safety", reason="Pi restarted; explicit resume required")
        self._boot_id = boot
        self._last_status_observed_at = observed_at if self._number(observed_at) else None
        if self._status.get("motion_state", {}).get("stop_latched") and not self.stopped:
            self.stopped = True
            self.phase = "stopped"
            self.blocked_reason = "Pi stop latched; explicit resume required"
            self._cancel_pending(self.blocked_reason)
            self.target = None
        self.inputs = {name: self._input(raw, used=False, why="not used by stationary behavior")
                       for name, raw in self._status.get("inputs", {}).items()}
        self.inputs["transport"] = InputHealth(available=True, provenance="real",
            captured_at=self.wall_clock(), max_age_s=2.0, source="robot HTTP",
            used=True, ignored_reason=None).snapshot(self.wall_clock())
        for name in ("battery", "ultrasonic", "camera", "microphone", "interpretation"):
            self.inputs.setdefault(name, self._missing("no health metadata", source=name).snapshot(self.wall_clock()))
        for name in ("battery", "ultrasonic"):
            if self._usable(name):
                self.inputs[name].update(used=True, ignored_reason=None)
        battery = self._status.get("battery_v")
        if self._usable("battery") and self._number(battery) and battery <= 6.2 and not self.stopped:
            self.request("stop", source="safety", reason="critical battery; no further actuator motion")
        if self._status.get("reflex_stopped") and not self.stopped:
            self.request("stop", source="safety", reason="Pi reflex stopped motion")
        self._observe_perception(snapshot)
        if self._motion and self._motion_block():
            if not self.stopped:
                self.request("stop", source="safety", reason=self._motion_block())

    def _input(self, raw, *, used, why):
        try:
            data = _dict(raw)
            health = InputHealth.model_validate({k: v for k, v in data.items()
                                                 if k in InputHealth.model_fields})
        except Exception:
            health = self._missing("invalid health metadata")
        if health.usable(self.wall_clock(), allow_simulated=bool(self._status.get("simulate"))):
            health.used, health.ignored_reason = used, None if used else why
        else:
            health.used = False
            health.ignored_reason = health.failure_reason or "stale, unavailable, or unsupported provenance"
        return health.snapshot(self.wall_clock())

    def _missing(self, reason, *, source=""):
        health = unavailable(reason, source=source)
        health.received_at = self.wall_clock()
        return health

    def _usable(self, name):
        raw = self.inputs.get(name, {})
        try:
            health = InputHealth.model_validate({k: v for k, v in raw.items()
                                                 if k in InputHealth.model_fields})
            return health.usable(self.wall_clock(), allow_simulated=bool(self._status.get("simulate")))
        except Exception:
            return False

    def _observe_perception(self, snapshot):
        previous_labels = self._visible_labels
        self._visible_labels = set()
        self._ignored_detections = []
        if snapshot is None:
            self.target = None
            return
        try:
            snapshot = _dict(snapshot)
            inputs = snapshot.get("inputs") or {}
            if not isinstance(inputs, dict) or not isinstance(snapshot.get("detections", []), list):
                raise ValueError("invalid perception payload")
        except (ValueError, TypeError):
            self.inputs["interpretation"] = self._missing("invalid perception payload").snapshot(self.wall_clock())
            self.target = None
            return
        for name in ("camera", "interpretation"):
            self.inputs[name] = self._input(inputs.get(name, {}),
                                           used=True, why="attention only")
        if not all(self._usable(name) for name in ("camera", "interpretation")):
            self.target = None
            return
        labels = set()
        for detection in snapshot.get("detections", [])[:100]:
            if not isinstance(detection, dict):
                self._ignored_detections.append({"reason": "invalid detection payload"})
                continue
            source = detection.get("source")
            reason = None
            if source not in {"yolo", "nanoowl", "simblob"}:
                reason = "dummy or unsupported detector source"
            elif source == "simblob" and not self._status.get("simulate"):
                reason = "simulation detection on real deployment"
            elif not self._number(detection.get("score")) or detection["score"] < 0.5:
                reason = "confidence below attention threshold"
            if reason:
                self._ignored_detections.append({"label": str(detection.get("label", ""))[:64],
                                                 "source": source, "reason": reason})
            else:
                labels.add(str(detection.get("label", "")).strip().lower()[:64])
        labels.discard("")
        if self._ignored_detections and not labels:
            self.inputs["interpretation"].update(used=False, ignored_reason="all detections ignored; inspect ignored_detections")
        self._visible_labels = labels
        self._visible_until = self.clock() + 2.0
        if self.target not in labels:
            self.target = None
        # A visible person is a detection, never an identity or familiarity claim.
        newly_visible = labels - previous_labels
        notice_interval = 60.0 - 40.0 * self.mood.temperament.curiosity
        if newly_visible and self.phase == "settled" and self.clock() - self._last_notice > notice_interval and not self.stopped:
            self.target = sorted(newly_visible)[0]
            self._target_until = self.clock() + 2.0
            self._last_notice = self.clock()
            self.mood.event("notice")
            self._begin_episode()

    @staticmethod
    def _number(value):
        return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)

    def _motion_block(self):
        if self.stopped:
            return "stop latched; explicit resume required"
        if self._status_at is None or self.clock() - self._status_at > 2.0:
            return "robot status stale or disconnected"
        if not self.motion_enabled:
            return "body expression disabled; enable only after hardware acceptance"
        if not self._usable("battery") or not self._number(self._status.get("battery_v")):
            return "battery unavailable or stale"
        if self._status["battery_v"] <= 6.2:
            return "critical battery"
        if not self._usable("ultrasonic") or not self._number(self._status.get("distance_cm")):
            return "ultrasonic unavailable or stale"
        if self._status["distance_cm"] < 25.0:
            return "near obstacle; hold current pose"
        return ""

    def _begin_episode(self):
        self.phase = "notice"
        self._phase_until = self.clock() + self.mood.temperament.hesitation_s

    def hear(self, text, *, source="operator"):
        text = str(text).strip()[:1000]
        if not text:
            return None
        self.last_heard = text
        if source == "microphone" and self._usable("microphone"):
            self.inputs["microphone"].update(used=True, ignored_reason=None)
        self._interaction_wall = self.wall_clock()
        cmd = commands.interpret(text)
        name = cmd.name if cmd else "engagement"
        self._captured_engagements += 1
        greeting_index = self._greeting_count
        self._greeting_count += int(name == "greet")
        self._cancel_obsolete(lambda intent: intent.source == "mind",
                              "newer user engagement superseded pending model result")
        if name in {"stop", "stay", "scold"}:
            return self.request("stop", reason="user requested stop")
        if name == "resume":
            return self.request("resume", reason="user explicitly resumed")
        if self.stopped:
            return self.request("acknowledge", reason="engagement while stopped")
        self.mood.event("praise" if name == "praise" else "play" if name == "play" else "engagement")
        # Repetition extends listening without repeatedly chirping or moving.
        repeat = self.clock() - self._last_engagement < 8.0
        self._last_engagement = self.clock()
        preserve_initial_ack = repeat and self.phase in {"notice", "orient", "acknowledge"}
        self._cancel_obsolete(
            lambda intent: intent.source == "episode" and
            (not preserve_initial_ack or intent.kind not in {IntentionName.ORIENT, IntentionName.ACKNOWLEDGE}),
            "newer user engagement superseded episode transition")
        if name in {"stand", "sit"}:
            self._cancel_obsolete(lambda intent: intent.kind in {IntentionName.STAND, IntentionName.SIT},
                                  "newer posture request superseded pending posture")
        if self.voice.busy:
            self.voice.stop()
        if name == "greet" and not repeat:
            self._episode_reply = ("Hello.", "Here I am.", "Hello. I'm listening.")[greeting_index % 3]
        if name in {"come", "explore", "spin", "shake", "unsupported"}:
            intent = self.request("respond", payload={"say": "I'll stay nearby for now."},
                                  reason="locomotion and gesture unavailable in this milestone")
            intent.result["unsupported_request"] = name
            return intent
        if name in {"sit", "stand"}:
            return self.request(name, reason="explicit posture request")
        if name == "listen":
            return self.request("listen", reason="explicit listening request")
        if name == "play":
            return self.request("play", reason="invited to play")
        if repeat:
            if self.phase in {"notice", "orient", "acknowledge"}:
                return None  # finish the first acknowledgement without restarting it
            self.phase = "listen"
            self._phase_until = self.clock() + self.mood.temperament.listen_s
            return self.request("listen", reason="continued engagement")
        self._begin_episode()
        return None

    def accept_thought(self, thought):
        now = self.wall_clock()
        generated = getattr(thought, "generated_at", 0.0)
        ttl = getattr(thought, "ttl_s", 0.0)
        if not self._number(generated) or not self._number(ttl) or not (0 <= now - generated < ttl <= THOUGHT_TTL_S):
            intent = self.request("respond", source="mind", reason="stale model result")
            self._pending = [i for i in self._pending if i is not intent]
            self._finish(intent, "expired", "model result expired")
            return intent
        if generated < self._interaction_wall:
            intent = self.request("respond", source="mind", reason="superseded model result")
            self._pending = [i for i in self._pending if i is not intent]
            self._finish(intent, "cancelled", "newer engagement or stop superseded model result")
            return intent
        try:
            output = MindOutput.model_validate({key: getattr(thought, key, default)
                for key, default in {"say": "", "intention": "respond", "gesture": "none",
                                     "target": None, "mood_hint": None, "observation": ""}.items()})
        except Exception as exc:
            intent = self.request("respond", source="mind", reason="invalid model output")
            self._pending = [i for i in self._pending if i is not intent]
            self._finish(intent, "blocked", f"invalid model output: {str(exc)[:200]}")
            return intent
        intent = self.request(output.intention, source="mind", reason="fresh validated local model result",
                              ttl_s=min(ttl - (now - generated), 5.0), payload=output.model_dump())
        if output.target:
            target = output.target.lower()
            if self.clock() <= self._visible_until and target in self._visible_labels and not self.stopped:
                self.target = target
                self._target_until = self.clock() + min(ttl, 2.0)
                intent.result["attention_target"] = target
            else:
                intent.result["ignored_target"] = "target absent from fresh supported perception"
        return intent

    def _poll(self):
        for attr in ("_motion", "_control"):
            pair = getattr(self, attr)
            if pair is None or not pair[1].done():
                continue
            intent, future = pair
            setattr(self, attr, None)
            try:
                result = _dict(future.result())
                self._movements.append({"intention_id": intent.id, "at": self.wall_clock(),
                                        "response": result})
                was_cancelled = intent.status == IntentionStatus.CANCELLED
                self._finish(intent, "cancelled" if was_cancelled else "completed" if result.get("ok") else "failed",
                             result.get("detail", ""), response=result)
                if intent.kind == IntentionName.RESUME and result.get("ok") and not was_cancelled:
                    self.stopped = False
                    self.phase = "settled"
                    self.blocked_reason = ""
                    status = result.get("status") or {}
                    observed_at = status.get("observed_at")
                    if status.get("boot_id") == self._boot_id and self._number(observed_at):
                        self._last_status_observed_at = max(self._last_status_observed_at or 0.0, observed_at)
                if attr == "_motion" and result.get("status"):
                    self._status["pose"] = result["status"].get("pose", "unknown")
                    self._status["motion_state"] = result["status"].get("motion_state", {})
                if intent.kind == IntentionName.STOP:
                    for waiting in self._coalesced_stops:
                        self._finish(waiting, "completed" if result.get("ok") else "failed",
                                     "coalesced Pi stop result", response=result)
                    self._coalesced_stops.clear()
            except Exception as exc:
                reason = f"command result unavailable: {str(exc)[:200]}"
                self._movements.append({"intention_id": intent.id, "at": self.wall_clock(),
                                        "outcome": "unknown", "reason": reason})
                self._finish(intent, "failed", reason)
                self.connection_lost(reason)
                if intent.kind == IntentionName.STOP:
                    for waiting in self._coalesced_stops:
                        self._finish(waiting, "failed", reason)
                    self._coalesced_stops.clear()
            if attr == "_control" and self._deferred_stop is not None:
                stop = self._deferred_stop
                self._deferred_stop = None
                self._control = (stop, self._control_pool.submit(self.control_client.stop))

    def _execute(self, intent):
        intent.status = IntentionStatus.ACTIVE
        kind = intent.kind.value
        if kind == "resume":
            if self._control or self._motion:
                intent.status = IntentionStatus.PENDING
                return False
            if self._status_at is None or self.clock() - self._status_at > 2.0:
                self._finish(intent, "blocked", "cannot resume without fresh robot connection")
            else:
                self._control = (intent, self._control_pool.submit(self.control_client.resume))
            return True
        if self.stopped:
            self._finish(intent, "blocked", "stop latched; explicit resume required")
            return True
        if kind in {"stand", "sit"}:
            if self._motion:
                intent.status = IntentionStatus.PENDING
                return False
            why = self._motion_block()
            if why:
                self._finish(intent, "blocked", why)
            elif self._status.get("pose") == ("standing" if kind == "stand" else "sitting"):
                self._finish(intent, "completed", "already in requested pose; no actuator command")
            else:
                self._motion = (intent, self._motion_pool.submit(getattr(self.client, kind)))
            return True
        if kind == "orient":
            self._finish(intent, "completed", "attention only; no measured bearing or physical turn")
        elif kind == "listen":
            self.phase = "listen"
            self._phase_until = self.clock() + self.mood.temperament.listen_s
            self._finish(intent, "completed", "listening window opened")
        elif kind in {"acknowledge", "play", "settle"}:
            sound = {"acknowledge": "acknowledge", "play": "play", "settle": "settle"}[kind]
            reply = self._episode_reply if kind == "acknowledge" else ""
            if reply:
                self.last_say, self._episode_reply = reply, ""
            spoken = bool(self.voice.say(reply)) if reply and self.mode != "quiet" and getattr(self.voice, "enabled", False) else False
            queued = bool(self.voice.sound(sound)) if self.mode != "quiet" and not spoken else False
            self._finish(intent, "completed", "quiet mode" if self.mode == "quiet" else "audio requested; inspect playback health",
                         sound_queued=queued, speech_queued=spoken)
            if kind == "play":
                self.phase = "listen"
                self._phase_until = self.clock() + self.mood.temperament.persistence_s
            elif kind == "settle":
                self.phase = "settled"
                if self.motion_enabled:
                    self.request("sit", source="episode", reason="settling after engagement")
            elif self.motion_enabled:
                self.request("stand", source="episode", reason="acknowledge engagement once")
        else:
            say = str(intent.payload.get("say", ""))[:240]
            self.last_say = say
            queued = bool(self.voice.say(say)) if say and self.mode != "quiet" else False
            self._finish(intent, "completed", "response prepared; inspect playback health", speech_queued=queued)
            self.phase = "respond"
            self._phase_until = self.clock() + 1.0
        gesture = expressions.plan(intent.payload.get("gesture", "none"))
        if gesture.primitive and self.motion_enabled:
            self.request(gesture.primitive, source="episode", reason="bounded expression")
        return True

    def tick(self):
        if self._closed:
            return
        self._poll()
        now = self.clock()
        self.mood.advance(now)
        if self.target and (now > self._target_until or now > self._visible_until):
            self.target = None
        if self._status_at is not None and now - self._status_at > 2.0 and not self.stopped:
            self.connection_lost("robot status expired")
        if self._motion and self._motion_block() and not self.stopped:
            self.request("stop", source="safety", reason=self._motion_block())
        for intent in list(self._pending):
            if self.wall_clock() >= intent.expires_at:
                self._finish(intent, "expired", "intention expired before execution")
                self._pending.remove(intent)
        if self._pending:
            intent = max(self._pending, key=lambda item: item.priority)
            if self._execute(intent):
                self._pending.remove(intent)
        if self.stopped or now < self._phase_until:
            return
        if self.phase == "notice":
            self.phase = "orient"
            self.request("orient", source="episode", reason="noticed engagement")
            self._phase_until = now + 0.25 / self.mood.temperament.tempo
        elif self.phase == "orient":
            self.phase = "acknowledge"
            self.request("acknowledge", source="episode", reason="acknowledge once then listen")
            self._phase_until = now + 0.5
        elif self.phase in {"acknowledge", "respond"} and not self.voice.busy:
            self.phase = "listen"
            self._phase_until = now + self.mood.temperament.listen_s
        elif self.phase == "listen" and not self.voice.busy:
            self.phase = "settle"
            self.request("settle", source="episode", reason="interaction window ended; remain nearby")

    def snapshot(self):
        for name, raw in list(self.inputs.items()):
            self.inputs[name] = self._input(raw, used=raw.get("used", False),
                                            why=raw.get("ignored_reason") or "not consumed")
        return {"episode": {"phase": self.phase}, "mode": self.mode,
                "mood": self.mood.current, "state": self.mood.snapshot(),
                "stopped": self.stopped, "blocked_reason": self.blocked_reason,
                "target": self.target, "last_heard": self.last_heard, "last_say": self.last_say,
                "observed_labels": sorted(self._visible_labels) if self.clock() <= self._visible_until else [],
                "ignored_detections": self._ignored_detections,
                "distance_cm": self._status.get("distance_cm"), "battery_v": self._status.get("battery_v"),
                "pose": self._status.get("pose", "unknown"),
                "continuity": {"engagements": self._captured_engagements, "greetings": self._greeting_count,
                               "meaning": "interaction counts, not person recognition"},
                "inputs": self.inputs, "intentions": [i.model_dump(mode="json") for i in self._history],
                "movements": list(self._movements), "audio": self.voice.health(),
                "motion_enabled": self.motion_enabled, "position": "unmeasured; no locomotion in milestone 1"}

    def close(self):
        if self._closed:
            return
        self.request("stop", source="safety", reason="companion shutdown")
        self._cancel_pending("companion shutdown")
        self.voice.stop()
        # The caller may close HTTP clients immediately after close(). Give the
        # separate control connection a bounded chance to acknowledge first.
        deadline = time.monotonic() + 1.5
        while self._control and time.monotonic() < deadline:
            self._poll()
            if self._control:
                time.sleep(0.005)
        if self._control:
            self._finish(self._control[0], "failed", "shutdown stop acknowledgement timed out; physical state unknown")
        if self._motion:
            self._movements.append({"intention_id": self._motion[0].id, "at": self.wall_clock(),
                                    "outcome": "unknown", "reason": "shutdown before movement result; Pi stop requested"})
        self._closed = True
        self._motion_pool.shutdown(wait=False, cancel_futures=True)
        self._control_pool.shutdown(wait=False, cancel_futures=False)
