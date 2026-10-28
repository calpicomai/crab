"""Read-only MJPEG timing contracts; no physical capture or model loading."""
import io
import json
import math
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
import numpy as np

from brain.perception.camera import MjpegCamera
from brain.perception.engine import PerceptionEngine
from brain.pet.coordinator import Coordinator
from shared.clock import CaptureClock


def part(stamp, sequence=1, value=100, provenance="real"):
    image = io.BytesIO()
    Image.new("RGB", (2, 2), (value, value, value)).save(image, "JPEG")
    return (f"--frame\r\nX-Crab-Captured-At: {stamp}\r\n"
            f"X-Crab-Sequence: {sequence}\r\nX-Crab-Provenance: {provenance}\r\n\r\n").encode() + image.getvalue()


class PerceptionClockTests(unittest.TestCase):
    def reader(self, offset=.03, *, boot="boot-a"):
        with patch("brain.perception.camera.threading.Thread"):
            reader = MjpegCamera("http://127.0.0.1:1/camera/stream")
        reader._accept_clock({"server_at": 100.005 + offset, "server_boot_id": boot},
                             100., 10., 100.01, 10.01)
        reader._bind_stream({"X-Crab-Boot-Id": boot, "X-Crab-Provenance": "real"})
        self.addCleanup(reader.close)
        return reader

    def test_both_small_offset_directions_preserve_original_capture_and_receipt(self):
        for offset in (.03, -.03):
            with self.subTest(offset=offset), patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
                reader = self.reader(offset)
                stamp = 100.015 + offset
                reader._extract_frames(part(stamp), Image)
                frame, health, _ = reader.read_snapshot()
                self.assertIsNotNone(frame)
                self.assertEqual(health.captured_at, stamp)
                self.assertEqual(health.received_at, 100.02)
                self.assertIsInstance(health.clock, CaptureClock)
                self.assertTrue(health.usable())

    def test_uncalibrated_frames_are_unavailable(self):
        with patch("brain.perception.camera.threading.Thread"):
            reader = MjpegCamera("http://127.0.0.1:1/camera/stream")
        self.addCleanup(reader.close)
        reader._extract_frames(part(100), Image)
        self.assertIsNone(reader.read())
        self.assertIn("clock", reader.health().failure_reason)

    def test_probe_and_stream_boot_must_match(self):
        reader = self.reader()
        with self.assertRaisesRegex(ValueError, "boot"):
            reader._bind_stream({"X-Crab-Boot-Id": "other"})

    def test_legacy_stream_without_boot_is_explicitly_incompatible(self):
        reader = self.reader()
        with self.assertRaisesRegex(ValueError, "boot"):
            reader._bind_stream({})

    def test_excessive_offset_uncertainty_and_malformed_probes_clear_old_health(self):
        cases = (
            ({"server_at": 100.5, "server_boot_id": "boot-a"}, 100., 10., 100.01, 10.01),
            ({"server_at": 100.1, "server_boot_id": "boot-a"}, 100., 10., 100.3, 10.3),
            ({"server_at": math.nan, "server_boot_id": "boot-a"}, 100., 10., 100.01, 10.01),
            ({"server_at": 100.03}, 100., 10., 100.01, 10.01),
            ({"server_at": 100.03, "server_boot_id": []}, 100., 10., 100.01, 10.01),
        )
        for args in cases:
            with self.subTest(args=args):
                reader = self.reader()
                with self.assertRaises(ValueError):
                    reader._accept_clock(*args)
                self.assertIsNone(reader._clock)
                self.assertIsNone(reader.read())

    def test_replay_out_of_order_and_delayed_samples_do_not_refresh_capture(self):
        with patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
            reader = self.reader()
            reader._extract_frames(part(100.04, 5), Image)
            original = reader.health()
            for stamp, sequence in ((100.04, 5), (100.041, 4), (100.039, 6), (97., 10)):
                reader._extract_frames(part(stamp, sequence), Image)
                self.assertEqual(reader.health().captured_at, original.captured_at)
                self.assertEqual(reader.health().received_at, original.received_at)
                self.assertEqual(reader._sequence, 1)

    def test_future_and_malformed_observations_fail_without_new_decode(self):
        cases = ((100.2, 2), (math.nan, 2), (math.inf, 2), (100.041, 0), (100.041, 2**63))
        for stamp, sequence in cases:
            with self.subTest(stamp=stamp, sequence=sequence), patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
                reader = self.reader()
                reader._extract_frames(part(100.04), Image)
                with patch.object(Image, "open", wraps=Image.open) as decode:
                    reader._extract_frames(part(stamp, sequence), Image)
                    self.assertEqual(decode.call_count, 0)
                self.assertIsNone(reader.read())
                self.assertIn("invalid camera", reader.health().failure_reason)

    def test_local_clock_step_and_anchor_expiry_cannot_produce_fresh_frames(self):
        for wall, mono in ((100.05, 10.02), (106.02, 16.02), (100.0, 10.02)):
            with self.subTest(wall=wall, mono=mono):
                reader = self.reader()
                with patch("time.time", return_value=wall), patch("time.monotonic", return_value=mono):
                    reader._extract_frames(part(100.04), Image)
                    self.assertIsNone(reader.read())

    def test_same_boot_remote_clock_step_invalidates_queued_decoder(self):
        reader = self.reader()
        with patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
            reader._extract_frames(part(100.04, 10), enqueue=True)
            old = reader._pending
            with self.assertRaisesRegex(ValueError, "stepped"):
                reader._accept_clock({"server_at": 100.12, "server_boot_id": "boot-a"},
                                     100.02, 10.02, 100.03, 10.03)
            reader._decode(old, Image)
            self.assertIsNone(reader.read())

    def test_reconnect_recalibrates_and_keeps_same_boot_replay_watermark(self):
        reader = self.reader()
        with patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
            reader._extract_frames(part(100.04, 10), enqueue=True)
            old = reader._pending
            reader._invalidate("disconnected")
            self.assertIsNone(reader._clock)
            reader._accept_clock({"server_at": 100.055, "server_boot_id": "boot-a"},
                                 100.02, 10.02, 100.03, 10.03)
            reader._bind_stream({"X-Crab-Boot-Id": "boot-a"})
        with patch("time.time", return_value=100.04), patch("time.monotonic", return_value=10.04):
            reader._decode(old, Image)
            self.assertIsNone(reader.read())
            reader._extract_frames(part(100.04, 10), Image)
            self.assertIsNone(reader.read())
            reader._extract_frames(part(100.065, 11), Image)
            self.assertIsNotNone(reader.read())

    def test_new_boot_requires_reconnect_and_then_accepts_reset_sequence(self):
        reader = self.reader()
        with patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
            reader._extract_frames(part(100.04, 100), enqueue=True)
            old = reader._pending
            with self.assertRaisesRegex(ValueError, "boot"):
                reader._accept_clock({"server_at": 100.055, "server_boot_id": "boot-b"},
                                     100.02, 10.02, 100.03, 10.03)
            self.assertIsNone(reader._clock)
            reader._accept_clock({"server_at": 100.055, "server_boot_id": "boot-b"},
                                 100.02, 10.02, 100.03, 10.03)
            reader._bind_stream({"X-Crab-Boot-Id": "boot-b"})
        with patch("time.time", return_value=100.04), patch("time.monotonic", return_value=10.04):
            reader._decode(old, Image)
            self.assertIsNone(reader.read())
            reader._extract_frames(part(100.065, 1), Image)
            self.assertIsNotNone(reader.read())

    def test_slow_decoder_cannot_extend_capture_age_or_replace_receipt(self):
        reader = self.reader()
        with patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
            reader._extract_frames(part(100.04), enqueue=True)
            old = reader._pending
        with patch("time.time", return_value=102.03), patch("time.monotonic", return_value=12.03):
            with patch.object(Image, "open", wraps=Image.open) as decode:
                reader._decode(old, Image)
                self.assertEqual(decode.call_count, 0)
            self.assertIsNone(reader.read())

    def test_close_fences_late_probes_and_decode(self):
        reader = self.reader()
        with patch("time.time", return_value=100.02), patch("time.monotonic", return_value=10.02):
            reader._extract_frames(part(100.04), enqueue=True)
            old = reader._pending
            reader.close()
            with self.assertRaisesRegex(ValueError, "stopped"):
                reader._accept_clock({"server_at": 100.055, "server_boot_id": "boot-a"},
                                     100.02, 10.02, 100.03, 10.03)
            reader._decode(old, Image)
            self.assertIsNone(reader.read())

    def test_clock_http_body_encoding_and_deadline_are_bounded(self):
        import httpx
        invalid = (
            httpx.Response(200, content=b"x"*4097),
            httpx.Response(200, headers={"Content-Length": "4097"}, content=b"{}"),
            httpx.Response(200, headers={"Content-Encoding": "custom"}, content=b"{}"),
            httpx.Response(200, content=b'{"server_at":NaN,"server_boot_id":"boot-a"}'),
        )
        for response in invalid:
            with self.subTest(headers=response.headers):
                reader = self.reader()
                def handle(request):
                    self.assertEqual(str(request.url), "http://127.0.0.1:1/sim/clock")
                    return response
                with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                    with self.assertRaises(ValueError):
                        reader._probe_clock(client)

    def test_clock_http_elapsed_budget_and_cancellation_reject_response(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                reader = self.reader()
                now = [10.02]
                def chunks(**kwargs):
                    if cancel:
                        reader.close()
                    else:
                        now[0] += .201
                    yield b'{"server_at":100.03,"server_boot_id":"boot-a"}'
                response = SimpleNamespace(headers={}, raise_for_status=lambda: None,
                                           is_stream_consumed=False, stream=chunks())
                class Client:
                    def stream(self, method, url, **kwargs):
                        self.arguments = method, url, kwargs
                        return nullcontext(response)
                client = Client()
                with patch("time.monotonic", side_effect=lambda: now[0]), patch("time.time", return_value=100.02):
                    with self.assertRaisesRegex(ValueError, "deadline|stopped"):
                        reader._probe_clock(client)
                self.assertEqual(client.arguments[0], "GET")
                self.assertEqual(client.arguments[2]["timeout"], .2)

    def test_tiny_raw_chunks_cannot_defer_deadline_or_cancellation_checks(self):
        import httpx
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                reader = self.reader()
                now, consumed = [10.02], [0]
                class Drip(httpx.SyncByteStream):
                    def __iter__(self):
                        for _ in range(2000):
                            consumed[0] += 1
                            now[0] += .1
                            if cancel:
                                reader._stop.set()
                            yield b" "
                with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip()))) as client:
                    with patch("time.monotonic", side_effect=lambda: now[0]), patch("time.time", return_value=100.02):
                        with self.assertRaisesRegex(ValueError, "deadline|stopped"):
                            reader._probe_clock(client)
                self.assertLessEqual(consumed[0], 3)

    def test_empty_raw_chunks_have_a_fixed_count_limit(self):
        import httpx
        reader = self.reader()
        consumed = [0]
        class Empty(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(5000):
                    consumed[0] += 1
                    yield b""
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Empty()))) as client:
            with self.assertRaisesRegex(ValueError, "chunk"):
                reader._probe_clock(client)
        self.assertLessEqual(consumed[0], 4097)

    def test_engine_preserves_clock_through_interpretation_and_coordinator_expiry(self):
        for offset in (.03, -.03):
            for delay in (0., 3.):
                with self.subTest(offset=offset, inference_delay=delay):
                    now = [100.02, 10.02]
                    reader = self.reader(offset)
                    robot = SimpleNamespace(stop=lambda: {"ok": True, "action": "stop"})
                    voice = SimpleNamespace(stop=lambda: None, health=lambda: {"available": False})
                    coordinator = Coordinator(robot, voice, clock=lambda: now[1], wall_clock=lambda: now[0])
                    self.addCleanup(coordinator.close)
                    with patch("time.time", side_effect=lambda: now[0]), patch("time.monotonic", side_effect=lambda: now[1]):
                        stamp = 100.015 + offset
                        reader._extract_frames(part(stamp), Image)
                        captured_health = reader.health()
                        def detect(frame):
                            now[0] += delay
                            now[1] += delay
                            return []
                        engine = object.__new__(PerceptionEngine)
                        engine.camera = reader
                        engine._backends = {"yolo": SimpleNamespace(name="yolo", detect=detect)}
                        engine._backend_failures = {}
                        engine._frame_id, engine.simulate = 0, False
                        result = engine.detect()
                        self.assertEqual(result.inputs["interpretation"].captured_at, stamp)
                        self.assertEqual(result.inputs["interpretation"].clock, captured_health.clock)
                        self.assertEqual(result.inputs["camera"].received_at, 100.02)
                        for name in ("camera", "interpretation"):
                            self.assertEqual(result.inputs[name].usable(), delay == 0)
                        coordinator.observe({"boot_id": "fake-pi", "observed_at": now[0]},
                                            snapshot=result.model_dump(mode="json"))
                        for name in ("camera", "interpretation"):
                            self.assertEqual(coordinator.inputs[name]["used"], delay == 0)
                            self.assertEqual(coordinator.inputs[name]["fresh"], delay == 0)
                        self.assertIsNone(coordinator.target)


