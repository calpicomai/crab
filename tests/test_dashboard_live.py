"""Bounded live telemetry delivery and retention; fake HTTP, no network/device."""
from __future__ import annotations

import copy
import math
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from brain.dashboard import Dashboard
from robot.telemetry import TelemetryStore


class FakeHTTP:
    """Controllable persistent transport that records concurrency and payloads."""
    def __init__(self, outcomes=(), block_first=False):
        self.outcomes = list(outcomes)
        self.block_first = block_first
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.condition = threading.Condition()
        self.calls = []
        self.active = self.peak = self.created = 0
        self.client_options = None

    def client(self, **options):
        self.created += 1
        self.client_options = options
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed.set()

    def post(self, url, *, json):
        with self.condition:
            index = len(self.calls)
            self.calls.append(copy.deepcopy(json))
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.condition.notify_all()
        try:
            if index == 0 and self.block_first:
                self.entered.set()
                if not self.release.wait(2):
                    raise AssertionError("test did not release blocked fake transport")
            outcome = self.outcomes[index] if index < len(self.outcomes) else {"ok": True}
            if isinstance(outcome, Exception):
                raise outcome
            status, body = outcome if isinstance(outcome, tuple) else (200, outcome)
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def wait_calls(self, count):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.calls) >= count, timeout=1)


