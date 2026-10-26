"""Encoder selection tests use vendor/process fakes; no camera is opened."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from robot.camera import PiCamera
from robot.camera_encoder import hardware_mjpeg


class V4L2:
    pass


class Hardware(V4L2):
    __module__ = "picamera2.encoders.mjpeg_encoder"

    def __init__(self, bitrate):
        self.bitrate = bitrate


class HardwareSelectionTests(unittest.TestCase):
    def modules(self, capability=True, encoder=Hardware):
        return {"picamera2.encoders": SimpleNamespace(_hw_encoder_available=capability),
                "picamera2.encoders.mjpeg_encoder": SimpleNamespace(__name__="picamera2.encoders.mjpeg_encoder", MJPEGEncoder=encoder),
                "picamera2.encoders.v4l2_encoder": SimpleNamespace(V4L2Encoder=V4L2)}

    def test_selects_direct_hardware_implementation_and_explicit_bitrate(self):
        with patch("robot.camera_encoder.import_module", side_effect=self.modules().__getitem__):
            encoder, bitrate = hardware_mjpeg("4000000")
        self.assertIsInstance(encoder, Hardware)
        self.assertEqual(encoder.bitrate, bitrate)

    def test_missing_false_or_nonboolean_capability_never_uses_public_alias(self):
        for capability in (False, None, "true", 1):
            with self.subTest(capability=capability), patch("robot.camera_encoder.import_module", side_effect=self.modules(capability).__getitem__) as importer:
                with self.assertRaisesRegex(RuntimeError, "advertise hardware"):
                    hardware_mjpeg(4_000_000)
                self.assertEqual(importer.call_count, 1)

    def test_software_alias_or_unrelated_class_is_rejected(self):
        class Software:
            pass
        for module in ("picamera2.encoders.libav_mjpeg_encoder", "picamera2.encoders.mjpeg_encoder"):
            Software.__module__ = module
            with self.subTest(module=module), patch("robot.camera_encoder.import_module", side_effect=self.modules(encoder=Software).__getitem__):
                with self.assertRaises(RuntimeError):
                    hardware_mjpeg(4_000_000)

    def test_unavailable_api_is_not_substituted(self):
        with patch("robot.camera_encoder.import_module", side_effect=ImportError("unsupported installed version")):
            with self.assertRaises(ImportError):
                hardware_mjpeg(4_000_000)

    def test_invalid_bitrate_never_imports_vendor_hardware(self):
        for value in (True, 0, -1, 999999, 12000001, 4e6, None, "NaN", "4e6"):
            with self.subTest(value=value), patch("robot.camera_encoder.import_module") as importer:
                with self.assertRaises(ValueError):
                    hardware_mjpeg(value)
                importer.assert_not_called()


class CameraProfileTests(unittest.TestCase):
    def start(self, camera, hardware=None):
        hardware = hardware if hardware is not None else Mock()
        driver = Mock(camera_controls={"FrameDurationLimits": ()})
        with patch("robot.camera._PICAMERA2_AVAILABLE", True), \
             patch("robot.camera.Picamera2", return_value=driver) as constructor, \
             patch("robot.camera.JpegEncoder", create=True) as software, \
             patch("robot.camera.FileOutput", create=True), \
             patch("robot.camera.hardware_mjpeg", hardware):
            camera.start()
        self.addCleanup(camera.stop)
        return driver, constructor, software

    def test_constructor_and_software_default_do_not_probe_hardware(self):
        hardware = Mock(side_effect=AssertionError("hardware not selected"))
        with patch("robot.camera.hardware_mjpeg", hardware):
            camera = PiCamera()
            self.assertEqual(camera.capture_settings["encoder"], "not started")
        driver, _, software = self.start(camera, hardware)
        software.assert_called_once_with(q=80)
        hardware.assert_not_called()
        self.assertEqual(driver.create_video_configuration.call_args.kwargs["main"], {"size": (640, 480)})

    def test_hardware_profile_keeps_single_capture_no_queue_and_no_quality_equivalence(self):
        encoder = Hardware(4_000_000)
        hardware = Mock(return_value=(encoder, 4_000_000))
        camera = PiCamera(960, 720, fps=15, quality=90, encoder="mjpeg_hw")
        driver, _, software = self.start(camera, hardware)
        kwargs = driver.create_video_configuration.call_args.kwargs
        self.assertEqual(kwargs["main"], {"size": (960, 720), "format": "YUV420"})
        self.assertFalse(kwargs["queue"])
        driver.start_recording.assert_called_once()
        self.assertIs(driver.start_recording.call_args.args[0], encoder)
        software.assert_not_called()
        self.assertFalse(camera.capture_settings["quality_applied"])
        self.assertFalse(camera.capture_settings["hardware_benchmark_verified"])
        self.assertEqual(camera.capture_settings["requested_bitrate_bps"], 4_000_000)
        self.assertFalse(camera.health().available, "no frame means no capture health")

    def test_unsupported_hardware_fails_before_camera_construction_and_no_software_fallback(self):
        hardware = Mock(side_effect=RuntimeError("hardware not supported"))
        camera = PiCamera(encoder="mjpeg_hw")
        _, constructor, software = self.start(camera, hardware)
        constructor.assert_not_called()
        software.assert_not_called()
        self.assertIn("hardware not supported", camera.health().failure_reason)

    def test_recording_failure_closes_camera_and_never_reports_selected_encoder(self):
        camera = PiCamera(encoder="mjpeg_hw")
        driver = Mock(camera_controls={})
        driver.start_recording.side_effect = RuntimeError("V4L2 unavailable")
        with patch("robot.camera._PICAMERA2_AVAILABLE", True), patch("robot.camera.Picamera2", return_value=driver), patch("robot.camera.FileOutput", create=True), patch("robot.camera.hardware_mjpeg", return_value=(Hardware(4_000_000), 4_000_000)):
            camera.start()
        driver.close.assert_called_once()
        self.assertIsNone(camera._picam2)
        self.assertEqual(camera.capture_settings["encoder"], "not started")
        self.assertIn("V4L2 unavailable", camera.health().failure_reason)

    def test_failed_software_start_never_claims_quality_was_applied(self):
        camera = PiCamera()
        driver = Mock(camera_controls={})
        driver.start_recording.side_effect = RuntimeError("recording failed")
        with patch("robot.camera._PICAMERA2_AVAILABLE", True), patch("robot.camera.Picamera2", return_value=driver), patch("robot.camera.JpegEncoder", create=True), patch("robot.camera.FileOutput", create=True):
            camera.start()
        self.assertEqual(camera.capture_settings["encoder"], "not started")
        self.assertFalse(camera.capture_settings["quality_applied"])
        self.assertFalse(camera.health().available)
        driver.close.assert_called_once()

    def test_invalid_mode_or_hardware_dimensions_are_unavailable(self):
        for kwargs in ({"encoder": "auto"}, {"encoder": "mjpeg_hw", "width": 4096},
                       {"encoder": "mjpeg_hw", "height": 721}, {"encoder": "mjpeg_hw", "fps": 60}):
            with self.subTest(kwargs=kwargs):
                camera = PiCamera(**kwargs)
                _, constructor, software = self.start(camera)
                constructor.assert_not_called()
                software.assert_not_called()
                self.assertFalse(camera.health().available)

    def test_explicit_simulation_never_imports_or_starts_hardware_profile(self):
        camera = PiCamera(simulate=True, encoder="mjpeg_hw")
        with patch("robot.camera.hardware_mjpeg") as hardware, patch("robot.camera.Picamera2") as driver, patch.object(camera, "_run_synthetic"):
            camera.start()
            camera.stop()
        hardware.assert_not_called()
        driver.assert_not_called()
        self.assertIn("synthetic", camera.capture_settings["encoder"])


if __name__ == "__main__":
    unittest.main()
