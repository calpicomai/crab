"""Reproducible, hardware-free companion scenarios with inspectable traces.

Run ``python -m brain.pet.scenarios --all``. These exercise the real coordinator
with a virtual clock and explicit simulated observations; they do not open an
HTTP connection, synthesize speech, or load a model. Audio events are intentions,
not evidence that a speaker made sound. Motion physics are not simulated here.
"""
from __future__ import annotations

import argparse
import copy
import json
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCENARIOS = (
    "greeting", "interruption", "ignored-command", "sensor-dropout",
    "low-battery", "blocked-route", "model-timeout", "repeated-interaction",
    "reconnect",
)


class VirtualClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SimulatedVoice:
    """Records audible intentions while explicitly reporting no output sound."""
    def __init__(self):
        self.events = []
        self.busy = False
        self.enabled = False

    def say(self, text):
        self.events.append({"kind": "speech", "text": text, "played": False})
        return False

    def sound(self, name):
        self.events.append({"kind": "sound", "name": name, "played": False})
        return False

    def stop(self):
        self.busy = False

    def health(self):
        return {"available": False, "provenance": "simulated",
                "failure_reason": "scenario records audio intentions; no sound emitted"}

    def close(self):
        pass


class ScenarioRobot:
    """A protocol-shaped fake. Locomotion fails loudly if ever requested."""
    def __init__(self):
        self.calls = []

    def _result(self, action):
        from shared import Action, CommandResponse, RobotStatus
        self.calls.append(action)
        return CommandResponse(ok=True, action=Action(action),
                               status=RobotStatus(simulate=True),
                               detail="explicit scenario fixture; no hardware")

    def stand(self):
        return self._result("stand")

    def sit(self):
        return self._result("sit")

    def stop(self):
        return self._result("stop")

    def resume(self):
        return self._result("resume")

    def walk(self, *args, **kwargs):
        raise AssertionError("stationary companion requested locomotion")

    def turn(self, *args, **kwargs):
        raise AssertionError("stationary companion requested a precise turn")

    def turn_steps(self, *args, **kwargs):
        raise AssertionError("stationary companion requested a turn")


class ImmediateExecutor:
    """Deterministic scheduler for fake operations, not a concurrency test."""
    def __init__(self, *args, **kwargs):
        pass

    def submit(self, fn, *args, **kwargs):
        result = Future()
        try:
            result.set_result(fn(*args, **kwargs))
        except Exception as exc:
            result.set_exception(exc)
        return result

    def shutdown(self, **kwargs):
        pass


def simulated_status(clock, *, battery=7.5, distance=90.0, sonar_available=True):
    def health(available=True, reason=None):
        return {"available": available, "provenance": "simulated",
                "captured_at": clock(), "received_at": clock(), "max_age_s": 2.0,
                "failure_reason": reason, "confidence": None}
    return {"simulate": True, "pose": "sitting", "is_moving": False,
            "battery_v": battery, "distance_cm": distance if sonar_available else None,
            "inputs": {"battery": health(),
                       "ultrasonic": health(sonar_available, None if sonar_available else "sensor dropout"),
                       "camera": health(False, "scenario camera not enabled"),
                       "interpretation": health(False, "scenario detector not enabled"),
                       "microphone": health(False, "scripted text input")}}


def _stable(value, ids):
    """Normalize generated intention identifiers while preserving references."""
    if isinstance(value, dict):
        return {k: (_stable_id(v, ids) if k in {"id", "intent_id", "intention_id"}
                    and isinstance(v, str) else _stable(v, ids)) for k, v in value.items()}
    if isinstance(value, list):
        return [_stable(v, ids) for v in value]
    if hasattr(value, "model_dump"):
        return _stable(value.model_dump(mode="json"), ids)
    return value


def _stable_id(value, ids):
    if value not in ids:
        ids[value] = f"intention-{len(ids) + 1}"
    return ids[value]