class DashboardTests(unittest.TestCase):
    def make(self, transport, *, enabled=True):
        patcher = patch("brain.dashboard.httpx.Client", side_effect=transport.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        dashboard = Dashboard("http://localhost:8000", enabled)
        # Release even if an assertion fails; do not leave a daemon in patched I/O.
        self.addCleanup(dashboard.close)
        self.addCleanup(transport.release.set)
        return dashboard

    def until(self, predicate):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.002)
        self.fail("publisher did not reach expected state within one second")

    def test_single_inflight_and_only_newest_pending_during_burst(self):
        transport = FakeHTTP(block_first=True)
        dashboard = self.make(transport)
        dashboard.push({"tick": 0})
        self.assertTrue(transport.entered.wait(1))
        for tick in range(1, 101):
            dashboard.push({"tick": tick})
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(dashboard.health()["pending"])
        self.assertEqual(dashboard.health()["replaced"], 99)
        transport.release.set()
        self.until(lambda: dashboard.health()["sent"] == 2)
        self.assertEqual([call["tick"] for call in transport.calls], [0, 100])
        self.assertEqual([call["_sequence"] for call in transport.calls], [1, 101])
        self.assertGreater(transport.calls[0]["_publisher_started_at_ns"], 0)
        self.assertEqual(transport.calls[0]["_publisher_started_at_ns"],
                         transport.calls[1]["_publisher_started_at_ns"])
        self.assertEqual(transport.peak, 1)
        self.assertEqual(transport.created, 1)
        self.assertFalse(transport.client_options["trust_env"])
        self.assertFalse(transport.client_options["follow_redirects"])
        self.assertGreaterEqual(dashboard.health()["last_latency_ms"], 0)

    def test_visible_histories_are_trimmed_and_nested_values_are_copied(self):
        transport = FakeHTTP(block_first=True)
        dashboard = self.make(transport)
        dashboard.push({"tick": "blocker"})
        self.assertTrue(transport.entered.wait(1))
        state = {"tick": 1, "intentions": [{"id": i, "result": {"reason": "original"}} for i in range(20)],
                 "movements": [{"cycle": i} for i in range(10)],
                 "state": {"engagement": .2}}
        dashboard.push(state)
        # Local log/caller keeps the full window and can update after push.
        self.assertEqual(len(state["intentions"]), 20)
        state["intentions"][-1]["result"]["reason"] = "mutated"
        state["movements"][-1]["cycle"] = 1000
        state["state"]["engagement"] = .9
        transport.release.set()
        self.until(lambda: dashboard.health()["sent"] == 2)
        sent = transport.calls[-1]
        self.assertEqual([intent["id"] for intent in sent["intentions"]], list(range(8, 20)))
        self.assertEqual([movement["cycle"] for movement in sent["movements"]], list(range(4, 10)))
        self.assertEqual(sent["intentions"][-1]["result"]["reason"], "original")
        self.assertEqual(sent["state"]["engagement"], .2)
        for key in ("_produced_at", "_sent_at"):
            self.assertTrue(math.isfinite(sent[key]))
        self.assertNotIn("_publisher_id", state)

    def test_http_rejection_and_transport_failure_recover_on_fresh_sample(self):
        transport = FakeHTTP(outcomes=[(503, {"ok": False}),
                                       httpx.ReadTimeout("slow Pi"),
                                       {"ok": False, "reason": "outdated"}, {"ok": True}])
        dashboard = self.make(transport)
        for tick in range(3):
            dashboard.push({"tick": tick})
            self.assertTrue(transport.wait_calls(tick + 1))
            self.until(lambda: dashboard.health()["failure_reason"] is not None)
            self.assertEqual(dashboard.health()["sent"], 0)
            self.assertIsNone(dashboard.health()["last_success_at"])
        dashboard.push({"tick": 3})
        self.until(lambda: dashboard.health()["sent"] == 1)
        self.assertIsNone(dashboard.health()["failure_reason"])
        self.assertIsNotNone(dashboard.health()["last_success_at"])
        self.assertEqual([call["tick"] for call in transport.calls], [0, 1, 2, 3])
        self.assertEqual(transport.created, 1)

    def test_truthy_non_boolean_acknowledgement_is_not_success(self):
        transport = FakeHTTP(outcomes=[{"ok": "false"}, {"ok": 1}, ["ok"], {"ok": True}])
        dashboard = self.make(transport)
        for tick in range(3):
            dashboard.push({"tick": tick})
            self.assertTrue(transport.wait_calls(tick + 1))
            self.until(lambda: dashboard.health()["failure_reason"] is not None)
            self.assertEqual(dashboard.health()["sent"], 0)
        dashboard.push({"tick": 3})
        self.until(lambda: dashboard.health()["sent"] == 1)
        self.assertIsNone(dashboard.health()["failure_reason"])

    def test_close_drains_one_latest_sample_and_closes_persistent_transport(self):
        transport = FakeHTTP(block_first=True)
        dashboard = self.make(transport)
        dashboard.push({"tick": 0})
        self.assertTrue(transport.entered.wait(1))
        dashboard.push({"tick": 1})
        # Closing has a bounded wait even if I/O is still outstanding.
        dashboard.close(timeout=0)
        dashboard.push({"tick": 2})
        transport.release.set()
        dashboard.close()
        self.assertTrue(transport.closed.is_set())
        self.assertFalse(dashboard._thread.is_alive())
        self.assertEqual([call["tick"] for call in transport.calls], [0, 1])
        self.assertFalse(dashboard.health()["pending"])
        self.assertEqual(dashboard.health()["sent"], 2)

    def test_disabled_publisher_creates_no_worker_or_http_client(self):
        transport = FakeHTTP()
        dashboard = self.make(transport, enabled=False)
        dashboard.push({"tick": 1})
        dashboard.close()
        self.assertEqual(transport.created, 0)
        self.assertEqual(transport.calls, [])
        self.assertFalse(dashboard.health()["enabled"])


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.store = TelemetryStore(clock=lambda: self.now)

    def payload(self, publisher="first", sequence=1, **extra):
        return {"_publisher_id": publisher, "_sequence": sequence, "_produced_at": self.now,
                "_publisher_started_at_ns": {"first": 100, "second": 200}.get(publisher, 300),
                "state": {"phase": "listen"}, **extra}

    def test_ordering_rejects_duplicate_and_older_sequence_without_mutating_state(self):
        newest = self.payload(sequence=3, name="newest")
        self.assertTrue(self.store.publish(newest)["ok"])
        self.now += 1
        for sequence in (1, 2, 3):
            self.assertFalse(self.store.publish(self.payload(sequence=sequence, name="stale"))["ok"])
        state, revision = self.store.snapshot()
        self.assertEqual(revision, 1)
        self.assertEqual(state["name"], "newest")
        self.assertEqual(state["_received_at"], 1000)
        self.assertTrue(self.store.publish(self.payload(sequence=4, name="next"))["ok"])
        self.assertEqual(self.store.revision, 2)

    def test_restarted_publisher_can_begin_at_one_but_retired_publisher_cannot_return(self):
        self.assertTrue(self.store.publish(self.payload(sequence=12))["ok"])
        self.now += 1
        self.assertTrue(self.store.publish(self.payload(publisher="second", sequence=1))["ok"])
        self.assertFalse(self.store.publish(self.payload(sequence=1000))["ok"])
        self.assertTrue(self.store.publish(self.payload(publisher="second", sequence=2))["ok"])
        state, revision = self.store.snapshot()
        self.assertEqual(state["_publisher_id"], "second")
        self.assertEqual(state["_sequence"], 2)
        self.assertEqual(revision, 3)

    def test_invalid_and_nonfinite_json_cannot_replace_last_usable_state(self):
        self.store.publish(self.payload())
        state, revision = self.store.snapshot()
        invalid = [self.payload(publisher=""), self.payload(publisher="x"*65),
                   self.payload(sequence=0), self.payload(sequence=True), self.payload(sequence="2"),
                   self.payload(_publisher_started_at_ns=None), self.payload(_publisher_started_at_ns=True),
                   self.payload(_publisher_started_at_ns=0), self.payload(_publisher_started_at_ns=1.5),
                   self.payload(sequence=2, value=float("nan")),
                   self.payload(sequence=2, nested={"value": float("inf")}),
                   self.payload(sequence=2, value={1, 2}),
                   self.payload(sequence=2, value=b"bytes")]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertFalse(self.store.publish(payload)["ok"])
                self.assertEqual(self.store.snapshot(), (state, revision))

    def test_delayed_unseen_publisher_cannot_retire_newer_publisher(self):
        self.assertTrue(self.store.publish(self.payload(publisher="second"))["ok"])
        self.now += 10  # Receipt time cannot make an older process current.
        result = self.store.publish(self.payload(publisher="first", sequence=10000))
        self.assertFalse(result["ok"])
        self.assertIn("publisher epoch", result["reason"])
        equal_epoch = self.store.publish(self.payload(publisher="equal", _publisher_started_at_ns=200))
        self.assertFalse(equal_epoch["ok"])
        self.assertTrue(self.store.publish(self.payload(publisher="second", sequence=2))["ok"])
        self.assertEqual(self.store.snapshot()[0]["_publisher_id"], "second")

    def test_publisher_epoch_is_constant_and_old_epochs_stay_retired_after_many_restarts(self):
        self.store.publish(self.payload())
        rejected = self.store.publish(self.payload(sequence=2, _publisher_started_at_ns=101))
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.store.revision, 1)
        for index in range(1, 40):
            self.assertTrue(self.store.publish(self.payload(
                publisher=f"restart-{index}", _publisher_started_at_ns=100+index))["ok"])
        self.assertFalse(self.store.publish(self.payload(sequence=1000))["ok"])
        state, revision = self.store.snapshot()
        self.assertEqual(state["_publisher_id"], "restart-39")
        self.assertEqual(revision, 40)

    def test_non_object_payload_is_rejected_without_losing_state(self):
        self.store.publish(self.payload())
        before = self.store.snapshot()
        for payload in (None, [], "not an object", 1):
            with self.subTest(payload=payload):
                self.assertFalse(self.store.publish(payload)["ok"])
                self.assertEqual(self.store.snapshot(), before)

    def test_publish_and_snapshot_nested_values_cannot_mutate_stored_state(self):
        payload = self.payload()
        self.store.publish(payload)
        payload["state"]["phase"] = "caller changed input"
        state, revision = self.store.snapshot()
        self.assertEqual(state["state"]["phase"], "listen")
        state["state"]["phase"] = "reader changed snapshot"
        second, second_revision = self.store.snapshot()
        self.assertEqual(second["state"]["phase"], "listen")
        self.assertEqual(second_revision, revision)

    def test_empty_snapshot_and_elapsed_time_do_not_refresh_retained_state(self):
        self.assertEqual(self.store.snapshot(), ({}, 0))
        self.store.publish(self.payload())
        self.now += 60
        state, revision = self.store.snapshot()
        self.assertEqual(state["_received_at"], 1000)
        self.assertEqual(revision, 1)
        self.assertEqual(state["state"]["phase"], "listen")


if __name__ == "__main__":
    unittest.main()
