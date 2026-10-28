"""Proofs for passive commissioning, configuration isolation, and redaction."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from brain import diagnostics
from brain.pet import readiness

ROOT = Path(__file__).resolve().parents[1]

# Fresh interpreters prevent an earlier test import from hiding eager imports.
PASSIVE_RUNNER = r'''
import builtins, importlib.util, os, runpy, socket, subprocess, sys
real_import = builtins.__import__
forbidden = {"httpx", "pydantic", "websockets", "torch", "cv2", "faster_whisper",
             "ultralytics", "nanoowl", "rclpy", "picrawler", "robot_hat", "RPi",
             "sounddevice", "pyaudio", "brain.client", "brain.config", "brain.pet.voice",
             "brain.hearing", "brain.pet.inputs", "brain.pet.loop", "brain.pet.brain", "robot"}
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    absolute = importlib.util.resolve_name("." * level + name, globals["__package__"]) if level else name
    names = [absolute, *(absolute + "." + child for child in (fromlist or ()))]
    if any(candidate == item or candidate.startswith(item + ".") for candidate in names for item in forbidden):
        raise AssertionError("forbidden runtime import: " + name)
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
real_open = builtins.open
def metadata_only_open(path, *args, **kwargs):
    if isinstance(path, (str, bytes, os.PathLike)):
        value = os.fsdecode(path)
        assert "PRIVATE-COMMISSIONING-CANARY" not in value, "private/model contents must remain unopened"
        assert not value.startswith("/dev/") or value == os.devnull, "must not open devices"
    return real_open(path, *args, **kwargs)
builtins.open = metadata_only_open
def forbidden_operation(*args, **kwargs):
    raise AssertionError("network/control/capture/process operation forbidden")
socket.socket = forbidden_operation
socket.create_connection = forbidden_operation
socket.getaddrinfo = forbidden_operation
real_run = subprocess.run
def git_only(command, **kwargs):
    assert command[0] == "git" and command[-3:] == ["rev-parse", "--verify", "HEAD"], command
    assert kwargs["timeout"] == 1 and kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    return real_run(command, **kwargs)
subprocess.run = git_only
real_popen = subprocess.Popen
def git_popen(command, *args, **kwargs):
    assert command[0] == "git", command
    return real_popen(command, *args, **kwargs)
subprocess.Popen = git_popen
sys.argv = ["brain.pet.readiness", *sys.argv[1:]]
runpy.run_module("brain.pet.readiness", run_name="__main__")
'''


class PassiveReadinessTests(unittest.TestCase):
    def run_fresh(self, args, extra_env=None, *, guarded=True):
        env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": str(ROOT)}
        env.update(extra_env or {})
        command = [sys.executable, "-c", PASSIVE_RUNNER] if guarded else [sys.executable, "-m", "brain.pet.readiness"]
        return subprocess.run([*command, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)

    def test_unused_invalid_legacy_values_do_not_break_help_or_offline(self):
        env = {"WANDER_SPEED": "unused-invalid", "COSTMAP_BINS": "unused-invalid",
               "WANDER_REFLEX_CM": "unused-invalid", "PET_REFLECT_S": "unused-invalid"}
        for args in (["--help"], ["--offline", "--ros-humble"]):
            with self.subTest(args=args):
                result = self.run_fresh(args, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("offline", result.stdout)

    def test_help_does_not_validate_or_expose_invalid_active_values(self):
        result = self.run_fresh(["--help"], {"ROBOT_PORT": "private-value"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("private-value", result.stdout + result.stderr)

    def test_invalid_active_config_has_clear_error_and_no_runtime_activity(self):
        for name, value in (("ROBOT_PORT", "secret-token"), ("ROBOT_PORT", "0"),
                            ("ROBOT_PORT", "65536"), ("ROBOT_TIMEOUT_S", "nan"),
                            ("ROBOT_TIMEOUT_S", "inf"), ("ROBOT_TIMEOUT_S", "-1"),
                            ("PET_STT", "private-invalid-flag"),
                            ("PET_AUDIO_SINK", "private-invalid-sink"),
                            ("PET_VOICE_PLAYER", "'private-invalid-player")):
            with self.subTest(name=name, value=value):
                result = self.run_fresh(["--offline"], {name: value})
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertIn(name, report["failure_reason"])
                if "private" in value or "secret" in value:
                    self.assertNotIn(value, result.stdout + result.stderr)

    def test_online_active_config_error_also_prevents_runtime_imports(self):
        result = self.run_fresh([], {"ROBOT_TIMEOUT_S": "private-invalid-timeout"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ROBOT_TIMEOUT_S", json.loads(result.stdout)["failure_reason"])
        self.assertNotIn("private-invalid-timeout", result.stdout + result.stderr)

    def test_offline_accepts_private_config_without_echoing_or_opening_it(self):
        secret = "PRIVATE-COMMISSIONING-CANARY"
        result = self.run_fresh(["--offline", "--base-url", "http://user:" + secret + "@private.local"], {
            "API_KEY": secret, "LLM_API_KEY": secret,
            "PET_HOME": "/private/" + secret, "PET_NAME": secret,
            "PET_WAKE_WORD": secret, "PET_IDENTITY_FILE": "/private/" + secret,
            "ROBOT_HOST": secret, "LLM_BASE_URL": "http://user:" + secret + "@private.local",
            "PET_VOICE_MODEL": "/private/" + secret, "PET_VOICE_PLAYER": secret,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["profile"]["capabilities"]["control_owner"]["acquired"])
        self.assertFalse(report["hardware_verified"])
        self.assertEqual(report["commissioning_gates"]["jetson_storage_journal"]["state"], "unknown")
        self.assertEqual(report["checks"]["remote_diagnostics"]["state"], "disabled")

    def test_client_import_isolated_from_unused_navigation_but_not_active_config(self):
        env = dict(os.environ, WANDER_SPEED="unused-invalid", COSTMAP_BINS="unused-invalid")
        script = "from brain.client import RobotClient; from brain import config; assert config.WANDER_REFLEX_CM == 25"
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        env["ROBOT_TIMEOUT_S"] = "private-invalid-timeout"
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ROBOT_TIMEOUT_S must be a finite positive number", result.stderr)
        self.assertNotIn("private-invalid-timeout", result.stderr)

    def test_explicit_legacy_consumer_still_rejects_invalid_value(self):
        env = dict(os.environ, WANDER_SPEED="private-invalid-speed")
        result = subprocess.run([sys.executable, "-c", "from brain import config; print(config.WANDER_SPEED)"],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WANDER_SPEED has an invalid legacy navigation value", result.stderr)
        self.assertNotIn("private-invalid-speed", result.stderr)

    def test_stationary_config_does_not_evaluate_legacy_reflex_fallbacks(self):
        env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": str(ROOT),
               "WANDER_REFLEX_CM": "private-invalid-wander", "AGENT_REFLEX_CM": "private-invalid-agent",
               "PET_REFLEX_CM": "private-invalid-pet"}
        script = "from brain.pet import config; assert not config.PET_STT; assert 'PET_REFLEX_CM' not in vars(config)"
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for target, expected_key in (("brain.config.WANDER_REFLEX_CM", "WANDER_REFLEX_CM"),
                                     ("brain.agent.config.AGENT_REFLEX_CM", "AGENT_REFLEX_CM"),
                                     ("brain.pet.config.PET_REFLEX_CM", "PET_REFLEX_CM")):
            module, attribute = target.rsplit(".", 1)
            script = f"import importlib; print(getattr(importlib.import_module({module!r}), {attribute!r}))"
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_key, result.stderr)
            self.assertNotIn("private-invalid", result.stderr)
        # Explicit overrides do not evaluate a malformed unused fallback.
        env.update(AGENT_REFLEX_CM="31", PET_REFLEX_CM="37")
        script = "from brain.pet import config; assert config.PET_REFLEX_CM == 37; assert config.llm_config.AGENT_REFLEX_CM == 31"
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stt_default_empty_and_explicit_values_match_runtime_and_passive_profile(self):
        cases = ((None, False), ("", False), ("  ", False), ("0", False), ("false", False),
                 ("off", False), ("1", True), ("true", True), (" YES ", True), ("on", True))
        script = """import json
