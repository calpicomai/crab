"""Interruption and worker-lifecycle regressions with fake input/model only."""
import queue
import threading
import time
import unittest

from brain.pet.brain import PetThought
from brain.pet.loop import EventInbox, MindWorker


class InboxTests(unittest.TestCase):
    def event(self, text):
        return {"text": text, "timestamp": time.time(), "source": "dashboard"}

    def test_old_resume_cannot_follow_a_new_urgent_stop(self):
        inbox = EventInbox()
        inbox.put_nowait(self.event("resume"))
        inbox.put_nowait(self.event("hello"))
        inbox.put_nowait(self.event("stop"))
        self.assertEqual(inbox.get_nowait()["text"], "stop")
        with self.assertRaises(queue.Empty):
            inbox.get_nowait()

    def test_only_resume_submitted_after_stop_can_follow_it(self):
        inbox = EventInbox()
        inbox.put_nowait(self.event("resume"))
        inbox.put_nowait(self.event("stop"))
        inbox.put_nowait(self.event("resume"))
        self.assertEqual(inbox.get_nowait()["text"], "stop")
        self.assertEqual(inbox.get_nowait()["text"], "resume")
        with self.assertRaises(queue.Empty):
            inbox.get_nowait()

    def test_burst_cannot_evict_stop(self):
        inbox = EventInbox()
        inbox.put_nowait(self.event("stop"))
        for _ in range(100):
            inbox.put_nowait(self.event("hello"))
        self.assertEqual(inbox.get_nowait()["text"], "stop")


class MindLifecycleTests(unittest.TestCase):
    class Brain:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.closed = threading.Event()

        def reflect(self, *args):
            self.entered.set()
            self.release.wait(2)
            return PetThought("late response")

        def close(self):
            self.closed.set()

    def test_closing_busy_mind_discards_response_and_closes_adapter(self):
        brain = self.Brain()
        worker = MindWorker(brain, None)
        worker.submit(1, {}, "calm", "")
        try:
            self.assertTrue(brain.entered.wait(.5))
            worker.close()
        finally:
            brain.release.set()
            worker.thread.join(1)
        self.assertFalse(worker.thread.is_alive(), "closed mind must not block forever waiting for another request")
        self.assertTrue(brain.closed.is_set())
        self.assertTrue(worker.results.empty(), "closed worker must not publish stale personal dialogue")
        self.assertFalse(worker.submit(2, {}, "calm", ""), "closed worker must reject new work")

    def test_closing_idle_mind_closes_adapter(self):
        brain = self.Brain()
        worker = MindWorker(brain, None)
        worker.close()
        worker.thread.join(1)
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(brain.closed.is_set())


if __name__ == "__main__":
    unittest.main()
