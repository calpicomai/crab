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

    def cancel_commands(self):
        self.fences += 1

    def resume(self, *, expected_control_epoch=None):
        self.resume_epochs.append(expected_control_epoch)
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("held test resume timed out")
        self.latch = False
        return {"ok": True, "action": "resume"}

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


if __name__ == "__main__":
    unittest.main()
