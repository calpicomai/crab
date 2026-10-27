"""Adapter diagnostics/CLI tests; actual generated messages and DDS tested separately."""
from dataclasses import fields, replace
import os
from pathlib import Path
import subprocess
import sys
import unittest

from brain.ros_observer.core import age_for_publish
from brain.ros_observer.node import diagnostics_for, to_ros
from tests import test_ros_observer as fixtures


class NodeDiagnosticsTests(unittest.TestCase):
    def snapshot(self):
        return fixtures.ConverterTests().convert()

    def test_startup_failure_and_untrusted_are_distinct(self):
        health = {"running": True, "poll_hz": 1}
        first = diagnostics_for(None, health, "")[0]
        self.assertEqual(first.level, 1)
        self.assertIn("first", first.message)
        failed = diagnostics_for(None, health, "observation HTTP status 503")[0]
        self.assertEqual(failed.level, 2)
        self.assertIn("503", failed.message)
        untrusted = replace(self.snapshot(), context=replace(self.snapshot().context, timing_verified=False))
        self.assertIn("unverified", diagnostics_for(untrusted, health, "")[0].message)
        self.assertEqual(diagnostics_for(None, {"running":False}, "")[0].level, 2)

    def test_available_used_and_verified_are_independent(self):
        diagnostics = diagnostics_for(self.snapshot(), {"running":True}, "")
        self.assertEqual(diagnostics[0].level, 0)
        item = next(x for x in diagnostics if x.name == "crab/input/ultrasonic")
        values = dict(item.values)
        self.assertEqual(values["available"], "True")
        self.assertEqual(values["used"], "False")
        self.assertEqual(values["physically_verified"], "unknown")
        self.assertIn("no command authority", diagnostics[0].message)

    def test_aged_last_snapshot_is_diagnostic_only_and_never_current(self):
        value = age_for_publish(self.snapshot(), 13.)
        diagnostics = diagnostics_for(value, {"running":True}, "")
        self.assertEqual(diagnostics[0].level, 1)
        self.assertIn("stale", diagnostics[0].message)
        self.assertEqual(next(x for x in diagnostics if x.name == "crab/input/ultrasonic").level, 1)
        failed = diagnostics_for(value, {"running":True}, "deadline exceeded")
        self.assertEqual(failed[0].level, 2)

    def test_missing_host_and_failed_input_never_show_healthy(self):
        value = self.snapshot()
        value = replace(value, pi=replace(value.pi, host_valid=False),
            inputs=(replace(value.inputs[0], failure_reason="no echo", available=False),))
        diagnostics = diagnostics_for(value, {"running":True}, "")
        self.assertEqual(next(x for x in diagnostics if x.name == "crab/host/pi").level, 1)
        self.assertEqual(next(x for x in diagnostics if x.name == "crab/input/ultrasonic").level, 2)

    def test_recursive_mapping_contains_only_contract_fields(self):
        value = self.snapshot()
        types = {}
        for item in (value, value.context, value.pi, value.inputs[0]):
            types[type(item).__name__] = type(type(item).__name__, (), {"__slots__":tuple(x.name for x in fields(item))})
        message = to_ros(value, types)
        self.assertEqual(message.companion_context.sequence, value.companion_context.sequence)
        self.assertEqual(message.inputs[0].context.captured_at, value.inputs[0].context.captured_at)
        self.assertIsInstance(message.inputs, list)
        self.assertFalse(hasattr(message, "last_heard"))

    def test_help_has_no_ros_runtime_dependency_and_invalid_poll_fails_before_io(self):
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE":"1"}
        command = [sys.executable, "-m", "brain.ros_observer.node"]
        result = subprocess.run(command+["--help"], cwd=root, env=env, text=True, capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--base-url", result.stdout)
        for rate in ("6", "nan", "0"):
            result = subprocess.run(command+["--base-url","http://127.0.0.1:1","--poll-hz",rate],
                cwd=root, env=env, text=True, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 2)
            self.assertIn("poll_hz", result.stderr)
