"""Dashboard delivery contracts without starting hardware or an HTTP listener."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from robot import config
from robot.camera import CameraFrame
from robot.telemetry import TelemetryStore


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        with patch.object(config, "SIMULATE", True), patch.object(config, "ACTUATORS_ENABLED", False):
            from robot import server
        self.server = server
        self.now = 100.0
        self.store = TelemetryStore(clock=lambda: self.now)
        for target, name, value in ((server, "_telemetry", self.store),
                                    (server, "sim_world", None)):
            active = patch.object(target, name, value)
            active.start()
            self.addCleanup(active.stop)

    def publish(self, sequence):
        return self.server.sim_brain({"_publisher_id": "fixture", "_sequence": sequence,
                                      "_publisher_started_at_ns": 1, "tick": sequence})

    def test_latest_jpeg_headers_match_returned_frame(self):
        camera = Mock()
        camera.get_snapshot.return_value = CameraFrame(b"jpeg1", 98.75, 42, "simulated")
        with patch.object(self.server, "camera", camera), patch.object(self.server.time, "time", return_value=100):
            response = self.server.camera_frame()
        self.assertEqual(response.body, b"jpeg1")
        self.assertEqual(response.headers["X-Crab-Sequence"], "42")
        self.assertEqual(response.headers["X-Crab-Captured-At"], "98.75")
        self.assertEqual(float(response.headers["X-Crab-Frame-Age-Ms"]), 1250)
        self.assertEqual(response.headers["X-Crab-Provenance"], "simulated")
        self.assertIn("no-store", response.headers["Cache-Control"])
        camera.get_snapshot.assert_called_once_with(timeout=0)
        camera.health.assert_not_called()

    def test_missing_or_stalled_camera_returns_uncacheable_failure(self):
        for camera in (None, Mock(get_snapshot=Mock(return_value=None))):
            with self.subTest(camera=camera), patch.object(self.server, "camera", camera):
                response = self.server.camera_frame()
                self.assertEqual(response.status_code, 503)
                self.assertIn("no-store", response.headers["Cache-Control"])

    def test_receipt_age_advances_without_new_telemetry(self):
        self.assertTrue(self.publish(1)["ok"])
        with patch.object(self.server.time, "time", return_value=101.5):
            response = self.server.sim_state()
        state = json.loads(response.body)
        self.assertEqual(state["telemetry_age_ms"], 1500)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["brain"]["tick"], 1)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_slow_event_consumer_gets_newest_snapshot_without_replay(self):
        async def consume():
            request = Mock(is_disconnected=AsyncMock(side_effect=[False, False, True]))
            self.publish(1)
            stream = self.server._dashboard_events(request)
            first = await anext(stream)
            for sequence in range(2, 51):
                self.publish(sequence)
            latest = await anext(stream)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
            return first, latest

        with patch.object(self.server.asyncio, "sleep", new=AsyncMock()):
            first, latest = asyncio.run(consume())
        self.assertTrue(first.startswith("event: state\ndata: "))
        self.assertEqual(json.loads(first.split("data: ", 1)[1])["brain"]["tick"], 1)
        self.assertEqual(json.loads(latest.split("data: ", 1)[1])["brain"]["tick"], 50)


if __name__ == "__main__":
    unittest.main()
