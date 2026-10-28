"""Fail-closed capture and transport tests: no real hardware is constructed."""
import io
import sys
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from shared.health import InputHealth
from robot.camera import PiCamera
from robot.sensors import BatterySensor, DistanceSensor
from brain.perception.camera import MjpegCamera
from brain.perception.engine import PerceptionEngine


class SensorHealthTests(unittest.TestCase):
    def test_freshness_provenance_and_receipt_do_not_replace_capture(self):
        health = InputHealth(available=True, provenance="real", captured_at=100, received_at=120)
        self.assertTrue(health.usable(101))
        self.assertFalse(health.usable(120))
        self.assertFalse(health.usable(99))
        health.provenance = "simulated"
        self.assertFalse(health.usable(101))
        self.assertTrue(health.usable(101, allow_simulated=True))
        health.provenance = "unknown"
        self.assertFalse(health.usable(101, allow_simulated=True))

    def test_missing_real_sonar_is_not_synthetic(self):
        with patch("robot.sensors._ULTRASONIC_AVAILABLE", False):
            sonar = DistanceSensor()
        self.assertIsNone(sonar.read_cm())
        self.assertFalse(sonar.simulate)
        self.assertFalse(sonar.health().available)
        self.assertIn("unavailable", sonar.health().failure_reason)

    def test_invalid_echoes_fail_closed_and_next_read_recovers(self):
        with patch("robot.sensors._ULTRASONIC_AVAILABLE", False):
            sonar = DistanceSensor()
        sonar._sonar = Mock()
        for value in (None, -1, -2, 0, float("nan"), float("inf")):
            sonar._sonar.read.return_value = value
            self.assertIsNone(sonar.read_cm())
            self.assertFalse(sonar.health().available)
        sonar._sonar.read.return_value = 23.5
        self.assertEqual(sonar.read_cm(), 23.5)
        self.assertTrue(sonar.health().usable())

    def test_battery_unsupported_never_guesses_adc(self):
        fake = types.ModuleType("robot_hat")
        fake.ADC = Mock()
        with patch.dict(sys.modules, {"robot_hat": fake, "robot_hat.device": None}):
            battery = BatterySensor()
        self.assertIsNone(battery.read_v())
        self.assertFalse(battery.simulate)
        fake.ADC.assert_not_called()

    def test_vendor_battery_failure_and_explicit_simulation(self):
        fake = types.ModuleType("robot_hat")
        fake.get_battery_voltage = Mock(side_effect=[7.4, OSError("ADC gone"), float("nan")])
        with patch.dict(sys.modules, {"robot_hat": fake}):
            battery = BatterySensor()
        self.assertEqual(battery.read_v(), 7.4)
        self.assertTrue(battery.health().usable())
        self.assertIsNone(battery.read_v())
        self.assertIn("ADC gone", battery.health().failure_reason)
        self.assertIsNone(battery.read_v())
        simulated = BatterySensor(simulate=True)
        self.assertEqual(simulated.read_v(), 7.6)
        self.assertEqual(simulated.health().provenance, "simulated")

    def test_missing_real_camera_and_failed_start_never_spawn_synthetic(self):
        with patch("robot.camera._PICAMERA2_AVAILABLE", False):
            camera = PiCamera()
            camera.start()
        self.assertIsNone(camera.get_frame(timeout=0))
        self.assertFalse(camera.simulate)
        self.assertIsNone(camera._thread)
        with patch("robot.camera._PICAMERA2_AVAILABLE", True), patch("robot.camera.Picamera2", side_effect=OSError("disconnected")):
            camera = PiCamera()
            camera.start()
        self.assertIsNone(camera.get_frame(timeout=0))
        self.assertIn("disconnected", camera.health().failure_reason)

    def stream_reader(self):
        with patch("brain.perception.camera.threading.Thread"):
            reader = MjpegCamera("http://127.0.0.1:1/camera/stream")
        wall, mono = time.time(), time.monotonic()
        reader._accept_clock({"server_at": wall, "server_boot_id": "test-boot"}, wall, mono, wall, mono)
        reader._bind_stream({"X-Crab-Boot-Id": "test-boot"})
        return reader

    def test_stream_start_and_dropout_never_generate_frame(self):
        reader = self.stream_reader()
        self.assertIsNone(reader.read())
        reader._store(np.zeros((2, 2, 3), dtype=np.uint8), captured_at=time.time(), provenance="real",
                      clock=reader._clock, boot_id="test-boot")
        self.assertIsNotNone(reader.read())
        reader._invalidate("network lost")
        self.assertIsNone(reader.read())
        self.assertEqual(reader.health().failure_reason, "network lost")
        self.assertFalse(reader.simulate)

    def test_mjpeg_metadata_survives_chunk_boundaries_and_stays_simulated(self):
        reader = self.stream_reader()
        image = io.BytesIO()
        Image.new("RGB", (2, 2)).save(image, "JPEG")
        stamp = time.time()
        header = (f"--frame\r\nContent-Type: image/jpeg\r\nX-Crab-Captured-At: {stamp}\r\n"
                  "X-Crab-Provenance: simulated\r\nX-Crab-Sequence: 1\r\n\r\n").encode()
        buf = reader._extract_frames(header[:20], Image)
        buf = reader._extract_frames(buf + header[20:] + image.getvalue()[:30], Image)
        reader._extract_frames(buf + image.getvalue()[30:], Image)
        self.assertIsNotNone(reader.read())
        self.assertEqual(reader.health().captured_at, stamp)
        self.assertEqual(reader.health().provenance, "simulated")
        self.assertFalse(reader.health().usable())
        self.assertTrue(reader.health().usable(allow_simulated=True))
        reader._health.captured_at = time.time() - 10
        self.assertIsNone(reader.read())

    def test_legacy_stream_without_timestamp_is_ignored(self):
        reader = self.stream_reader()
        image = io.BytesIO()
        Image.new("RGB", (2, 2)).save(image, "JPEG")
        reader._extract_frames(image.getvalue(), Image)
        self.assertIsNone(reader.read())
        self.assertIn("timestamp", reader.health().failure_reason)

    def test_failed_detector_does_not_fabricate_person_or_healthy_snapshot(self):
        with patch("brain.perception.camera.threading.Thread"), patch("brain.perception.engine.backends.build", side_effect=RuntimeError("model missing")):
            engine = PerceptionEngine(simulate=False, backend_names=["yolo"])
        self.assertEqual(engine.loaded_backends(), [])
        snapshot = engine.detect()
        self.assertFalse(snapshot.simulate)
        self.assertEqual(snapshot.detections, [])
        self.assertFalse(snapshot.inputs["interpretation"].available)
        self.assertIn("model missing", snapshot.inputs["detector:yolo"].failure_reason)
        engine.close()

    def test_pi_capture_through_transport_preserves_provenance_and_age(self):
        pi = PiCamera(simulate=True, width=40, height=40)
        pi._failure = None
        jpeg = io.BytesIO()
        Image.new("RGB", (40, 40), (240, 0, 0)).save(jpeg, "JPEG")
        pi._buffer.write(jpeg.getvalue())
        captured_at = pi.health().captured_at
        reader = self.stream_reader()
        reader._extract_frames(next(pi.mjpeg_frames()), Image)
        with patch("brain.perception.engine.MjpegCamera", return_value=reader):
            engine = PerceptionEngine(simulate=False, backend_names=["simblob"])
        snapshot = engine.detect()
        self.assertTrue(snapshot.simulate)
        self.assertTrue(snapshot.detections)
        for name in ("camera", "interpretation"):
            self.assertEqual(snapshot.inputs[name].captured_at, captured_at)
            self.assertEqual(snapshot.inputs[name].provenance, "simulated")
            self.assertFalse(snapshot.inputs[name].usable())
        reader._health.captured_at -= 10
        stale = engine.detect()
        self.assertEqual(stale.detections, [])
        self.assertFalse(stale.inputs["interpretation"].available)
        engine.close()


if __name__ == "__main__":
    unittest.main()
