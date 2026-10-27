"""Stop/shutdown regressions with held RPCs; never contact a robot."""
import threading
import time
import unittest

from brain.pet.coordinator import Coordinator


class SilentVoice:
    busy = False
    enabled = False
    def stop(self):
        pass
    def health(self):
        return {"enabled": False}


class HeldResume:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = threading.Event()
        self.latch = False
        self.closed = False
        self.stop_calls = 0
        self.fences = 0
        self.resume_epochs = []
        self.boot_id = "one"

    def cancel_commands(self):
        self.fences += 1

    def resume(self, *, expected_control_epoch=None):
        self.resume_epochs.append(expected_control_epoch)
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("held test resume timed out")
        self.latch = False
        return {"ok": True, "action": "resume", "status": {
            "boot_id": self.boot_id, "observed_at": time.time(),
            "motion_state": {"stop_latched": False}}}

    def emergency_stop(self):
        self.stop_calls += 1
        self.latch = True
        self.stopped.set()
        return {"ok": True, "action": "stop", "detail": "fake stop latched"}

    def stop(self):
        if self.closed:
            raise RuntimeError("regular client already closed")
        return self.emergency_stop()

    def close(self):
        self.closed = True


def status(now):
    health = {"available": True, "provenance": "simulated", "captured_at": now, "max_age_s": 2}
    return {"simulate": True, "boot_id": "one", "observed_at": now, "battery_v": 7.5,
            "distance_cm": 90, "inputs": {"battery": health, "ultrasonic": health}}


