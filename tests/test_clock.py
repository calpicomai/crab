"""Clock interval arithmetic and producer provenance; virtual clocks, no I/O."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from shared.clock import CaptureClock, capture_age
from shared.health import InputHealth
from shared import RobotStatus
from shared.realtime import HelloMessage, PongMessage, StatusMessage, encode_message
from brain.realtime import RealtimeClient, RealtimeUnavailable


def anchor(offset=.030):
    return CaptureClock.measure(1000.006+offset, 1000., 10., 1000.012, 10.012)


class ClockBoundsTests(unittest.TestCase):
    def test_measured_30ms_both_directions_uses_upper_age_not_midpoint(self):
        for offset in (.030, -.030):
            sample = anchor(offset)
            self.assertAlmostEqual(sample.offset_lower_s, offset-.006)
            self.assertAlmostEqual(sample.offset_upper_s, offset+.006)
            age = capture_age(1000+offset, sample, wall=1000.012, mono=10.012)
            self.assertAlmostEqual(age, .018)
            # A receipt timestamp cannot refresh old capture evidence.
            health = InputHealth(available=True, provenance="real", captured_at=1000+offset,
                                 received_at=5000, clock=sample, max_age_s=1)
            self.assertTrue(health.usable(1000.012, mono=10.012))
            self.assertFalse(health.usable(1001.012, mono=11.012))
            self.assertEqual(health.captured_at, 1000+offset)

    def test_excessive_offset_uncertainty_and_malformed_probe_rejected(self):
        for offset in (-.5, .5, -4., 4.):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                anchor(offset)
        for remote in (None, True, "1000", float("nan"), float("inf"), 0, -1):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                CaptureClock.measure(remote, 1000., 10., 1000.012, 10.012)
        for wall, mono in ((1000.3, 10.3), (1000.5, 10.012), (1000., 9.9)):
            with self.assertRaises(ValueError):
                CaptureClock.measure(1000.1, 1000., 10., wall, mono)

    def test_future_malformed_and_expired_capture_is_unknown(self):
        for stamp in (1000.5, None, True, "1000", float("nan"), float("inf"), 0, -1):
            with self.subTest(stamp=stamp):
                self.assertIsNone(capture_age(stamp, anchor(), wall=1000.012, mono=10.012))
        self.assertIsNone(capture_age(1000.03, anchor(), wall=1006.012, mono=16.012))
        for step in (-1, 1):
            self.assertIsNone(capture_age(1000.03, anchor(), wall=1000.112+step, mono=10.112))
        self.assertIsNone(capture_age(1000.03, {"offset_upper_s": .03}, wall=1000.012, mono=10.012))

    def test_local_and_remote_clock_steps_fail_but_ordinary_drift_does_not(self):
        sample = anchor()
        for step in (-.100, .100):
            with self.subTest(remote_step=step), self.assertRaises(ValueError):
                CaptureClock.measure(1001.036+step, 1001., 11., 1001.012, 11.012, sample)
            with self.subTest(local_step=step), self.assertRaises(ValueError):
                CaptureClock.measure(1001.036, 1001.+step, 11., 1001.012+step, 11.012, sample)
        renewed = CaptureClock.measure(1001.0365, 1001., 11., 1001.012, 11.012, sample)
        self.assertIsNotNone(capture_age(1001.03, renewed, wall=1001.012, mono=11.012))

    def test_clock_mapping_does_not_make_failed_or_untrusted_sensor_usable(self):
        for changes in ({"available": False}, {"failure_reason": "no echo"},
                        {"provenance": "unknown"}, {"provenance": "simulated"}):
            data = dict(available=True, provenance="real", captured_at=1000.03, clock=anchor())
            health = InputHealth(**{**data, **changes})
            self.assertFalse(health.usable(1000.012, mono=10.012))


class RealtimeClockTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.mono = 1000., 10.
        self.time = SimpleNamespace(time=lambda: self.wall, monotonic=lambda: self.mono)
        for module in ("brain.realtime.time", "shared.clock.time"):
            p = patch(module, self.time)
            p.start()
            self.addCleanup(p.stop)
        self.changes = []
        self.link = RealtimeClient("http://localhost", on_status=lambda *args: self.changes.append(args))
        self.connection = SimpleNamespace(socket=SimpleNamespace(shutdown=lambda how: None))
        self.link._connection = self.connection
        self.ids = {"boot_id": "one", "session_id": "session"}
        self.receive(HelloMessage(**self.ids, control_epoch=0))

    def receive(self, message):
        self.link._receive(self.connection, encode_message(message))

    def status(self, stamp, sequence=1):
        self.receive(StatusMessage(**self.ids, control_epoch=0, sequence=sequence,
                                  status=RobotStatus(boot_id=self.ids["boot_id"], observed_at=stamp)))

    def calibrate(self, remote=1000.036):
        self.link._ping = ("probe", self.mono, self.wall)
        self.wall += .012
        self.mono += .012
        self.receive(PongMessage(**self.ids, request_id="probe", server_at=remote))

    def test_no_readiness_until_correlated_probe_and_original_capture_survives(self):
        self.status(1000.03)
        self.assertFalse(self.link._ready.is_set())
        self.receive(PongMessage(**self.ids, request_id="unmatched", server_at=1000.036))
        self.assertIsNone(self.link._clock)
        self.calibrate()
        self.assertTrue(self.link._ready.is_set())
        self.assertEqual(self.changes[0][0]["observed_at"], 1000.03)
        self.assertAlmostEqual(self.link.health()["status_capture_age_s"], .018)

    def test_initial_calibration_buffer_cannot_replay_an_older_status(self):
        self.status(1000.03, 10)
        self.status(999.8, 9)
        self.status(999.9, 10)
        self.calibrate()
        self.assertEqual(self.link._status_sequence, 10)
        self.assertEqual(self.changes[0][0]["observed_at"], 1000.03)

    def test_replays_out_of_order_and_repeated_capture_do_not_extend_deadline(self):
        self.calibrate()
        self.status(1000.03, 3)
        deadline = self.link._last_status_mono
        self.wall += .2
        self.mono += .2
        for stamp, sequence in ((1000.2, 2), (1000.03, 4), (1000.02, 5)):
            self.status(stamp, sequence)
            self.assertEqual(self.link._last_status_mono, deadline)
        self.wall += .8
        self.mono += .8
        with self.assertRaisesRegex(RealtimeUnavailable, "expired"):
            self.link._heartbeat(self.connection, 10.)

    def test_delayed_status_expires_by_capture_not_receipt(self):
        self.calibrate()
        self.status(999.14)  # ~.91 seconds old: only .09 seconds of life remain
        self.wall += .1
        self.mono += .1
        with self.assertRaisesRegex(RealtimeUnavailable, "expired"):
            self.link._heartbeat(self.connection, 10.)
        for stamp in (998., 1001., None):
            with self.subTest(stamp=stamp), self.assertRaises(RealtimeUnavailable):
                self.status(stamp, 2)

    def test_invalidation_discards_calibration_and_new_session_recalibrates(self):
        self.calibrate()
        self.status(1000.03)
        self.link._invalidate(self.connection, "disconnect")
        self.assertIsNone(self.link._clock)
        self.assertFalse(self.link._ready.is_set())
        self.assertIsNone(self.changes[-1][0])
        self.link._connection = self.connection
        self.link._hello = None
        self.ids = {"boot_id": "two", "session_id": "new"}
        self.receive(HelloMessage(**self.ids, control_epoch=0))
        self.status(999.99, 2)
        self.assertFalse(self.link._ready.is_set())
        self.calibrate(remote=1000.000)
        self.assertTrue(self.link._ready.is_set())

    def test_clock_step_blocks_dispatch_and_heartbeat(self):
        self.calibrate()
        self.status(1000.03)
        self.wall += 1
        with self.assertRaises(RealtimeUnavailable):
            self.link.command("resume", {})
        with self.assertRaisesRegex(RealtimeUnavailable, "clock"):
            self.link._heartbeat(self.connection, 10.)
        self.assertTrue(self.link._outgoing.empty())


if __name__ == "__main__":
    unittest.main()