class PerceptionClockWireTests(unittest.TestCase):
    def test_actual_http_stream_and_clock_get_with_positive_skew(self):
        requests = []
        finish = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                if self.path == "/sim/clock":
                    body = json.dumps({"server_at": time.time()+.03, "server_boot_id": "loopback-boot"}).encode()
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/camera/stream":
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("X-Crab-Boot-Id", "loopback-boot")
                    self.send_header("X-Crab-Provenance", "simulated")
                    self.end_headers()
                    try:
                        for sequence in range(1, 200):
                            if finish.is_set():
                                break
                            self.wfile.write(part(time.time()+.02, sequence, provenance="simulated"))
                            self.wfile.flush()
                            if finish.wait(.02):
                                break
                    except (BrokenPipeError, ConnectionResetError):
                        pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        owner = threading.Thread(target=server.serve_forever, daemon=True)
        owner.start()
        reader = MjpegCamera(f"http://127.0.0.1:{server.server_port}/camera/stream")
        try:
            deadline = time.monotonic()+3
            while time.monotonic() < deadline and (reader.read() is None or requests.count("/sim/clock") < 2):
                time.sleep(.01)
            frame, health, sequence = reader.read_snapshot()
            self.assertIsNotNone(frame, health.failure_reason)
            self.assertGreater(sequence, 0)
            self.assertTrue(health.fresh())
            self.assertEqual(health.provenance, "simulated")
            self.assertFalse(health.usable())
            self.assertIsInstance(health.clock, CaptureClock)
            self.assertGreaterEqual(requests.count("/sim/clock"), 2)
            self.assertLessEqual(requests.count("/sim/clock"), 4)
            self.assertEqual(requests.count("/camera/stream"), 1)
            self.assertEqual(set(requests), {"/sim/clock", "/camera/stream"})
        finally:
            finish.set()
            reader.close()
            reader._thread.join(timeout=1)
            server.shutdown()
            server.server_close()
            owner.join(timeout=1)
        self.assertFalse(reader._thread.is_alive())
        self.assertIsNone(reader.read())

    def test_pi_camera_responses_advertise_existing_boot_without_capture(self):
        with patch.dict("os.environ", {"PICRAWLER_SIMULATE": "1", "PICRAWLER_CAMERA_ENABLED": "0",
                                      "PICRAWLER_AUDIO_ENABLED": "0", "PICRAWLER_ULTRASONIC_ENABLED": "0",
                                      "PICRAWLER_BATTERY_ENABLED": "0"}):
            from robot import server
        from unittest.mock import Mock
        camera = Mock(simulate=True)
        camera.mjpeg_frames.return_value = iter(())
        camera.get_snapshot.return_value = SimpleNamespace(data=b"synthetic", captured_at=time.time(),
                                                          sequence=1, provenance="simulated")
        with patch.object(server, "camera", camera):
            self.assertEqual(server.camera_stream().headers["X-Crab-Boot-Id"], server.leases.boot_id)
            self.assertEqual(server.camera_frame().headers["X-Crab-Boot-Id"], server.leases.boot_id)


if __name__ == "__main__":
    unittest.main()
