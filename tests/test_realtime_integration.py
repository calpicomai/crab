"""Real loopback HTTP/WS integration; all devices are explicit simulation.

No configured robot address, hardware libraries, camera, sound, or personal
memory is accessed. Run with the existing brain virtual environment.
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
import unittest
import uuid

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from brain.realtime_benchmark import RealtimePeer, SimulatedServer
from shared import ACTION_PATHS, COMPANION_EVENTS_PATH, Action, CommandResponse
from shared.realtime import CommandRequest, PingRequest, WS_PATH


class RealtimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.server = self.enterContext(SimulatedServer())

    def peer(self, **kwargs):
        return self.enterContext(RealtimePeer(self.server, **kwargs))

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.assertTrue(predicate(), "condition did not become true before deadline")

    def blocked_cycle(self):
        entered, release = threading.Event(), threading.Event()
        original = self.server.engine._do_action
        calls = []

        def boundary(action, speed):
            calls.append((action, speed))
            entered.set()
            if not release.wait(3):
                raise RuntimeError("integration fixture did not release simulated boundary")
            original(action, speed)

        self.server.engine._do_action = boundary
        self.addCleanup(release.set)
        return entered, release, calls

    def test_hello_current_status_and_correlated_heartbeat(self):
        peer = self.peer()
        status = peer.until("status")
        self.assertEqual(peer.hello.boot_id, self.server.leases.boot_id)
        self.assertEqual(status.boot_id, peer.hello.boot_id)
        self.assertEqual(status.session_id, peer.hello.session_id)
        self.assertTrue(status.status.simulate)
        self.assertEqual(status.status.motion_state["sequence"], 0)
        self.assertEqual(status.status.inputs["battery"].provenance, "simulated")
        self.assertLess(abs(time.time()-status.status.inputs["battery"].captured_at), 1)
        ping = PingRequest(**peer.envelope())
        peer.send(ping)
        pong = peer.until("pong", request_id=ping.request_id)
        self.assertEqual(pong.session_id, peer.hello.session_id)

    def test_event_is_pushed_and_not_drained_by_parallel_http_reader(self):
        peer = self.peer()
        peer.until("status")
        started = time.monotonic()
        response = self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "hello websocket"})
        self.assertTrue(response.json()["ok"])
        event = peer.until("event", timeout=1)
        self.assertEqual(event.event.text, "hello websocket")
        self.assertEqual(event.event.source, "dashboard")
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(self.server.http.get(COMPANION_EVENTS_PATH).json()["events"], [])

    def test_command_results_reuse_lease_and_motion_guards(self):
        peer = self.peer()
        missing = peer.command(Action.WALK, {"steps": 1, "command_id": "missing-lease"}, lease=False)
        self.assertFalse(missing.ok)
        self.assertIn("lease", missing.detail)
        grant = peer.lease()
        payload = {"steps": 1, "lease_id": grant.lease_id, "command_id": uuid.uuid4().hex}
        first = peer.command(Action.WALK, payload, lease=False)
        self.assertIsInstance(first, CommandResponse)
        self.assertTrue(first.ok, first.detail)
        self.assertEqual(first.command_id, payload["command_id"])
        self.assertEqual(first.motion.completed_cycles, 1)
        replay = peer.command(Action.WALK, payload, lease=False)
        self.assertFalse(replay.ok)
        fresh_lease_same_command = peer.command(Action.WALK, payload)
        self.assertFalse(fresh_lease_same_command.ok)
        self.assertEqual(self.server.engine.motion_state()["sequence"], 1)
        self.server.battery.value = None
        blocked = peer.command(Action.TURN_STEPS, {"direction": "left"})
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.motion.completed_cycles, 0)
        self.assertEqual(self.server.engine.motion_state()["sequence"], 1)

    def test_expired_lease_never_executes(self):
        peer = self.peer()
        self.server.leases.ttl_s = .03
        grant = peer.lease()
        time.sleep(.04)
        result = peer.command(Action.WALK, {"steps": 1, "lease_id": grant.lease_id,
                                            "command_id": uuid.uuid4().hex}, lease=False)
        self.assertFalse(result.ok)
        self.assertIn("lease", result.detail)
        self.assertEqual(self.server.engine.motion_state()["sequence"], 0)

    def test_invalid_command_payload_does_not_move(self):
        peer = self.peer()
        peer.socket.send(json.dumps({"version": 1, "type": "command", **peer.envelope(),
                                     "action": "walk", "payload": {"steps": 1, "servo_angles": [0]*12}}))
        # Invalid envelopes may close the session after a bounded error response.
        try:
            error = peer.until("error", timeout=1)
            self.assertTrue(error.reason)
        except ConnectionClosed:
            pass
        self.assertEqual(self.server.engine.motion_state()["sequence"], 0)

    def test_http_stop_bypasses_blocked_ws_vendor_boundary(self):
        peer = self.peer()
        entered, release, calls = self.blocked_cycle()
        grant = peer.lease()
        request = CommandRequest(**peer.envelope(), action=Action.WALK,
                                 payload={"steps": 1, "lease_id": grant.lease_id,
                                          "command_id": uuid.uuid4().hex})
        peer.send(request)
        self.assertTrue(entered.wait(1), "simulated boundary never started")
        started = time.monotonic()
        stopped = self.server.http.post(ACTION_PATHS[Action.STOP], json={})
        self.assertTrue(stopped.json()["ok"])
        self.assertLess(time.monotonic()-started, .8)
        self.assertFalse(release.is_set())
        self.assertTrue(self.server.engine.motion_state()["stop_latched"])
        release.set()
        result = peer.until("result", request_id=request.request_id).response
        self.assertFalse(result.ok)
        self.assertTrue(result.motion.cancelled)
        self.assertEqual(result.motion.completed_cycles, 1)
        self.assertEqual(len(calls), 1)

    def test_resume_event_from_before_stop_cannot_acquire_fresh_authority(self):
        peer = self.peer()
        peer.until("status")
        self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "resume"})
        old_event = peer.until("event")
        self.assertEqual(old_event.event.text, "resume")
        stopped = self.server.http.post(ACTION_PATHS[Action.STOP], json={})
        self.assertTrue(stopped.json()["ok"])
        deadline = time.monotonic()+1
        while True:
            current = peer.until("status", timeout=max(.001, deadline-time.monotonic()))
            if current.control_epoch > old_event.control_epoch:
                break
            self.assertLess(time.monotonic(), deadline)
        self.assertTrue(current.status.motion_state["stop_latched"])
        # Simulate the old RESUME arriving at behavior after the newer STOP.
        # It must retain its original epoch even though newer status was observed.
        rejected = peer.lease(control_epoch=old_event.control_epoch)
        self.assertFalse(rejected.ok)
        self.assertIsNone(rejected.lease_id)
        self.assertTrue(self.server.engine.motion_state()["stop_latched"])
        self.assertEqual(self.server.engine.motion_state()["sequence"], 0)

        self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "resume"})
        explicit = peer.until("event")
        self.assertGreater(explicit.control_epoch, old_event.control_epoch)
        grant = peer.lease(control_epoch=explicit.control_epoch)
        self.assertTrue(grant.ok, grant.reason)
        result = peer.command(Action.RESUME, {"lease_id": grant.lease_id,
                                              "command_id": uuid.uuid4().hex}, lease=False)
        self.assertTrue(result.ok, result.detail)
        self.assertFalse(self.server.engine.motion_state()["stop_latched"])
        self.assertEqual(self.server.engine.motion_state()["sequence"], 0)

    def test_client_keeps_saved_event_epoch_when_status_advances(self):
        from brain.realtime import RealtimeClient, RealtimeUnavailable
        events = queue.Queue()
        client = RealtimeClient(self.server.base_url, on_event=events.put_nowait)
        self.addCleanup(client.close)
        client.start()
        self.wait_for(lambda: client.health().get("ready"))
        self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "resume"})
        old_event = events.get(timeout=1)
        self.server.http.post(ACTION_PATHS[Action.STOP], json={})
        self.wait_for(lambda: client.health().get("control_epoch", -1) > old_event["control_epoch"])
        with self.assertRaises(RealtimeUnavailable):
            client.command(Action.RESUME, {}, expected_control_epoch=old_event["control_epoch"])
        self.assertTrue(self.server.engine.motion_state()["stop_latched"])
        self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "resume"})
        explicit = events.get(timeout=1)
        result = client.command(Action.RESUME, {}, expected_control_epoch=explicit["control_epoch"])
        self.assertTrue(result.ok, result.detail)
        self.assertFalse(self.server.engine.motion_state()["stop_latched"])
        self.assertEqual(self.server.engine.motion_state()["sequence"], 0)

    def test_second_connection_cannot_replace_current_owner(self):
        peer = self.peer()
        with connect(self.server.ws_url+WS_PATH, proxy=None, open_timeout=1,
                     close_timeout=.2, ping_interval=None) as other:
            try:
                data = other.recv(timeout=1)
                self.assertEqual(json.loads(data)["type"], "error")
            except ConnectionClosed:
                pass
        ping = PingRequest(**peer.envelope())
        peer.send(ping)
        self.assertEqual(peer.until("pong", request_id=ping.request_id).session_id,
                         peer.hello.session_id)

    def test_browser_origin_is_rejected_before_ownership_or_stop_changes(self):
        self.assertFalse(self.server.engine.motion_state()["stop_latched"])
        with self.assertRaises(InvalidStatus) as rejected:
            with connect(self.server.ws_url+WS_PATH, origin="https://untrusted.invalid",
                         proxy=None, open_timeout=1, close_timeout=.2, ping_interval=None):
                self.fail("browser-origin WebSocket must not become a control session")
        self.assertEqual(rejected.exception.response.status_code, 403)
        self.assertFalse(self.server.module.realtime_hub.has_owner)
        self.assertFalse(self.server.engine.motion_state()["stop_latched"])
        self.assertEqual(self.peer().until("status").status.motion_state["sequence"], 0)

    def test_reconnect_gets_current_status_and_no_old_events(self):
        with RealtimePeer(self.server) as first:
            first.until("status")
            self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "old session event"})
            self.assertEqual(first.until("event").event.text, "old session event")
            old_session = first.hello.session_id
        self.wait_for(lambda: self.server.engine.motion_state()["stop_latched"])
        self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "disconnected event"})
        with RealtimePeer(self.server) as second:
            self.assertNotEqual(second.hello.session_id, old_session)
            current = second.until("status")
            self.assertTrue(current.status.motion_state["stop_latched"])
            self.server.http.post(COMPANION_EVENTS_PATH, json={"text": "current session event"})
            self.assertEqual(second.until("event").event.text, "current session event")


class RealtimeFailureIntegrationTests(unittest.TestCase):
    def test_heartbeat_loss_latches_stop_and_old_session_is_rejected(self):
        with SimulatedServer(heartbeat_timeout_s=1.2) as server:
            with RealtimePeer(server, heartbeat=False) as first:
                first.until("status")
                old = first.hello
                deadline = time.monotonic()+2
                while not server.engine.motion_state()["stop_latched"] and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(server.engine.motion_state()["stop_latched"])
            with RealtimePeer(server) as second:
                grant = second.lease()
                stale = CommandRequest(boot_id=old.boot_id, session_id=old.session_id,
                                       request_id=uuid.uuid4().hex, action=Action.RESUME,
                                       payload={"lease_id": grant.lease_id, "command_id": uuid.uuid4().hex})
                second.send(stale)
                try:
                    self.assertEqual(second.until("error", timeout=1).type, "error")
                except ConnectionClosed:
                    pass
                self.assertTrue(server.engine.motion_state()["stop_latched"])
                self.assertEqual(server.engine.motion_state()["sequence"], 0)

    def test_lost_command_result_is_unknown_and_is_not_replayed_on_reconnect(self):
        from brain.realtime import CommandOutcomeUnknown, RealtimeClient
        with SimulatedServer() as server:
            entered, release = threading.Event(), threading.Event()
            calls, outcomes, statuses = [], [], []
            original = server.engine._do_action

            def slow_boundary(action, speed):
                calls.append(action)
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("fixture boundary was not released")
                original(action, speed)

            server.engine._do_action = slow_boundary
            client = RealtimeClient(server.base_url, on_status=lambda status, reason: statuses.append(status),
                                    reconnect_min_s=.05, reconnect_max_s=.1)
            client.start()
            worker = None
            try:
                deadline = time.monotonic()+2
                while not client.health().get("ready") and time.monotonic()<deadline:
                    time.sleep(.005)
                self.assertTrue(client.health().get("ready"), client.health())
                original_session = client.health()["session_id"]

                def command():
                    try:
                        outcomes.append(client.command(Action.WALK, {"steps": 1}, timeout=2))
                    except Exception as exc:
                        outcomes.append(exc)

                worker = threading.Thread(target=command)
                worker.start()
                self.assertTrue(entered.wait(1))
                client._connection.socket.shutdown(socket.SHUT_RDWR)  # deliberate network fault injection
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(outcomes[0], CommandOutcomeUnknown)
                release.set()
                deadline = time.monotonic()+3
                while time.monotonic()<deadline:
                    health = client.health()
                    if health.get("ready") and health.get("session_id") != original_session:
                        break
                    time.sleep(.005)
                self.assertTrue(client.health().get("ready"), client.health())
                self.assertNotEqual(client.health()["session_id"], original_session)
                # Wait through another published status; reconnect must not reissue WALK.
                time.sleep(.25)
                self.assertEqual(calls, ["forward"])
                self.assertEqual(server.engine.motion_state()["sequence"], 1)
                self.assertTrue(server.engine.motion_state()["stop_latched"])
                self.assertIn(None, statuses)
            finally:
                release.set()
                client.close()
                if worker:
                    worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
