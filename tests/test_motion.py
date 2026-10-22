"""Motion contracts: all drivers are fakes and actuator calls are disabled."""
import threading
import unittest
from unittest.mock import Mock, patch

from robot import config
from robot.gait import GaitEngine, Pose


class MotionTests(unittest.TestCase):
    def engine(self):
        engine = GaitEngine(simulate=True)
        engine.battery_fn = lambda: 7.4
        engine.clearance_fn = lambda: 100.0
        return engine

    def test_missing_library_does_not_create_fake_healthy_robot(self):
        with patch("robot.gait._PICRAWLER_AVAILABLE", False), patch.object(config, "ACTUATORS_ENABLED", True):
            engine = GaitEngine(simulate=False)
            engine.battery_fn = lambda: 7.4
            self.assertFalse(engine.simulate)
            self.assertFalse(engine.enabled)
            self.assertFalse(engine.stand().ok)

    def test_disabled_hardware_does_not_even_construct_driver(self):
        with patch("robot.gait.Picrawler") as driver, patch.object(config, "ACTUATORS_ENABLED", False):
            engine = GaitEngine(simulate=False)
            self.assertFalse(engine.enabled)
            driver.assert_not_called()

    def test_unknown_clearance_and_battery_fail_closed(self):
        for reading in (None, float("nan"), float("inf"), -1, 0):
            with self.subTest(reading=reading):
                engine = self.engine()
                engine.clearance_fn = lambda: reading
                self.assertFalse(engine.walk(1).ok)
                self.assertEqual(engine.motion_state()["sequence"], 0)
                engine.clearance_fn = lambda: 100.0
                engine.battery_fn = lambda: reading
                self.assertFalse(engine.turn_steps("left").ok)
                self.assertFalse(engine.sit().ok)

    def test_reader_exception_is_unknown(self):
        engine = self.engine()
        engine.clearance_fn = Mock(side_effect=OSError("disconnected"))
        result = engine.walk(1)
        self.assertFalse(result.ok)
        self.assertIn("unavailable", result.reason)
        self.assertTrue(engine.reflex_stopped)

    def test_low_battery_caps_all_primitives_and_cannot_be_raised(self):
        engine = self.engine()
        engine.battery_fn = lambda: (config.BATTERY_CRITICAL_V + config.BATTERY_LOW_V) / 2
        for run in (lambda: engine.walk(1, 100), lambda: engine.turn_steps("right", speed=100),
                    lambda: engine.test_leg(0, 100), engine.stand, engine.sit):
            result = run()
            self.assertTrue(result.ok, result.reason)
            self.assertLessEqual(result.applied_speed, config.BATTERY_LOW_SPEED)
        engine.battery_fn = lambda: config.BATTERY_CRITICAL_V - .1
        self.assertFalse(engine.sit().ok)  # even social/rest motion obeys limits

    def test_caller_cannot_disable_reflex(self):
        engine = self.engine()
        engine.clearance_fn = lambda: config.REFLEX_STOP_CM - 1
        with patch.object(config, "REFLEX_ENABLED", False):
            self.assertFalse(engine.walk(1, min_clearance_cm=0).ok)

    def test_precise_turn_rejected_and_coarse_simulator_is_labeled(self):
        engine = self.engine()
        engine.world = Mock()
        for angle in (1, 15, 180, -90):
            self.assertFalse(engine.turn(angle).ok)
        engine.world.rotate.assert_not_called()
        result = engine.turn_steps("left")
        self.assertTrue(result.ok)
        self.assertEqual(result.completed_cycles, 1)
        self.assertEqual(result.estimated_turn_degrees, -config.SIM_TURN_CYCLE_DEG)
        self.assertEqual(result.estimate_provenance, "idealized_simulation")
        engine.world.rotate.assert_called_once_with(-config.SIM_TURN_CYCLE_DEG)
        self.assertTrue(engine.turn(0).ok)

    def test_real_cycle_does_not_claim_measured_angle(self):
        # A mock object, never the vendor driver or actual hardware.
        with patch("robot.gait.Picrawler", return_value=Mock()), patch("robot.gait._PICRAWLER_AVAILABLE", True), \
             patch.object(config, "ACTUATORS_ENABLED", True):
            engine = GaitEngine(False)
            engine.battery_fn = lambda: 7.4
            engine.clearance_fn = lambda: 100.0
            result = engine.turn_steps("right")
            self.assertTrue(result.ok)
            self.assertIsNone(result.estimated_turn_degrees)
            self.assertEqual(result.estimate_provenance, "unmeasured")
            engine._crawler.do_action.assert_called_once_with("turn right", 1, min(50, config.MAX_MOTION_SPEED))

    def test_enabled_hardware_initialization_waits_for_fresh_safe_inputs(self):
        for battery, distance in ((None, 100), (6.0, 100), (7.4, None), (7.4, 5)):
            with self.subTest(battery=battery, distance=distance), \
                 patch("robot.gait.Picrawler") as driver, patch("robot.gait._PICRAWLER_AVAILABLE", True), \
                 patch.object(config, "ACTUATORS_ENABLED", True):
                engine = GaitEngine(False)
                driver.assert_not_called()
                engine.battery_fn = lambda: battery
                engine.clearance_fn = lambda: distance
                self.assertFalse(engine.walk(1).ok)
                self.assertFalse(engine.turn_steps("right").ok)
                self.assertFalse(engine.stand().ok)
                driver.assert_not_called()
                self.assertEqual(engine.motion_state()["sequence"], 0)
                self.assertFalse(engine.motion_state()["initialization_executed"])

    def test_initialization_honors_stricter_requested_clearance(self):
        with patch("robot.gait.Picrawler") as driver, patch("robot.gait._PICRAWLER_AVAILABLE", True), \
             patch.object(config, "ACTUATORS_ENABLED", True):
            engine = GaitEngine(False)
            engine.battery_fn = lambda: 7.4
            engine.clearance_fn = lambda: 30
            self.assertFalse(engine.walk(1, min_clearance_cm=50).ok)
            driver.assert_not_called()

    def test_initialization_is_accounted_once_without_claiming_pose(self):
        with patch("robot.gait.Picrawler", return_value=Mock()) as driver, \
             patch("robot.gait._PICRAWLER_AVAILABLE", True), patch.object(config, "ACTUATORS_ENABLED", True):
            engine = GaitEngine(False)
            self.assertEqual(engine.get_status().pose, Pose.UNKNOWN)
            driver.assert_not_called()
            engine.battery_fn = lambda: 7.4
            engine.clearance_fn = lambda: 100
            first = engine.walk(1)
            self.assertTrue(first.ok)
            self.assertTrue(first.initialization_executed)
            self.assertEqual(first.sequence, 2)  # vendor init plus one gait cycle
            second = engine.walk(1)
            self.assertTrue(second.ok)
            self.assertFalse(second.initialization_executed)
            self.assertEqual(second.sequence, 3)
            driver.assert_called_once()

    def test_failed_initialization_is_uncertain_and_cannot_auto_retry(self):
        with patch("robot.gait.Picrawler", side_effect=OSError("partial initialization")) as driver, \
             patch("robot.gait._PICRAWLER_AVAILABLE", True), patch.object(config, "ACTUATORS_ENABLED", True):
            engine = GaitEngine(False)
            engine.battery_fn = lambda: 7.4
            engine.clearance_fn = lambda: 100
            result = engine.walk(1)
            self.assertFalse(result.ok)
            self.assertTrue(result.initialization_executed)
            self.assertTrue(result.movement_uncertain)
            self.assertEqual(result.sequence, 1)
            self.assertEqual(result.completed_cycles, 0)
            self.assertFalse(engine.enabled)
            engine.resume()
            self.assertFalse(engine.walk(1).ok)
            driver.assert_called_once()

    def test_stop_during_initialization_prevents_requested_gait(self):
        fake_driver = Mock()
        def constructor():
            engine.stop()
            return fake_driver
        with patch("robot.gait.Picrawler", side_effect=constructor), \
             patch("robot.gait._PICRAWLER_AVAILABLE", True), patch.object(config, "ACTUATORS_ENABLED", True):
            engine = GaitEngine(False)
            engine.battery_fn = lambda: 7.4
            engine.clearance_fn = lambda: 100
            result = engine.walk(1)
            self.assertFalse(result.ok)
            self.assertTrue(result.cancelled)
            self.assertTrue(result.initialization_executed)
            self.assertEqual(result.sequence, 1)
            self.assertEqual(result.completed_cycles, 0)
            self.assertEqual(engine.get_status().pose, Pose.UNKNOWN)
            fake_driver.do_action.assert_not_called()

    def test_stop_does_not_wait_for_vendor_and_no_old_commands_queue(self):
        engine = self.engine()
        entered, release = threading.Event(), threading.Event()
        original = engine._do_action
        def blocked(action, speed):
            entered.set()
            self.assertTrue(release.wait(2))
            original(action, speed)
        engine._do_action = blocked
        results = []
        worker = threading.Thread(target=lambda: results.append(engine.walk(1)))
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertFalse(engine.stand().ok)
        self.assertTrue(engine.stop().ok)
        self.assertFalse(engine.resume().ok)
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].cancelled)
        self.assertEqual(results[0].completed_cycles, 1)
        self.assertEqual(results[0].sequence, 1)
        self.assertFalse(engine.walk(1).ok)
        self.assertTrue(engine.resume().ok)

    def test_interrupt_staged_pose_accounts_completed_leg(self):
        engine = self.engine()
        original = engine._do_single_leg
        def stop_after_first(leg, coord, speed):
            original(leg, coord, speed)
            engine.stop()
        engine._do_single_leg = stop_after_first
        result = engine.stand()
        self.assertFalse(result.ok)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.sequence, 1)
        self.assertEqual(engine.get_status().pose, Pose.UNKNOWN)

    def test_bounded_commands_rejected_before_motion(self):
        engine = self.engine()
        self.assertFalse(engine.walk(config.MAX_MOTION_CYCLES + 1).ok)
        self.assertFalse(engine.turn_steps("back").ok)
        self.assertFalse(engine.turn_steps("left", cycles=100).ok)
        self.assertFalse(engine.walk(1, mode="custom").ok)
        self.assertFalse(engine.walk(True).ok)
        self.assertFalse(engine.walk(1, speed=True).ok)
        self.assertEqual(engine.motion_state()["sequence"], 0)

    def test_vendor_error_invalidates_estimate_and_latches_stop(self):
        engine = self.engine()
        engine._do_action = Mock(side_effect=OSError("servo transport failed"))
        result = engine.walk(1)
        self.assertFalse(result.ok)
        self.assertTrue(result.movement_uncertain)
        self.assertEqual(result.sequence, 1)
        self.assertTrue(engine.motion_state()["stop_latched"])
        self.assertEqual(engine.get_status().pose, Pose.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
