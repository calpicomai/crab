"""WebSocket client edge cases use fake duplex sockets; no hardware/network."""
from __future__ import annotations

import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from websockets.exceptions import InvalidStatus

from brain.client import RobotClient
from brain.realtime import (CommandOutcomeUnknown, RealtimeBusy, RealtimeClient,
                            RealtimeUnavailable, _NoRedirectConnection)
from shared import Action, CommandResponse, RobotStatus
from shared.realtime import (CommandRequest, EventMessage, HelloMessage, InputEvent,
                             LeaseMessage, LeaseRequest, PingRequest, PongMessage,
                             ResultMessage, StatusMessage, encode_message,
                             parse_client_message)


class FakeConnection:
    def __init__(self, server, index):
        self.server, self.index = server, index
        self.boot_id, self.session_id = "boot", f"session{index}"
        self.control_epoch = 0
        self.incoming = queue.Queue()
        self.closed = threading.Event()
        self.socket = SimpleNamespace(shutdown=lambda how: self.close())
        self.push(HelloMessage(**self.ids(), control_epoch=self.control_epoch))
        self.status(stamp=server.status_stamp)

    def ids(self):
        return {"boot_id": self.boot_id, "session_id": self.session_id}

    def push(self, message):
        self.incoming.put(encode_message(message))

    def status(self, stamp=None, sequence=1, control_epoch=None):
        if control_epoch is not None:
            self.control_epoch = control_epoch
        self.push(StatusMessage(**self.ids(), sequence=sequence,
                                control_epoch=self.control_epoch,
                                status=RobotStatus(boot_id=self.boot_id,
                                                   observed_at=time.time()+self.server.clock_offset if stamp is None else stamp)))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed.set()
        self.incoming.put(None)

    def recv(self, timeout=None):
        if self.closed.is_set():
            raise OSError("fake socket disconnected")
        try:
            value = self.incoming.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if value is None:
            raise OSError("fake socket disconnected")
        return value

    def send(self, data):
        if self.closed.is_set():
            raise OSError("fake socket disconnected")
        message = parse_client_message(data)
        self.server.sent.append(message)
        if self.server.handler and self.server.handler(self, message):
            return
        if isinstance(message, PingRequest):
            self.push(PongMessage(**self.ids(), request_id=message.request_id,
                                  server_at=time.time()+self.server.clock_offset))
        elif isinstance(message, LeaseRequest):
            self.push(LeaseMessage(**self.ids(), request_id=message.request_id,
                                   control_epoch=message.control_epoch,
                                   ok=True, lease_id=f"lease{len(self.server.sent)}", ttl_s=1.0))
        elif isinstance(message, CommandRequest):
            self.push(ResultMessage(**self.ids(), request_id=message.request_id,
                                    response=CommandResponse(ok=True, action=message.action,
                                                             command_id=message.command.command_id)))


class FakeServer:
    def __init__(self, handler=None, status_stamp=None, clock_offset=0):
        self.handler, self.status_stamp = handler, status_stamp
        self.clock_offset = clock_offset
        self.connections, self.sent, self.options = [], [], []

    def connect(self, uri, **kwargs):
        self.options.append((uri, kwargs))
        connection = FakeConnection(self, len(self.connections) + 1)
        self.connections.append(connection)
        return connection


