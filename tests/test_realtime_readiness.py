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
    def run_report(self, health, *, audio_error=None):
        client = MagicMock(spec=["base_url", "health", "get_status", "__enter__", "__exit__"])
        client.base_url = "http://localhost:8000"
        client.__enter__.return_value = client
        client.health.return_value = health
        client.get_status.return_value = CommandResponse(ok=True, action=Action.GET_STATUS,
                                                        status=RobotStatus())
        voice = MagicMock(spec=["health"])
        voice.health.return_value = {"enabled": False, "reason": "audio disabled"}
        response = httpx.Response(200, json={"played": False, "checks": {}},
                                  request=httpx.Request("POST", client.base_url + AUDIO_SELF_TEST_PATH))
        output = io.StringIO()
        with (patch.object(readiness, "RobotClient", return_value=client) as factory,
              patch.object(readiness, "Voice", return_value=voice) as voice_factory,
              patch.object(readiness.httpx, "post", return_value=response, side_effect=audio_error) as post,
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
        voice_factory.assert_called_once()
        # Readiness never invokes a speech/effect/model worker; only static health.
        self.assertEqual([call[0] for call in voice.mock_calls], ["health"])
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

    def test_later_diagnostic_failure_preserves_compatibility_explanation(self):
        code, report = self.run_report({}, audio_error=httpx.ConnectError("audio diagnostics unavailable"))
        self.assertEqual(code, 1)
        self.assertIn("Update Pi and Jetson/brain together", report["failure_reason"])
        self.assertIn("audio diagnostics unavailable", report["failure_reason"])


if __name__ == "__main__":
    unittest.main()
