"""Independent input polling, using blocked fake transports and no network."""
from __future__ import annotations

import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from brain.pet.inputs import InputWorker


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeClients:
    def __init__(self, status=None, events=None, perception=None):
        self.status = status or (lambda: {"observed_at": time.time(), "inputs": {}})
        self.events = events or (lambda: {"events": []})
        self.perception = perception or (lambda: {"inputs": {}, "detections": []})
        self.created, self.closed = [], []
        self.calls = {"status": 0, "events": 0, "perception": 0}
        self.active = dict.fromkeys(self.calls, 0)
        self.peak = dict.fromkeys(self.calls, 0)
        self.lock = threading.Lock()

    def _call(self, name):
        with self.lock:
            self.calls[name] += 1
            self.active[name] += 1
            self.peak[name] = max(self.peak[name], self.active[name])
        try:
            return getattr(self, name)()
        finally:
            with self.lock:
                self.active[name] -= 1

    def client(self, kind):
        owner = self

        class Client:
            def __enter__(self):
                owner.created.append(self)
                return self

            def __exit__(self, *args):
                owner.closed.append(self)

            def get_status(self):
                payload = owner._call("status")
                return SimpleNamespace(ok=True, detail="", status=SimpleNamespace(model_dump=lambda **kwargs: payload))

            def get(self, url):
                name = "perception" if url.endswith("/snapshot") else "events"
                return Response(owner._call(name))

        return Client()

    def robot(self, *args, **kwargs):
        return self.client("status")

    def http(self, *args, **kwargs):
        return self.client("http")