class RealtimeStopTests(unittest.TestCase):
    def setUp(self):
        self.robot = HeldResume()
        self.c = Coordinator(self.robot, SilentVoice(), motion_enabled=True)
        self.c.observe(status(time.time()))

    def tearDown(self):
        self.robot.release.set()
        self.c.close()

    def start_resume(self):
        self.c.stopped = True
        intent = self.c.hear("resume")
        self.c.tick()
        self.assertTrue(self.robot.started.wait(.5))
        return intent

    def test_stop_does_not_wait_for_outstanding_resume(self):
        resume = self.start_resume()
        self.c.hear("stop")
        self.assertTrue(self.robot.stopped.wait(.3), "urgent stop waited behind the held resume")
        self.assertFalse(self.robot.release.is_set())
        self.assertTrue(self.c.stopped)
        self.assertTrue(self.robot.latch)
        self.assertEqual(resume.status.value, "cancelled")
        self.assertGreater(self.robot.fences, 0)
        self.robot.release.set()
        result = self.c._resume[1].result(timeout=1)
        self.assertTrue(result.cleanup_stop["ok"])
        self.assertTrue(self.robot.latch)
        self.c.tick()
        self.assertTrue(self.c.stopped)

    def test_close_cannot_lose_stop_behind_late_resume(self):
        self.start_resume()
        future = self.c._resume[1]
        started = time.monotonic()
        self.c.close()
        self.assertLess(time.monotonic() - started, .5)
        self.assertTrue(self.robot.stopped.is_set())
        self.assertFalse(future.done())
        self.robot.close()  # main closes normal transports after coordinator.close()
        self.robot.release.set()
        result = future.result(timeout=1)
        self.assertTrue(result.cleanup_stop["ok"])
        self.assertTrue(self.robot.latch, "late resume undid shutdown stop")
        self.assertGreaterEqual(self.robot.stop_calls, 2)
        self.assertTrue(any(item.get("outcome") == "unknown" for item in self.c.snapshot()["movements"]))

    def test_cancelled_late_error_with_confirmed_cleanup_does_not_issue_another_stop(self):
        def late_error(**_kwargs):
            self.robot.started.set()
            if not self.robot.release.wait(2):
                raise TimeoutError("held late-error fixture timed out")
            raise RuntimeError("late cancelled RPC failed")
        self.robot.resume = late_error
        self.start_resume()
        self.c.hear("stop")
        self.c._control[1].result(timeout=1)
        self.c.tick()  # consume the original STOP before the cancelled RPC returns
        self.assertIsNone(self.c._control)
        self.assertEqual(self.robot.stop_calls, 1)
        self.robot.release.set()
        outcome = self.c._resume[1].result(timeout=1)
        self.assertTrue(outcome.cleanup_stop["ok"])
        self.assertEqual(self.robot.stop_calls, 2)
        self.c.tick()
        if self.c._control:
            self.c._control[1].result(timeout=1)
        self.assertTrue(self.c.stopped)
        self.assertEqual(self.robot.stop_calls, 2, "confirmed late cleanup must not advance STOP again")

    def test_uncancelled_resume_does_not_issue_cleanup_stop(self):
        self.start_resume()
        self.robot.release.set()
        result = self.c._resume[1].result(timeout=1)
        self.assertIsNone(result.cleanup_stop)
        self.assertEqual(self.robot.stop_calls, 0)
        self.c.tick()
        self.assertFalse(self.c.stopped)

    def test_old_event_epoch_is_not_replaced_by_newer_status(self):
        self.c.stopped = True
        intent = self.c.hear("resume", source="dashboard", control_epoch=3)
        # A fresh status may arrive while the older event's intention waits.
        self.c.observe({**status(time.time()), "control_epoch": 4})
        self.c.tick()
        self.assertTrue(self.robot.started.wait(.5))
        self.assertEqual(intent.payload["control_epoch"], 3)
        self.assertEqual(self.robot.resume_epochs, [3])

    def test_disconnect_cancels_completed_resume_before_owner_polls_it(self):
        resume = self.start_resume()
        self.robot.release.set()
        self.c._resume[1].result(timeout=1)
        self.c.connection_lost("WebSocket disconnected")
        self.assertTrue(self.robot.stopped.wait(.3))
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(resume.status.value, "cancelled")
        self.assertTrue(self.robot.latch)

    def test_disconnect_cancels_pending_resume_while_already_stopped(self):
        self.c.stopped = True
        resume = self.c.hear("resume")
        self.c.connection_lost("WebSocket disconnected")
        self.c.tick()
        self.assertEqual(resume.status.value, "cancelled")
        self.assertFalse(self.robot.started.is_set())
        self.assertTrue(self.c.stopped)

    def test_new_pi_epoch_cancels_resume_completed_before_poll(self):
        self.c.observe({**status(time.time()), "control_epoch": 3})
        self.c.stopped = True
        resume = self.c.hear("resume", control_epoch=3)
        self.c.tick()
        self.assertTrue(self.robot.started.wait(.5))
        self.robot.release.set()
        self.c._resume[1].result(timeout=1)
        self.c.observe({**status(time.time()), "control_epoch": 4,
                        "motion_state": {"stop_latched": True}})
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(resume.status.value, "cancelled")
        self.assertEqual(self.robot.stop_calls, 0, "epoch observations must not create a stop feedback loop")

    def test_new_pi_epoch_preserves_explicit_resume_from_that_epoch(self):
        self.c.observe({**status(time.time()), "control_epoch": 3})
        self.c.stopped = True
        # New-epoch dashboard input may precede the corresponding status.
        resume = self.c.hear("resume", control_epoch=4)
        self.c.tick()
        self.assertTrue(self.robot.started.wait(.5))
        self.c.observe({**status(time.time()), "control_epoch": 4,
                        "motion_state": {"stop_latched": True}})
        self.robot.release.set()
        self.c._resume[1].result(timeout=1)
        self.c.tick()
        self.assertFalse(self.c.stopped)
        self.assertEqual(resume.status.value, "completed")
        self.assertEqual(self.robot.stop_calls, 0)

    def test_control_epoch_resets_with_pi_boot_identity(self):
        self.c.observe({**status(time.time()), "control_epoch": 90})
        self.c.observe({**status(time.time()), "boot_id": "two", "control_epoch": 0,
                        "motion_state": {"stop_latched": True}})
        self.robot.boot_id = "two"
        self.assertTrue(self.robot.stopped.wait(.3))
        self.c._control[1].result(timeout=1)
        self.c.tick()
        resume = self.c.hear("resume", control_epoch=0)
        self.c.tick()
        self.assertTrue(self.robot.started.wait(.5))
        self.c.observe({**status(time.time()), "boot_id": "two", "control_epoch": 0,
                        "motion_state": {"stop_latched": True}})
        self.robot.release.set()
        self.c._resume[1].result(timeout=1)
        self.c.tick()
        self.assertFalse(self.c.stopped)
        self.assertEqual(resume.status.value, "completed")
        self.assertEqual(self.robot.resume_epochs, [0])

    def test_delayed_status_receipt_does_not_refresh_producer_age(self):
        now = time.time()
        delayed = Coordinator(self.robot, SilentVoice(), motion_enabled=True)
        try:
            # Fresh sensor timestamps cannot refresh an old authoritative status.
            delayed.observe({**status(now), "observed_at": now - 3})
            self.assertGreater(delayed.clock() - delayed._status_at, 2.9)
            transport = delayed.snapshot()["inputs"]["transport"]
            self.assertFalse(transport["fresh"])
            self.assertAlmostEqual(transport["captured_at"], now - 3)
            self.assertIn("stale", delayed._motion_block())
            resume = delayed.hear("resume")
            delayed.tick()
            self.assertNotEqual(resume.status.value, "active")
            self.assertFalse(self.robot.started.is_set())
        finally:
            delayed.close()

    def test_transport_source_and_missing_timestamp_are_honest(self):
        other = Coordinator(self.robot, SilentVoice(), transport_source="robot WebSocket")
        try:
            sample = status(time.time())
            del sample["observed_at"]
            other.observe(sample)
            health = other.snapshot()["inputs"]["transport"]
            self.assertEqual(health["source"], "robot WebSocket")
            self.assertFalse(health["available"])
            self.assertIsNone(health["captured_at"])
            other.connection_lost("heartbeat expired")
            self.assertEqual(other.snapshot()["inputs"]["transport"]["source"], "robot WebSocket")
        finally:
            other.close()