class RealtimeClientTests(unittest.TestCase):
    def link(self, server, **kwargs):
        connector = patch("brain.realtime.connect", server.connect)
        connector.start()
        self.addCleanup(connector.stop)
        link = RealtimeClient("http://localhost:8000", reconnect_min_s=0.01,
                              reconnect_max_s=0.02, **kwargs)
        self.addCleanup(link.close)
        link.start()
        return link

    def until(self, predicate):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.002)
        self.fail("condition did not complete")

    def command_thread(self, link, action=Action.RESUME, timeout=0.5):
        results = []
        def call():
            try:
                results.append(link.command(action, {}, timeout=timeout))
            except Exception as exc:
                results.append(exc)
        thread = threading.Thread(target=call)
        thread.start()
        self.addCleanup(lambda: thread.join(1))
        return thread, results

    def test_fresh_lease_correlated_result_and_local_connection_policy(self):
        server = FakeServer()
        statuses, events = [], []
        link = self.link(server, on_status=lambda status, reason: statuses.append((status, reason)),
                         on_event=events.append)
        self.assertTrue(link._ready.wait(1))
        response = link.command(Action.STAND, {})
        self.assertTrue(response.ok)
        commands = [m for m in server.sent if isinstance(m, CommandRequest)]
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].command.lease_id)
        self.assertEqual(response.command_id, commands[0].command.command_id)
        uri, kwargs = server.options[0]
        self.assertEqual(uri, "ws://localhost:8000/companion/ws")
        self.assertIsNone(kwargs["proxy"])
        self.assertIs(kwargs["create_connection"], _NoRedirectConnection)
        self.assertLessEqual(kwargs["max_queue"], 4)
        connection = server.connections[0]
        connection.push(EventMessage(**connection.ids(), sequence=1,
                                    control_epoch=connection.control_epoch,
                                    event=InputEvent(text="hello", timestamp=time.time())))
        self.until(lambda: len(events) == 1)
        self.assertEqual(events[0]["source"], "dashboard")
        self.assertEqual(events[0]["control_epoch"], 0)
        self.assertEqual(statuses[0][0]["boot_id"], "boot")
        self.assertEqual(statuses[0][0]["control_epoch"], 0)

    def test_new_status_does_not_upgrade_saved_resume_event_authority(self):
        server, events = FakeServer(), []
        link = self.link(server, on_event=events.append)
        self.assertTrue(link._ready.wait(1))
        connection = server.connections[0]
        connection.push(EventMessage(**connection.ids(), sequence=1, control_epoch=0,
                                    event=InputEvent(text="resume", timestamp=time.time())))
        self.until(lambda: len(events) == 1)
        saved_epoch = events[0]["control_epoch"]
        connection.status(sequence=2, control_epoch=1)
        self.until(lambda: link.health()["control_epoch"] == 1)
        with self.assertRaisesRegex(RealtimeUnavailable, "epoch is stale"):
            link.command(Action.RESUME, {}, expected_control_epoch=saved_epoch)
        self.assertFalse(any(isinstance(m, LeaseRequest) for m in server.sent))
        # An older event received after the new status is discarded entirely.
        connection.push(EventMessage(**connection.ids(), sequence=2, control_epoch=0,
                                    event=InputEvent(text="resume", timestamp=time.time())))
        connection.push(EventMessage(**connection.ids(), sequence=3, control_epoch=1,
                                    event=InputEvent(text="resume", timestamp=time.time())))
        self.until(lambda: len(events) == 2)
        self.assertEqual([event["control_epoch"] for event in events], [0, 1])
        self.assertTrue(link.command(Action.RESUME, {}, expected_control_epoch=1).ok)
        lease = next(message for message in server.sent if isinstance(message, LeaseRequest))
        self.assertEqual(lease.control_epoch, 1)

    def test_new_epoch_cancels_lease_before_publishing_event(self):
        entered = threading.Event()
        def handler(connection, message):
            if isinstance(message, LeaseRequest):
                entered.set()
                return True
            return False
        server, callbacks = FakeServer(handler), []
        link = self.link(server, on_event=lambda event: callbacks.append(
            (event, link.health()["pending_requests"])))
        self.assertTrue(link._ready.wait(1))
        thread, results = self.command_thread(link)
        self.assertTrue(entered.wait(1))
        connection = server.connections[0]
        connection.push(EventMessage(**connection.ids(), sequence=1, control_epoch=1,
                                    event=InputEvent(text="stop", timestamp=time.time())))
        thread.join(0.5)
        self.assertIsInstance(results[0], RealtimeUnavailable)
        self.until(lambda: len(callbacks) == 1)
        self.assertEqual(callbacks[0][1], 0)
        self.assertEqual(callbacks[0][0]["control_epoch"], 1)
        old = next(m for m in server.sent if isinstance(m, LeaseRequest))
        connection.push(LeaseMessage(**connection.ids(), request_id=old.request_id,
            control_epoch=0, ok=True, lease_id="old_lease", ttl_s=1.0))
        # FIFO status processing confirms the late lease was read and ignored.
        connection.status(sequence=2, control_epoch=1)
        self.until(lambda: link._status_sequence == 2)
        self.assertFalse(any(isinstance(m, CommandRequest) for m in server.sent))

    def test_lease_grant_must_match_requested_control_epoch(self):
        def handler(connection, message):
            if isinstance(message, LeaseRequest):
                connection.push(LeaseMessage(**connection.ids(), request_id=message.request_id,
                    control_epoch=message.control_epoch + 1, ok=True, lease_id="wrong_epoch", ttl_s=1.0))
                return True
            return False
        server = FakeServer(handler)
        link = self.link(server)
        self.assertTrue(link._ready.wait(1))
        with self.assertRaisesRegex(RealtimeUnavailable, "epoch mismatch"):
            link.command(Action.RESUME, {})
        self.assertFalse(any(isinstance(m, CommandRequest) for m in server.sent))

    def test_status_frames_never_complete_command_rpc(self):
        def handler(connection, message):
            if isinstance(message, CommandRequest):
                connection.status(sequence=2)
                return True
            return False
        server = FakeServer(handler)
        link = self.link(server)
        self.assertTrue(link._ready.wait(1))
        with self.assertRaises(CommandOutcomeUnknown):
            link.command(Action.STAND, {}, timeout=0.05)
        self.assertEqual(len([m for m in server.sent if isinstance(m, CommandRequest)]), 1)

    def test_result_correlation_mismatch_is_unknown_and_not_replayed(self):
        def handler(connection, message):
            if isinstance(message, CommandRequest):
                connection.push(ResultMessage(**connection.ids(), request_id=message.request_id,
                    response=CommandResponse(ok=True, action=Action.SIT,
                                             command_id=message.command.command_id)))
                return True
            return False
        server = FakeServer(handler)
        link = self.link(server)
        self.assertTrue(link._ready.wait(1))
        with self.assertRaises(CommandOutcomeUnknown):
            link.command(Action.STAND, {})
        self.until(lambda: len(server.connections) >= 2 and link.health()["ready"])
        self.assertEqual(len([m for m in server.sent if isinstance(m, CommandRequest)]), 1)

    def test_disconnect_after_dispatch_is_unknown_and_reconnect_does_not_replay(self):
        def handler(connection, message):
            if isinstance(message, CommandRequest):
                connection.close()
                return True
            return False
        server = FakeServer(handler)
        changes = []
        link = self.link(server, on_status=lambda status, reason: changes.append((status, reason)))
        self.assertTrue(link._ready.wait(1))
        with self.assertRaises(CommandOutcomeUnknown):
            link.command(Action.RESUME, {})
        self.until(lambda: len(server.connections) >= 2 and link.health()["ready"])
        self.assertTrue(any(status is None for status, _ in changes))
        self.assertEqual(len([m for m in server.sent if isinstance(m, CommandRequest)]), 1)

    def test_stop_during_lease_wait_cannot_dispatch_old_resume(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def handler(connection, message):
            if isinstance(message, LeaseRequest) and not entered.is_set():
                entered.set()
                release.wait(1)
            return False
        server = FakeServer(handler)
        link = self.link(server)
        self.assertTrue(link._ready.wait(1))
        thread, results = self.command_thread(link)
        self.assertTrue(entered.wait(1))
        link.cancel_commands("stop")
        thread.join(0.2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(results[0], RealtimeUnavailable)
        release.set()
        self.until(lambda: link.health()["send_queue_depth"] == 0)
        self.assertFalse(any(isinstance(m, CommandRequest) for m in server.sent))
        # Explicit new work uses a fresh generation and lease; no old work resumes.
        self.assertTrue(link.command(Action.RESUME, {}).ok)
        self.assertEqual(len([m for m in server.sent if isinstance(m, CommandRequest)]), 1)

    def test_stop_fences_packet_waiting_behind_blocked_sender(self):
        blocked, release = threading.Event(), threading.Event()
        calibrated = threading.Event()
        self.addCleanup(release.set)
        def handler(connection, message):
            if isinstance(message, PingRequest) and calibrated.is_set() and not blocked.is_set():
                blocked.set()
                release.wait(1)
            return False
        server = FakeServer(handler)
        link = self.link(server)
        self.assertTrue(link._ready.wait(1))
        calibrated.set()
        with link._lock:
            link._next_ping = 0
        self.assertTrue(blocked.wait(1))
        thread, results = self.command_thread(link)
        self.until(lambda: link.health()["pending_requests"] == 1)
        link.cancel_commands("stop")
        release.set()
        thread.join(0.5)
        self.assertIsInstance(results[0], RealtimeUnavailable)
        self.until(lambda: link.health()["send_queue_depth"] == 0)
        self.assertFalse(any(isinstance(m, (LeaseRequest, CommandRequest)) for m in server.sent))

    def test_busy_and_disconnected_commands_are_not_queued(self):
        entered = threading.Event()
        def handler(connection, message):
            if isinstance(message, CommandRequest):
                entered.set()
                return True
            return False
        server = FakeServer(handler)
        link = self.link(server)
        self.assertTrue(link._ready.wait(1))
        thread, results = self.command_thread(link)
        self.assertTrue(entered.wait(1))
        with self.assertRaises(RealtimeBusy):
            link.command(Action.STAND, {})
        link.close()
        thread.join(0.5)
        self.assertIsInstance(results[0], CommandOutcomeUnknown)
        with self.assertRaises(RealtimeUnavailable):
            link.command(Action.STAND, {})
        self.assertEqual(len([m for m in server.sent if isinstance(m, CommandRequest)]), 1)

    def test_lease_expired_in_transit_never_becomes_motor_command(self):
        def handler(connection, message):
            if isinstance(message, LeaseRequest):
                time.sleep(0.04)
                connection.push(LeaseMessage(**connection.ids(), request_id=message.request_id,
                                             control_epoch=message.control_epoch,
                                             ok=True, lease_id="expired", ttl_s=0.01))
                return True
            return False
        server = FakeServer(handler)
        link = self.link(server)
        self.assertTrue(link._ready.wait(1))
        with self.assertRaisesRegex(RealtimeUnavailable, "expired in transit"):
            link.command(Action.STAND, {})
        self.assertFalse(any(isinstance(m, CommandRequest) for m in server.sent))

    def test_stale_capture_is_not_freshened_by_receipt(self):
        changes = []
        server = FakeServer(status_stamp=time.time() - 20)
        link = self.link(server, on_status=lambda status, reason: changes.append((status, reason)))
        self.until(lambda: bool(changes))
        self.assertFalse(link.health()["ready"])
        self.assertTrue(all(status is None for status, _ in changes))
        self.assertIn("stale", changes[0][1])

    def test_small_positive_pi_clock_skew_does_not_disconnect_status(self):
        server = FakeServer(clock_offset=.030)
        link = self.link(server)
        self.assertTrue(link._ready.wait(.5), link.health())

    def test_small_negative_pi_clock_skew_keeps_events_and_sensor_health(self):
        events, statuses = [], []
        server = FakeServer(clock_offset=-.030)
        link = self.link(server, on_event=events.append, on_status=lambda s, r: statuses.append(s))
        self.assertTrue(link._ready.wait(.5), link.health())
        connection = server.connections[0]
        connection.push(EventMessage(**connection.ids(), control_epoch=0, sequence=1,
                                    event=InputEvent(text="resume", timestamp=time.time()-.030)))
        self.until(lambda: bool(events))
        self.assertIn("_clock", events[0])
        self.assertIn("_clock", statuses[0])
        self.assertLess(link.health()["status_capture_age_s"], .1)

    def test_excessive_pi_skew_and_slow_clock_probe_never_become_ready(self):
        for offset in (-.5, .5):
            changes = []
            link = self.link(FakeServer(clock_offset=offset),
                             on_status=lambda s, r: changes.append((s, r)))
            self.until(lambda: bool(changes))
            self.assertFalse(link._ready.is_set())
            self.assertIn("clock calibration", changes[0][1])
            link.close()
        def delayed(connection, message):
            if isinstance(message, PingRequest):
                time.sleep(.22)
            return False
        changes = []
        link = self.link(FakeServer(delayed), on_status=lambda s, r: changes.append((s, r)))
        self.until(lambda: bool(changes))
        self.assertFalse(link._ready.is_set())
        self.assertIn("uncertainty", changes[0][1])

    def test_redirects_and_nonlocal_destinations_are_rejected(self):
        with self.assertRaises(ValueError):
            RealtimeClient("https://example.com")
        response = SimpleNamespace(status_code=302, reason_phrase="Found",
                                   headers={"Location": "https://example.com"})
        instance = object.__new__(_NoRedirectConnection)
        with patch("brain.realtime.ClientConnection.handshake", side_effect=InvalidStatus(response)):
            with self.assertRaisesRegex(RealtimeUnavailable, "redirects are disabled"):
                instance.handshake()


class RobotClientTransportTests(unittest.TestCase):
    def test_resume_preserves_original_epoch_as_transport_metadata(self):
        calls = []
        realtime = SimpleNamespace(command=lambda *args, **kwargs: calls.append((args, kwargs)))
        client = RobotClient("http://localhost", realtime=realtime)
        self.addCleanup(client.close)
        client.resume(expected_control_epoch=7)
        self.assertEqual(calls[0][1]["expected_control_epoch"], 7)
        self.assertNotIn("control_epoch", calls[0][0][1])
        self.assertNotIn("expected_control_epoch", calls[0][0][1])

    def test_uncertain_websocket_dispatch_never_falls_back_to_http(self):
        calls = []
        realtime = SimpleNamespace(command=lambda *args, **kwargs: (_ for _ in ()).throw(CommandOutcomeUnknown("unknown")))
        client = RobotClient("http://localhost", realtime=realtime)
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request)),
                                     base_url=client.base_url)
        self.addCleanup(client.close)
        with self.assertRaises(CommandOutcomeUnknown):
            client.stand()
        self.assertEqual(calls, [])

    def test_http_stop_fences_websocket_before_request_and_get_status_stays_http(self):
        order = []
        realtime = SimpleNamespace(cancel_commands=lambda reason: order.append("cancel"),
                                   command=lambda *args, **kwargs: self.fail("must use HTTP"))
        def handler(request):
            order.append(request.url.path)
            return httpx.Response(200, json={"ok": True, "action": "stop" if request.url.path == "/stop" else "get_status"})
        client = RobotClient("http://localhost", realtime=realtime)
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url)
        self.addCleanup(client.close)
        self.assertTrue(client.stop().ok)
        self.assertEqual(order, ["cancel", "/stop"])
        self.assertTrue(client.get_status().ok)
        self.assertEqual(order[-1], "/status")

    def test_emergency_stop_has_fresh_http_client_after_normal_close(self):
        order, options = [], []
        realtime = SimpleNamespace(cancel_commands=lambda reason: order.append("cancel"))
        client = RobotClient("http://localhost", timeout=5, realtime=realtime)
        client.close()
        factory = httpx.Client
        def new_client(**kwargs):
            options.append(kwargs)
            return factory(**kwargs, transport=httpx.MockTransport(lambda request: (
                order.append(request.url.path) or httpx.Response(200, json={"ok": True, "action": "stop"}))))
        with patch("brain.client.httpx.Client", new_client):
            self.assertTrue(client.emergency_stop().ok)
        self.assertEqual(order, ["cancel", "/stop"])
        self.assertEqual(options[0]["timeout"], 1.0)
        self.assertFalse(options[0]["trust_env"])
        self.assertFalse(options[0]["follow_redirects"])


if __name__ == "__main__":
    unittest.main()
