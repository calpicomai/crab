"""Latest-frame transport contracts, using fake capture and in-memory JPEGs."""
import io
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from brain.perception.camera import MjpegCamera, _MAX_BUFFER_BYTES
from brain.perception.engine import PerceptionEngine
from robot.camera import PiCamera, _FrameBuffer
from shared.health import InputHealth


def part(value, sequence, captured_at=None):
    encoded = io.BytesIO()
    Image.new("RGB", (8, 8), (value, value, value)).save(encoded, "JPEG")
    stamp = time.time() if captured_at is None else captured_at
    header = (f"--frame\r\nContent-Type: image/jpeg\r\nX-Crab-Captured-At: {stamp}\r\n"
              f"X-Crab-Provenance: simulated\r\nX-Crab-Sequence: {sequence}\r\n\r\n")
    return header.encode() + encoded.getvalue() + b"\r\n"


class PiLatestFrameTests(unittest.TestCase):
    def camera(self):
        camera = PiCamera(simulate=True, fps=1000)
        camera._failure = None
        self.addCleanup(camera.stop)
        return camera

    def test_snapshot_keeps_matching_metadata_when_new_capture_arrives(self):
        camera = self.camera()
        camera._buffer.write(b"first")
        original = camera._buffer.snapshot
        first = original()

        def capture_during_read(*args, **kwargs):
            result = original(*args, **kwargs)
            camera._buffer.write(b"second")
            return result

        with patch.object(camera._buffer, "snapshot", side_effect=capture_during_read):
            selected = camera.get_snapshot()
        self.assertEqual(selected, first)
        self.assertEqual(selected.data, b"first")
        self.assertEqual(selected.sequence, 1)
        self.assertEqual(camera.get_frame(timeout=0), b"second")

    def test_slow_consumer_skips_all_intermediate_captures(self):
        camera = self.camera()
        camera._buffer.write(b"first")
        stream = camera.mjpeg_frames()
        self.assertIn(b"X-Crab-Sequence: 1\r\n", next(stream))
        for index in range(2, 102):
            camera._buffer.write(str(index).encode())
        newest = next(stream)
        self.assertIn(b"X-Crab-Sequence: 101\r\n", newest)
        self.assertTrue(newest.endswith(b"\r\n\r\n101\r\n"))
        stream.close()

    def test_stream_waits_for_new_capture_without_resending_or_busy_polling(self):
        camera = self.camera()
        camera._buffer.write(b"one")
        stream = camera.mjpeg_frames()
        next(stream)
        received, ready = [], threading.Event()

        def consume():
            received.append(next(stream))
            ready.set()

        with patch.object(camera._buffer._cond, "wait", wraps=camera._buffer._cond.wait) as wait:
            thread = threading.Thread(target=consume, daemon=True)
            thread.start()
            self.assertFalse(ready.wait(.05), "duplicate capture was sent")
            self.assertGreaterEqual(wait.call_count, 1)
            self.assertLessEqual(wait.call_count, 2)
            camera._buffer.write(b"two")
            self.assertTrue(ready.wait(1))
            thread.join(timeout=1)
        self.assertIn(b"X-Crab-Sequence: 2\r\n", received[0])
        stream.close()

    def test_stop_wakes_capture_waiter(self):
        buffer = _FrameBuffer()
        done = threading.Event()
        result = []

        def wait():
            result.append(buffer.snapshot(timeout=10, after_sequence=0))
            done.set()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        buffer.stop()
        self.assertTrue(done.wait(1))
        thread.join(timeout=1)
        self.assertEqual(result, [None])

    def test_advertised_rate_control_is_configured_using_vendor_microseconds(self):
        for advertised in (True, False):
            with self.subTest(advertised=advertised):
                driver = Mock()
                driver.camera_controls = {"FrameDurationLimits": (100, 1000000, 33333)} if advertised else {}
                camera = PiCamera(fps=10)
                with patch("robot.camera._PICAMERA2_AVAILABLE", True), \
                     patch("robot.camera.Picamera2", return_value=driver), \
                     patch("robot.camera.JpegEncoder", create=True), \
                     patch("robot.camera.FileOutput", create=True):
                    camera.start()
                kwargs = driver.create_video_configuration.call_args.kwargs
                self.assertFalse(kwargs["queue"])
                self.assertEqual(kwargs["controls"], {"FrameDurationLimits": (100000, 100000)} if advertised else {})
                self.assertFalse(camera.capture_settings["achieved_fps_verified"])
                camera.stop()


