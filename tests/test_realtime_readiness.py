"""Readiness checks advertise protocol support without taking control ownership."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import httpx

from brain.pet import readiness
from shared import AUDIO_SELF_TEST_PATH, Action, CommandResponse, RobotStatus
from shared.realtime import PROTOCOL_VERSION, WS_PATH


class RealtimeReadinessTests(unittest.TestCase):
    def run_report(self, health, *, audio_error=None, health_error=None, status_error=None, status_ok=True):
        client = MagicMock(spec=["base_url", "health", "get_status", "__enter__", "__exit__"])
        client.base_url = "http://localhost:8000"
        client.__enter__.return_value = client
        client.health.return_value = health
        client.health.side_effect = health_error
        client.get_status.side_effect = status_error
        client.get_status.return_value = CommandResponse(ok=status_ok, action=Action.GET_STATUS,
                                                        status=RobotStatus())
        response = httpx.Response(200, json={"played": False, "checks": {}},
                                  request=httpx.Request("POST", client.base_url + AUDIO_SELF_TEST_PATH))
        output = io.StringIO()
        with (patch.object(readiness, "RobotClient", return_value=client) as factory,
              patch.object(readiness, "httpx", httpx),
              patch.object(httpx, "post", return_value=response, side_effect=audio_error) as post,
              patch("brain.pet.voice.Voice", side_effect=AssertionError("must not construct active Voice")) as voice_factory,
              patch("brain.realtime.RealtimeClient", side_effect=AssertionError("must not open a control session")) as realtime,
              patch("brain.realtime.connect", side_effect=AssertionError("must not connect WebSocket")) as connect,
              contextlib.redirect_stdout(output)):
            code = readiness.main(["--base-url", client.base_url])
        factory.assert_called_once_with(client.base_url, timeout=3)
        client.health.assert_called_once_with()
        client.get_status.assert_called_once_with()
        realtime.assert_not_called()
        connect.assert_not_called()
        post.assert_called_once_with(client.base_url + AUDIO_SELF_TEST_PATH, json={"play": False},
                                     timeout=8, trust_env=False, follow_redirects=False)
        voice_factory.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["jetson_audio"]["runtime_tested"])
        return code, json.loads(output.getvalue())

    def advertisement(self, **changes):
        return {"available": True, "path": WS_PATH, "protocol_version": PROTOCOL_VERSION, **changes}

    def test_compatible_advertisement_is_read_only_and_not_live_validation(self):
        code, report = self.run_report({"realtime": self.advertisement()})
        self.assertEqual(code, 0)
        self.assertTrue(report["realtime"]["compatible"])
        self.assertFalse(report["realtime"]["live_connection_tested"])
        self.assertFalse(report["hardware_verified"])
        self.assertNotIn("failure_reason", report)

    def test_old_server_without_realtime_requires_paired_update(self):
        code, report = self.run_report({"ok": True})
        self.assertEqual(code, 1)
        self.assertFalse(report["realtime"]["compatible"])
        self.assertIn("missing", report["failure_reason"])
        self.assertIn("Update Pi and Jetson/brain together", report["failure_reason"])
        self.assertIn("audio_diagnostics", report)

    def test_unavailable_wrong_version_and_wrong_path_fail_closed(self):
        for changes in ({"available": False}, {"available": "true"},
                        {"protocol_version": PROTOCOL_VERSION + 1},
                        {"protocol_version": str(PROTOCOL_VERSION)},
                        {"protocol_version": True}, {"path": "/wrong-path"}):
            with self.subTest(changes=changes):
                code, report = self.run_report({"realtime": self.advertisement(**changes)})
                self.assertEqual(code, 1)
                self.assertFalse(report["realtime"]["compatible"])
                self.assertIn("matching Crab release", report["failure_reason"])

    def test_health_failure_does_not_hide_independent_status_audio_or_local_diagnostics(self):
        code, report = self.run_report({}, health_error=httpx.ConnectError("health fixture unavailable"))
        self.assertEqual(code,1)
        self.assertIn("status",report)
        self.assertIn("audio_diagnostics",report)
        self.assertIn("jetson_audio",report)
        self.assertEqual(report["checks"]["robot_health"]["state"],"error")
        self.assertEqual(report["checks"]["robot_status"]["state"],"completed")
        self.assertFalse(report["realtime"]["live_connection_tested"])
        self.assertFalse(report["hardware_verified"])

    def test_rejected_status_is_not_a_successful_readiness_result(self):
        code, report=self.run_report({"realtime":self.advertisement()},status_ok=False)
        self.assertEqual(code,1)
        self.assertEqual(report["checks"]["robot_status"]["state"],"error")
        self.assertIn("audio_diagnostics",report)

    def test_status_failure_preserves_health_compatibility_and_audio(self):
        code, report = self.run_report({"realtime":self.advertisement()}, status_error=httpx.ReadTimeout("status fixture timed out"))
        self.assertEqual(code,1)
        self.assertTrue(report["realtime"]["compatible"])
        self.assertIn("audio_diagnostics",report)
        self.assertEqual(report["checks"]["robot_status"]["state"],"error")
        self.assertEqual(report["checks"]["audio_diagnostics"]["state"],"completed")
        self.assertIn("ReadTimeout",report["failure_reason"])

    def test_later_diagnostic_failure_preserves_compatibility_explanation(self):
        code, report = self.run_report({}, audio_error=httpx.ConnectError("audio diagnostics unavailable"))
        self.assertEqual(code, 1)
        self.assertIn("Update Pi and Jetson/brain together", report["failure_reason"])
        self.assertIn("ConnectError", report["failure_reason"])

    def test_remote_error_messages_and_unexpected_fields_are_redacted(self):
        secret = "PRIVATE-READINESS-CANARY"
        code, report = self.run_report({"realtime": self.advertisement(), "reason": secret,
                                       "identity": {"name": secret}, "model_path": "/private/" + secret},
                                      status_error=httpx.ConnectError("http://user:" + secret + "@private.local"),
                                      audio_error=httpx.ReadTimeout(secret))
        self.assertEqual(code, 1)
        self.assertNotIn(secret, json.dumps(report))
        self.assertTrue(report["realtime"]["compatible"])

    def test_nonfinite_health_cannot_leak_a_partial_advertisement_into_json(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            for location in ("protocol_version", "unrelated_field"):
                with self.subTest(value=value, location=location):
                    health = {"realtime": self.advertisement()}
                    if location == "protocol_version":
                        health["realtime"][location] = value
                    else:
                        health[location] = value
                    code, report = self.run_report(health)
                    self.assertEqual(code, 1)
                    self.assertNotIn("robot", report)
                    self.assertIsNone(report["realtime"]["advertised"])
                    self.assertFalse(report["realtime"]["compatible"])
                    self.assertEqual(report["checks"]["robot_health"]["state"], "error")
                    self.assertEqual(report["checks"]["realtime_advertisement"]["state"], "not_tested")
                    self.assertEqual(report["checks"]["robot_status"]["state"], "completed")
                    self.assertEqual(report["checks"]["audio_diagnostics"]["state"], "completed")
                    self.assertIn("ValueError", report["failure_reason"])
                    json.dumps(report, allow_nan=False)

    def test_standalone_advertisement_projection_also_rejects_nonfinite_scalars(self):
        report = readiness.realtime_readiness({"realtime": self.advertisement(protocol_version=float("nan"))})
        self.assertFalse(report["compatible"])
        self.assertIsNone(report["advertised"]["protocol_version"])
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