class InputWorkerTests(unittest.TestCase):
    def worker(self, clients, **kwargs):
        heard = queue.Queue(maxsize=32)
        worker = InputWorker("http://localhost:8000", "http://localhost:8100", heard,
                             status_interval_s=0.02, events_interval_s=0.02,
                             perception_interval_s=0.02, **kwargs)
        self.addCleanup(worker.close)
        robot = patch("brain.pet.inputs.RobotClient", clients.robot)
        http = patch("brain.pet.inputs.httpx.Client", clients.http)
        robot.start()
        http.start()
        self.addCleanup(robot.stop)
        self.addCleanup(http.stop)
        return worker, heard

    def until(self, condition):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        self.fail("condition did not complete within one second")

    def test_blocked_perception_does_not_delay_status_or_commands(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def detector():
            entered.set()
            release.wait(1)
            return {"inputs": {}, "detections": []}

        clients = FakeClients(perception=detector, events=lambda: {
            "events": [{"text": "stop", "timestamp": time.time()}]})
        worker, heard = self.worker(clients)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.until(lambda: clients.calls["status"] >= 3 and not heard.empty())
        self.assertFalse(release.is_set())
        self.assertEqual(heard.get_nowait()["text"], "stop")
        self.assertIsNotNone(worker.samples.get_nowait()[0])
        self.assertEqual(clients.calls["perception"], 1)
        release.set()
        worker.close()
        self.assertEqual(len(clients.created), 3)
        self.assertEqual(len(clients.closed), 3)
        self.assertEqual(clients.peak, {"status": 1, "events": 1, "perception": 1})

    def test_perception_completion_never_republishes_old_status(self):
        blocked, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        counter = 0

        def status():
            nonlocal counter
            counter += 1
            if counter > 1:
                blocked.set()
                release.wait(1)
                raise RuntimeError("robot connection failed")
            return {"observed_at": 123.0, "inputs": {}}

        clients = FakeClients(status=status)
        worker, _ = self.worker(clients)
        worker.start()
        self.assertEqual(worker.samples.get(timeout=1)[0]["observed_at"], 123.0)
        self.assertTrue(blocked.wait(1))
        self.until(lambda: clients.calls["perception"] >= 4)
        self.assertTrue(worker.samples.empty())
        release.set()
        sample = worker.samples.get(timeout=1)
        self.assertIsNone(sample[0])
        self.assertIn("connection failed", sample[2])

    def test_capture_timestamps_remain_old_after_new_http_receipt(self):
        captured = time.time() - 60
        payload = {"frame_id": 9, "inputs": {"camera": {
            "available": True, "provenance": "real", "captured_at": captured,
            "received_at": captured + 0.1, "max_age_s": 2}}, "detections": []}
        clients = FakeClients(status=lambda: {"observed_at": captured, "inputs": {}},
                              perception=lambda: payload)
        worker, _ = self.worker(clients)
        worker.start()
        deadline = time.monotonic() + 1
        sample = None
        while time.monotonic() < deadline:
            sample = worker.samples.get(timeout=1)
            if sample[1] is not None:
                break
        self.assertIsNotNone(sample[1])
        self.assertEqual(sample[0]["observed_at"], captured)
        self.assertEqual(sample[1], payload)
        self.assertGreater(worker.health()["streams"]["perception"]["received_at"], captured + 50)

    def test_perception_failure_clears_previous_snapshot_without_losing_status(self):
        fail = threading.Event()

        def detector():
            if fail.is_set():
                raise RuntimeError("detector unavailable")
            return {"frame_id": 1, "inputs": {}, "detections": []}

        clients = FakeClients(perception=detector)
        worker, _ = self.worker(clients)
        worker.start()
        self.until(lambda: worker.health()["streams"]["perception"]["available"])
        fail.set()
        self.until(lambda: worker.health()["streams"]["perception"]["failure_reason"] == "detector unavailable")
        received = []
        def received_without_snapshot():
            try:
                sample = worker.samples.get_nowait()
            except queue.Empty:
                return False
            if sample[1] is None:
                received.append(sample)
                return True
            return False
        self.until(received_without_snapshot)
        status, snapshot, reason = received[0]
        self.assertIsNotNone(status)
        self.assertIsNone(snapshot)
        self.assertIsNone(reason)

    def test_shutdown_discards_inflight_results_and_closes_owned_clients(self):
        release = threading.Event()
        self.addCleanup(release.set)
        entered = {name: threading.Event() for name in ("status", "events", "perception")}

        def delayed(name, payload):
            def get():
                entered[name].set()
                release.wait(1)
                return payload
            return get

        clients = FakeClients(
            status=delayed("status", {"inputs": {}}),
            events=delayed("events", {"events": [{"text": "hello", "timestamp": time.time()}]}),
            perception=delayed("perception", {"inputs": {}}))
        worker, heard = self.worker(clients)
        worker.start()
        for event in entered.values():
            self.assertTrue(event.wait(1))
        closer = threading.Thread(target=worker.close)
        closer.start()
        self.assertTrue(worker.stop_event.wait(1))
        release.set()
        closer.join(1)
        self.assertFalse(closer.is_alive())
        self.assertEqual(worker.health()["running_workers"], 0)
        self.assertEqual(len(clients.closed), 3)
        self.assertTrue(worker.samples.empty())
        self.assertTrue(heard.empty())
        worker.start()  # closed workers cannot restart or replay late data
        self.assertEqual(worker.health()["running_workers"], 0)

    def test_disabled_camera_uses_two_clients_and_rejects_stale_events(self):
        clients = FakeClients(events=lambda: {"events": [
            {"text": "stale", "timestamp": time.time() - 6},
            {"text": "future", "timestamp": time.time() + 6},
            {"text": "bad", "timestamp": "now"},
            {"text": "fresh", "timestamp": time.time()},
        ]})
        worker, heard = self.worker(clients, use_camera=False)
        worker.start()
        self.until(lambda: not heard.empty())
        self.assertEqual(heard.get_nowait()["text"], "fresh")
        worker.close()
        self.assertEqual(clients.calls["perception"], 0)
        self.assertEqual(len(clients.created), 2)
        self.assertEqual(len(clients.closed), 2)
        self.assertEqual(worker.health()["workers"], 2)


if __name__ == "__main__":
    unittest.main()