def run_scenario(name):
    from .coordinator import Coordinator
    from .brain import PetThought
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario: {name}")
    clock, voice, robot = VirtualClock(), SimulatedVoice(), ScenarioRobot()
    identity = SimpleNamespace(name="Crab", seed_traits=["gentle", "curious"],
                               character="A patient, curious companion.")
    with patch("brain.pet.coordinator.ThreadPoolExecutor", ImmediateExecutor):
        companion = Coordinator(robot, voice, identity, clock=clock, wall_clock=clock,
                                mode="interaction", motion_enabled=name in {"sensor-dropout", "low-battery"})
    frames, ids = [], {}
    parameters = {}

    def step(event, utterance=None, seconds=0.5, connected=True):
        clock.advance(seconds)
        if connected:
            companion.observe(simulated_status(clock, **parameters))
        if utterance:
            companion.hear(utterance)
        companion.tick()
        state = _stable(copy.deepcopy(companion.snapshot()), ids)
        frames.append({"at_s": round(clock() - 1000.0, 2), "event": event,
                       "sensed": {"battery_v": parameters.get("battery", 7.5),
                                  "distance_cm": parameters.get("distance", 90.0),
                                  "sonar_available": parameters.get("sonar_available", True),
                                  "connection": connected},
                       "companion": state})

    try:
        step("ready", seconds=0)
        if name == "greeting":
            step("spoken greeting", "hello Crab")
        elif name == "interruption":
            step("spoken greeting", "hello Crab")
            step("stop takes precedence over praise", "good boy, stop")
        elif name == "ignored-command":
            step("unsupported movement request", "spin")
        elif name == "sensor-dropout":
            parameters["sonar_available"] = False
            step("ultrasonic loses support", "stand")
        elif name == "low-battery":
            parameters["battery"] = 6.0
            step("critical battery; no recovery posture motion", "stand")
        elif name == "blocked-route":
            parameters["distance"] = 8.0
            step("obstacle in forward beam", "come here")
        elif name == "model-timeout":
            step("spoken greeting", "hello Crab")
            thought = PetThought("This late response must be discarded.",
                                 generated_at=clock() - 20, ttl_s=8.0)
            companion.accept_thought(thought)
            step("model result arrived after its expiry")
        elif name == "repeated-interaction":
            for _ in range(5):
                step("repeated greeting", "hello Crab", seconds=0.2)
        elif name == "reconnect":
            step("spoken greeting", "hello Crab")
            companion.connection_lost("scenario network dropout")
            step("connection lost", connected=False)
            step("connection restored; pending actions must not replay")
        for _ in range(24):
            step("elapsed time", seconds=0.5)
        assert not any(c in {"walk", "turn", "turn_steps"} for c in robot.calls)
        return {"scenario": name, "provenance": "simulated",
                "assumptions": ["virtual clock", "scripted sensor observations",
                                "no physical audio", "no actuator execution", "no model inference"],
                "trace": frames, "audio_intentions": voice.events,
                "robot_commands": list(robot.calls)}
    finally:
        companion.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=SCENARIOS)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", action="store_true", help="Compact transitions and outcomes for review.")
    args = parser.parse_args(argv)
    names = SCENARIOS if args.all else (args.scenario or "greeting",)
    result = {"schema_version": 1, "scenarios": [run_scenario(name) for name in names]}
    if args.summary:
        result["scope"] = "Virtual-clock fixtures; no hardware, physical sound, real model, or motion physics."
        result["scenarios"] = [summarize(case) for case in result["scenarios"]]
    data = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data + "\n")
        print(f"Recorded {len(names)} simulated scenarios in {args.output}")
    else:
        print(data)
    return 0


def summarize(case):
    phases = []
    for frame in case["trace"]:
        phase = frame["companion"]["episode"]["phase"]
        if not phases or phases[-1]["phase"] != phase:
            phases.append({"at_s": frame["at_s"], "phase": phase})
    final = case["trace"][-1]["companion"]
    return {"scenario": case["scenario"], "phase_transitions": phases,
            "stopped": final["stopped"], "robot_commands": case["robot_commands"],
            "audio_intentions": case["audio_intentions"],
            "intentions": [{"kind": i["kind"], "status": i["status"],
                            "reason": i["result"].get("reason", i["reason"])} for i in final["intentions"]],
            "ignored_inputs": {k: v["ignored_reason"] for k, v in final["inputs"].items() if not v["used"]}}


if __name__ == "__main__":
    raise SystemExit(main())