class OrderedResume(HeldResume):
    def resume(self, *, expected_control_epoch=None):
        super().resume(expected_control_epoch=expected_control_epoch)
        return self.response


class ResumeOrderingTests(unittest.TestCase):
    """Explicit interleavings with virtual time, not timing sleeps or a network."""

    def setUp(self):
        self.wall, self.mono = 1000.0, 10.0
        self.robot = OrderedResume()
        self.c = Coordinator(self.robot, SilentVoice(), clock=lambda: self.mono,
                             wall_clock=lambda: self.wall)
        self.c.observe(self.sample(3, 1000))

    def tearDown(self):
        self.robot.release.set()
        self.c.close()

    def sample(self, epoch, stamp, *, latched=True, boot="one"):
        return {**status(stamp), "boot_id": boot, "control_epoch": epoch,
                "motion_state": {"stop_latched": latched}}

    def completed_resume(self, *, response=None):
        self.robot.response = response if response is not None else {
            "ok": True, "action": "resume", "status": self.sample(4, 1000.1, latched=False)}
        # Wire RobotStatus has no epoch; provenance comes from the original
        # explicit event and the Pi's validated lease, never current transport health.
        self.robot.response.get("status", {}).pop("control_epoch", None)
        intent = self.c.hear("resume", control_epoch=4)
        self.c.tick()
        self.assertTrue(self.robot.started.wait(.5))
        self.wall, self.mono = 1000.2, 10.2
        self.robot.release.set()
        self.c._resume[1].result(timeout=1)
        return intent

    def test_result_before_matching_status_does_not_relatch(self):
        resume = self.completed_resume()
        age_anchor = self.c._status_at
        self.c.tick()
        self.assertFalse(self.c.stopped)
        self.assertEqual(self.c._status["control_epoch"], 4)
        self.assertEqual(self.c._status_at, age_anchor, "RPC receipt must not refresh status age")
        # A poll captured before the acknowledgement cannot undo it.
        self.c.observe(self.sample(4, 1000.05))
        self.assertFalse(self.c.stopped)
        self.c.observe(self.sample(4, 1000.2, latched=False))
        self.assertFalse(self.c.stopped, "the acknowledged STOP epoch is not a new STOP")
        self.assertEqual(resume.status.value, "completed")

    def test_newer_matching_unlatched_status_before_result_is_valid(self):
        resume = self.completed_resume()
        self.c.observe(self.sample(4, 1000.2, latched=False))
        self.c.tick()
        self.assertFalse(self.c.stopped)
        self.assertEqual(resume.status.value, "completed")
        self.assertEqual(self.c._last_status_observed_at, 1000.2)

    def test_new_stop_wins_before_result_poll(self):
        resume = self.completed_resume()
        self.c.observe(self.sample(5, 1000.2))
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(self.c._status["control_epoch"], 5)
        self.assertEqual(resume.status.value, "cancelled")

    def test_new_stop_wins_after_result_poll(self):
        resume = self.completed_resume();self.c.tick()
        self.c.observe(self.sample(5, 1000.2))
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(self.c._status["control_epoch"], 5)
        self.assertEqual(resume.status.value, "completed")

    def test_lower_epoch_cannot_replace_acknowledged_epoch_or_refresh_age(self):
        self.completed_resume();self.c.tick()
        anchor = self.c._status_at
        self.c.observe(self.sample(3, 1000.3))
        self.assertEqual(self.c._status["control_epoch"], 4)
        self.assertEqual(self.c._status_at, anchor)
        self.assertFalse(self.c.stopped)
        self.c.observe(self.sample(5, 1000.4))
        self.assertTrue(self.c.stopped)

    def test_acknowledgement_cannot_defeat_status_expiry(self):
        self.completed_resume();self.c.tick()
        self.wall, self.mono = 1002.1, 12.1
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertIn("expired", self.c.blocked_reason)

    def test_newer_latched_status_cannot_be_cleared_by_older_ack(self):
        resume = self.completed_resume()
        self.c.observe(self.sample(4, 1000.2))
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(resume.status.value, "failed")

    def test_reboot_cancels_old_result_and_epoch_scope(self):
        resume = self.completed_resume()
        self.c.observe(self.sample(0, 1000.2, boot="two"))
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(resume.status.value, "cancelled")
        self.assertEqual(self.c._boot_id, "two")
        self.assertEqual(self.c._status["control_epoch"], 0)

    def assert_rejected_ack(self, **changes):
        response = {"ok": True, "action": "resume", "status": self.sample(4, 1000.1, latched=False)}
        response["status"].update(changes)
        resume = self.completed_resume(response=response)
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(resume.status.value, "failed")
        self.assertEqual(self.c._status["control_epoch"], 3)
        self.assertEqual(self.c._last_status_observed_at, 1000)
        self.assertTrue(self.robot.stopped.wait(.5), "uncertain resume must reassert STOP")

    def test_replayed_ack_is_not_an_epoch_acknowledgement(self):
        self.assert_rejected_ack(observed_at=1000)

    def test_stale_ack_is_not_an_epoch_acknowledgement(self):
        self.assert_rejected_ack(observed_at=997)

    def test_future_ack_cannot_extend_the_status_fence(self):
        self.assert_rejected_ack(observed_at=1001)

    def test_missing_ack_time_is_not_an_epoch_acknowledgement(self):
        self.assert_rejected_ack(observed_at=None)

    def test_wrong_boot_ack_cannot_clear_the_latch(self):
        self.assert_rejected_ack(boot_id="other")

    def test_malformed_latch_ack_cannot_clear_the_latch(self):
        self.assert_rejected_ack(motion_state={"stop_latched": 0})

    def test_failed_ack_never_creates_epoch_authority(self):
        resume = self.completed_resume(response={"ok":False,"action":"resume"})
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertEqual(self.c._status["control_epoch"], 3)
        self.assertEqual(resume.status.value, "failed")


if __name__ == "__main__":
    unittest.main()