class JetsonLatestFrameTests(unittest.TestCase):
    def reader(self):
        with patch("brain.perception.camera.threading.Thread"):
            reader = MjpegCamera("http://127.0.0.1:1/camera/stream")
        self.addCleanup(reader.close)
        return reader

    def test_batch_decodes_only_newest_complete_frame_and_keeps_partial_next(self):
        reader = self.reader()
        first, second, third = part(30, 1), part(120, 2), part(200, 3)
        split = third.index(b"\xff\xd8") + 20
        with patch.object(Image, "open", wraps=Image.open) as decode:
            remainder = reader._extract_frames(first + second + third[:split], Image)
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(int(reader.read()[0, 0, 0]), 120)
            reader._extract_frames(remainder + third[split:], Image)
            self.assertEqual(decode.call_count, 2)
            self.assertEqual(int(reader.read()[0, 0, 0]), 200)

    def test_duplicates_and_expired_network_backlog_are_not_decoded(self):
        reader = self.reader()
        newest = part(100, 1)
        with patch.object(Image, "open", wraps=Image.open) as decode:
            reader._extract_frames(newest, Image)
            health = reader.health()
            reader._extract_frames(newest, Image)
            reader._extract_frames(part(30, 2, captured_at=time.time() - 3), Image)
            self.assertEqual(decode.call_count, 1)
            self.assertEqual(reader.health().captured_at, health.captured_at)

    def test_pending_decoder_slot_is_bounded_and_replaces_old_frames(self):
        reader = self.reader()
        for sequence in range(1, 101):
            reader._extract_frames(part(sequence, sequence), enqueue=True)
        self.assertEqual(reader._pending.sequence, 100)
        self.assertIsNone(reader.read())
        reader._decode(reader._pending, Image)
        self.assertEqual(int(reader.read()[0, 0, 0]), 100)

    def test_slow_decode_does_not_block_receiving_newest_pending_frame(self):
        reader = self.reader()
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        decoded = []
        original_decode = reader._decode

        def slow_first(packet, image_module):
            if not entered.is_set():
                entered.set()
                release.wait(1)
            original_decode(packet, image_module)
            decoded.append(packet.sequence)
            if len(decoded) == 2:
                published.set()

        reader._decode = slow_first
        decoder = threading.Thread(target=reader._run_decoder, daemon=True)
        reader._decoder_thread = decoder
        decoder.start()
        try:
            reader._extract_frames(part(1, 1), enqueue=True)
            self.assertTrue(entered.wait(1))
            for sequence in range(2, 51):
                reader._extract_frames(part(sequence, sequence), enqueue=True)
            self.assertEqual(reader._pending.sequence, 50)
            release.set()
            self.assertTrue(published.wait(1))
            self.assertEqual(decoded, [1, 50])
            self.assertEqual(int(reader.read()[0, 0, 0]), 50)
        finally:
            release.set()
            reader.close()

    def test_late_decoder_cannot_restore_frame_after_disconnect(self):
        reader = self.reader()
        reader._extract_frames(part(100, 1), enqueue=True)
        in_flight = reader._pending
        reader._invalidate("network disconnected")
        reader._decode(in_flight, Image)
        self.assertIsNone(reader.read())
        self.assertEqual(reader.health().failure_reason, "network disconnected")

    def test_incomplete_buffer_has_a_fixed_limit(self):
        reader = self.reader()
        with self.assertRaisesRegex(ValueError, "8 MiB"):
            reader._extract_frames(b"\xff\xd8" + b"x" * _MAX_BUFFER_BYTES, Image)

    def test_engine_uses_atomic_frame_and_health_pair(self):
        health = InputHealth(available=True, provenance="simulated", captured_at=time.time())
        camera = Mock(width=8, height=8)
        camera.read_snapshot.return_value = (np.zeros((8, 8, 3), dtype=np.uint8), health, 4)
        camera.read.side_effect = AssertionError("separate frame read would race metadata")
        camera.health.side_effect = AssertionError("separate health read would race capture")
        with patch("brain.perception.engine.MjpegCamera", return_value=camera):
            engine = PerceptionEngine(simulate=False, backend_names=[])
        snapshot = engine.detect()
        self.assertEqual(snapshot.inputs["camera"].captured_at, health.captured_at)
        camera.read_snapshot.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