from brain.pet import config
from brain import diagnostics
item = diagnostics.flag('PET_STT', diagnostics.FLAGS['PET_STT'], __import__('os').environ)
print(json.dumps({'runtime': config.PET_STT, 'profile': item}))
"""
        for setting, expected in cases:
            with self.subTest(setting=setting):
                env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONPATH": str(ROOT)}
                if setting is not None:
                    env["PET_STT"] = setting
                result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["runtime"], expected)
                self.assertEqual(report["profile"]["requested"], expected)
                self.assertEqual(report["profile"]["specified"], setting is not None)
                passive = self.run_fresh(["--offline"], {} if setting is None else {"PET_STT": setting})
                self.assertEqual(passive.returncode, 0, passive.stderr)
                self.assertEqual(json.loads(passive.stdout)["profile"]["flags"]["PET_STT"]["requested"], expected)

    def test_launcher_mic_default_and_saved_opt_in_with_explicit_no_stt_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            brain = root / "brain"
            python = brain / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            # A local argument recorder, never the companion or an interpreter.
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            python.chmod(0o700)
            shutil.copyfile(ROOT / "brain" / "run.sh", brain / "run.sh")
            for value, expected_disabled in ((None, True), ("", True), ("0", True), ("1", False)):
                with self.subTest(value=value):
                    env = {"PATH": os.environ.get("PATH", os.defpath)}
                    if value is not None:
                        env["PET_STT"] = value
                    result = subprocess.run(["bash", str(brain / "run.sh"), "pet"], cwd=root, env=env,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual("--no-stt" in result.stdout.splitlines(), expected_disabled)
            (root / "crab.env").write_text("PET_STT=1\n")
            for extra, expected_disabled in (([], False), (["--", "--no-stt"], True)):
                result = subprocess.run(["bash", str(brain / "run.sh"), "pet", *extra], cwd=root,
                                        env={"PATH": os.environ.get("PATH", os.defpath)},
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("--no-stt" in result.stdout.splitlines(), expected_disabled)
            self.assertEqual((root / "crab.env").read_text(), "PET_STT=1\n")


class MetadataTests(unittest.TestCase):
    def report(self, env):
        with patch.object(diagnostics, "build_metadata", return_value={"state": "unknown", "commit": None}):
            return diagnostics.local_report(environ=env)

    def test_feature_states_distinguish_defaults_specification_and_missing_assets(self):
        available = {name: {"state": "available"} for name in (*diagnostics.MODULES, *diagnostics.EXECUTABLES)}
        with patch.object(diagnostics, "dependency_metadata", return_value=available):
            default = self.report({})
            self.assertEqual(default["profile"]["capabilities"]["speech_output"]["state"], "disabled")
            self.assertFalse(default["profile"]["flags"]["PET_VOICE"]["specified"])
            self.assertFalse(default["profile"]["flags"]["PET_STT"]["requested"])
            self.assertEqual(default["profile"]["capabilities"]["speech_input"]["state"], "disabled")
            report = self.report({"PET_VOICE": "1", "PET_STT": "0"})
            self.assertTrue(report["profile"]["flags"]["PET_VOICE"]["specified"])
            self.assertEqual(report["profile"]["capabilities"]["speech_output"]["state"], "unconfigured")
            self.assertEqual(report["profile"]["capabilities"]["speech_input"]["state"], "disabled")
            report = self.report({"PET_VOICE": "1", "PET_VOICE_MODEL": "/missing/private/model.onnx"})
            self.assertEqual(report["assets"]["PET_VOICE_MODEL"]["state"], "missing")
            self.assertNotIn("/missing/private", json.dumps(report))
            with tempfile.TemporaryDirectory() as folder:
                model = Path(folder) / "private-model.onnx"
                model.touch()
                Path(str(model) + ".json").touch()
                with patch("builtins.open", side_effect=AssertionError("must never read model/identity contents")):
                    asset = diagnostics.configured_asset("PET_VOICE_MODEL", {"PET_VOICE_MODEL": str(model)}, companion_suffix=".json")
                self.assertEqual(asset["state"], "available")
                self.assertFalse(asset["load_tested"])
        with patch.object(diagnostics.importlib.util, "find_spec", side_effect=RuntimeError("PRIVATE")):
            dependencies = diagnostics.dependency_metadata()
        self.assertEqual(dependencies["rclpy"]["state"], "error")
        self.assertNotIn("PRIVATE", json.dumps(dependencies))

    def test_build_query_is_bounded_fixed_read_only_and_redacted(self):
        with patch.object(diagnostics.subprocess, "run", side_effect=subprocess.TimeoutExpired("PRIVATE", 1)) as run:
            report = diagnostics.build_metadata()
        self.assertEqual(report["state"], "error")
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertEqual(run.call_args.kwargs["timeout"], 1)
        self.assertEqual(run.call_args.args[0][-3:], ["rev-parse", "--verify", "HEAD"])
        self.assertEqual(set(run.call_args.kwargs["env"]),
                         {"PATH", "LC_ALL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_OPTIONAL_LOCKS"})
        with patch.object(diagnostics.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "PRIVATE", "PRIVATE")):
            self.assertEqual(diagnostics.build_metadata()["state"], "unknown")
        with patch.object(diagnostics.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "a" * 40 + "\n", "PRIVATE")):
            self.assertEqual(diagnostics.build_metadata()["commit"], "a" * 40)

    def test_humble_inventory_distinguishes_armhf_userspace_from_arm64_kernel(self):
        jetson = {"os": "Linux", "distribution": "ubuntu", "distribution_version": "22.04",
                  "kernel_machine": "aarch64", "userspace_bits": 64, "userspace_abi": "aarch64-linux-gnu",
                  "python": {"version": "3.10.12"}}
        self.assertEqual(diagnostics.humble_compatibility(jetson)["state"], "compatible")
        pi = dict(jetson, distribution="debian", distribution_version="12", userspace_bits=32,
                  python={"version": "3.11.2"})
        self.assertEqual(diagnostics.humble_compatibility(pi)["state"], "incompatible")
        self.assertEqual(diagnostics.humble_compatibility(dict(jetson, userspace_bits=32))["state"], "incompatible")
        self.assertEqual(diagnostics.humble_compatibility(dict(jetson, userspace_abi="x86_64-linux-gnu"))["state"], "incompatible")
        self.assertEqual(diagnostics.humble_compatibility(dict(jetson, python={"version": "3.11.2"}))["state"], "incompatible")
        self.assertEqual(diagnostics.humble_compatibility({})["state"], "unknown")
        self.assertFalse(diagnostics.humble_compatibility(jetson)["runtime_tested"])

    def test_remote_projection_drops_personal_keys_and_arbitrary_messages(self):
        secret = "PRIVATE-REMOTE-CANARY"
        value = {"ok": True, "reason": secret, "inputs": {secret: {"available": True}},
                 "model": "/home/" + secret, "audio": {"player": secret}, "detail": secret}
        rendered = json.dumps(readiness._public_metadata(value))
        self.assertNotIn(secret, rendered)
        self.assertIn("reported detail withheld", rendered)

    def test_empty_default_on_emote_retains_runtime_semantics(self):
        item = diagnostics.flag("PET_EMOTE", diagnostics.FLAGS["PET_EMOTE"], {"PET_EMOTE": ""})
        self.assertTrue(item["requested"])
        self.assertTrue(item["specified"])


if __name__ == "__main__":
    unittest.main()
