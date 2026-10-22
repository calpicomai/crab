"""WebSocket input lifecycle and loss boundaries without network or hardware."""
import queue
import time
import unittest
from unittest.mock import patch

from brain.pet.inputs import InputWorker


class FakeLink:
    def __init__(self):
        self.started = self.closed = 0
        self.on_status = self.on_event = None

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def health(self):
        return {"transport": "websocket", "connected": True, "last_rtt_ms": 4}


class RealtimeInputsTests(unittest.TestCase):
    def make(self):
        from brain.pet.loop import EventInbox
        self.link = FakeLink()
        self.heard = EventInbox()
        worker = InputWorker("http://localhost:8000", "http://localhost:8100",
                             self.heard, use_camera=False, realtime=self.link)
        self.addCleanup(worker.close)
        return worker

    def test_push_mode_owns_link_and_never_polls_status_or_events(self):
        worker = self.make()
        with patch("brain.pet.inputs.RobotClient", side_effect=AssertionError("unexpected HTTP poll")), \
             patch("brain.pet.inputs.httpx.Client", side_effect=AssertionError("unexpected HTTP poll")):
            worker.start()
            worker.start()
            self.assertEqual(worker.threads, ())
            self.assertEqual(self.link.started, 1)
            self.assertEqual(worker.health()["transport"]["transport"], "websocket")
            self.link.on_event({"text": "hello", "timestamp": time.time(), "source": "dashboard"})
            self.assertEqual(self.heard.get_nowait()["text"], "hello")

    def test_status_retains_producer_capture_time(self):
        worker = self.make()
        worker.start()
        captured = time.time()-1
        status = {"observed_at": captured, "inputs": {
            "camera": {"captured_at": captured-1, "provenance": "simulated"}}}
        self.link.on_status(status, None)
        sample, perception, reason = worker.samples.get_nowait()
        self.assertEqual(sample, status)
        self.assertEqual(sample["inputs"]["camera"]["captured_at"], captured-1)
        self.assertIsNone(reason)
        self.assertIsNone(perception)

    def test_rapid_reconnect_cannot_erase_loss_or_replay_old_resume(self):
        worker = self.make()
        worker.start()
        self.heard.put_nowait({"text": "resume", "timestamp": time.time()})
        worker._snapshot = {"frame_id": "old-detector"}
        self.link.on_status(None, "connection dropped")
        self.link.on_status({"boot_id": "same", "observed_at": time.time()}, None)
        self.assertEqual(worker.failures.get_nowait(), "connection dropped")
        self.assertEqual(worker.samples.get_nowait()[0]["boot_id"], "same")
        self.assertIsNone(worker._snapshot)
        with self.assertRaises(queue.Empty):
            self.heard.get_nowait()

    def test_loss_preserves_urgent_stop_and_discards_ordinary_events(self):
        worker = self.make()
        worker.start()
        self.heard.put_nowait({"text": "stop", "timestamp": time.time()})
        self.heard.put_nowait({"text": "resume", "timestamp": time.time()})
        self.link.on_status(None, "heartbeat expired")
        self.assertEqual(self.heard.get_nowait()["text"], "stop")
        with self.assertRaises(queue.Empty):
            self.heard.get_nowait()

    def test_delayed_pre_stop_resume_is_ignored_after_stop_delivery(self):
        worker = self.make()
        worker.start()
        before = time.time()-.1
        self.link.on_event({"text": "stop", "timestamp": time.time(), "source": "dashboard"})
        self.assertEqual(self.heard.get_nowait()["text"], "stop")
        self.link.on_event({"text": "resume", "timestamp": before, "source": "dashboard"})
        with self.assertRaises(queue.Empty):
            self.heard.get_nowait()
        self.link.on_event({"text": "resume", "timestamp": time.time(), "source": "dashboard"})
        self.assertEqual(self.heard.get_nowait()["text"], "resume")

    def test_shutdown_discards_late_data_and_stale_events(self):
        worker = self.make()
        worker.start()
        for timestamp in (time.time()-6, time.time()+6, float("nan"), None):
            self.link.on_event({"text": "resume", "timestamp": timestamp, "source": "dashboard"})
        with self.assertRaises(queue.Empty):
            self.heard.get_nowait()
        worker.close()
        self.link.on_status({"observed_at": time.time()}, None)
        self.link.on_event({"text": "resume", "timestamp": time.time(), "source": "dashboard"})
        self.assertTrue(worker.samples.empty())
        with self.assertRaises(queue.Empty):
            self.heard.get_nowait()
        self.assertEqual(self.link.closed, 1)

    def test_new_epoch_drops_old_resume_but_keeps_fresh_engagement(self):
        worker = self.make()
        worker.start()
        self.link.on_status({"boot_id": "pi-one", "control_epoch": 3})
        self.link.on_event({"text": "resume", "timestamp": time.time(), "control_epoch": 3})
        self.link.on_status({"boot_id": "pi-one", "control_epoch": 4})
        self.link.on_event({"text": "resume", "timestamp": time.time(), "control_epoch": 3})
        with self.assertRaises(queue.Empty):
            self.heard.get_nowait()
        self.link.on_event({"text": "hello", "timestamp": time.time(), "control_epoch": 4})
        self.link.on_status({"boot_id": "pi-one", "control_epoch": 4})
        self.assertEqual(self.heard.get_nowait()["text"], "hello")

    def test_new_pi_boot_accepts_epoch_zero_and_discards_previous_boot_input(self):
        worker = self.make()
        worker.start()
        self.link.on_status({"boot_id": "pi-one", "control_epoch": 7})
        self.link.on_event({"text": "stop", "timestamp": time.time(), "control_epoch": 7})
        self.link.on_event({"text": "resume", "timestamp": time.time(), "control_epoch": 7})
        self.link.on_status({"boot_id": "pi-two", "control_epoch": 0})
        self.assertEqual(self.heard.get_nowait()["text"], "stop")
        with self.assertRaises(queue.Empty):
            self.heard.get_nowait()
        self.link.on_event({"text": "resume", "timestamp": time.time(), "control_epoch": 0})
        self.assertEqual(self.heard.get_nowait()["control_epoch"], 0)


if __name__ == "__main__":
    unittest.main()
