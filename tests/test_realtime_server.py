"""Transport bounds and cancellation races with fake sockets / explicit simulation."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from robot.realtime import RealtimeHub
from shared import Action, CommandResponse, ResumeCommand, RobotStatus, StopCommand
from shared.health import InputHealth


class Socket:
    def __init__(self, block_type=None):
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.block_type = block_type
        self.send_entered, self.send_release = asyncio.Event(), asyncio.Event()
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def receive(self):
        return await self.incoming.get()

    async def send_text(self, raw):
        message = json.loads(raw)
        if message["type"] == self.block_type:
            self.send_entered.set()
            await self.send_release.wait()
        await self.outgoing.put(message)

    async def close(self, code):
        self.closed = True

    async def send(self, hello, type_, **fields):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps({
            "version": 1, "boot_id": hello["boot_id"], "session_id": hello["session_id"],
            "type": type_, "request_id": uuid.uuid4().hex, **fields})})

    async def message(self, type_, timeout=1):
        async def matching():
            while True:
                message = await self.outgoing.get()
                if message["type"] == type_:
                    return message
        return await asyncio.wait_for(matching(), timeout)


async def until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(.002)


class RealtimeHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sockets, self.tasks = [], []
        self.stop_count, self.executions = 0, []
        self.control_epoch = 0
        self.sample_threads = []
        self.transition = threading.Lock()
        self.before_guard = None
        self.dispatch_entered = threading.Event()
        self.stop_wait = None
        self.stop_entered = threading.Event()

        def status():
            self.sample_threads.append(threading.get_ident())
            return RobotStatus(simulate=True, boot_id="boot", observed_at=time.time())

        def lease(expected_epoch, register):
            with self.transition:
                if expected_epoch != self.control_epoch:
                    return {"ok": False, "reason": "control epoch changed", "control_epoch": self.control_epoch}
                result = {"ok": True, "lease_id": uuid.uuid4().hex, "ttl_s": 2.0,
                          "control_epoch": self.control_epoch}
                return result if register(result) else {"ok": False, "reason": "cancelled", "control_epoch": self.control_epoch}

        def dispatch(action, command, authorize, status_provider):
            self.dispatch_entered.set()
            if self.before_guard is not None:
                if not self.before_guard.wait(2):
                    raise AssertionError("test failed to release dispatch")
            with self.transition:
                reason = authorize()
                if reason is None:
                    self.executions.append(action)
            return CommandResponse(ok=reason is None, action=action, detail=reason or "completed",
                                   command_id=command.command_id, status=status_provider())

        def stop():
            self.stop_entered.set()
            if self.stop_wait is not None:
                if not self.stop_wait.wait(2):
                    raise AssertionError("test failed to release stop")
            with self.transition:
                self.control_epoch += 1
                self.hub.invalidate_commands()
                self.stop_count += 1
            return CommandResponse(ok=True, action=Action.STOP)

        self.hub = RealtimeHub(boot_id_fn=lambda: "boot", epoch_fn=lambda: self.control_epoch, status_fn=status,
            dispatch_fn=dispatch, lease_fn=lease, stop_fn=stop,
            status_interval_s=.02, heartbeat_timeout_s=1.5, send_timeout_s=.15)

    async def asyncTearDown(self):
        if self.before_guard is not None:
            self.before_guard.set()
        if self.stop_wait is not None:
            self.stop_wait.set()
        for socket, task in zip(self.sockets, self.tasks):
            socket.send_release.set()
            if not task.done():
                await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 2)
        await self.hub.close()

    async def connect(self, socket=None):
        socket = socket or Socket()
        task = asyncio.create_task(self.hub.handle(socket))
        self.sockets.append(socket)
        self.tasks.append(task)
        return socket, task, await socket.message("hello")

    async def command(self, socket, hello, action=Action.RESUME):
        await socket.send(hello, "lease", control_epoch=self.control_epoch)
        issued = await socket.message("lease")
        await socket.send(hello, "command", action=action.value,
                          payload={"lease_id": issued["lease_id"], "command_id": uuid.uuid4().hex})

    async def test_single_sampler_off_event_loop_and_cached_status_requests(self):
        loop_thread = threading.get_ident()
        socket, task, hello = await self.connect()
        await socket.message("status")
        for _ in range(10):
            await socket.send(hello, "command", action="get_status", payload={})
            reply = await socket.message("result")
            self.assertTrue(reply["response"]["ok"])
        self.assertNotIn(loop_thread, self.sample_threads)
        self.assertEqual(len(set(self.sample_threads)), 1)
        self.assertLess(len(self.sample_threads), 10)
        await socket.incoming.put({"type": "websocket.disconnect"})
        await task
        other, _, _ = await self.connect()
        await other.message("status")
        self.assertEqual(len(set(self.sample_threads)), 1)

    async def test_rejected_worker_submission_releases_busy_and_preserves_the_unconsumed_lease(self):
        from robot.lifecycle import DaemonWorkers
        socket, _, hello = await self.connect()
        await socket.send(hello, "lease", control_epoch=self.control_epoch)
        issued = await socket.message("lease")
        workers = DaemonWorkers(capacity=1)
        self.hub.worker_fn = workers.submit
        release = threading.Event()
        blocked = workers.submit(release.wait, 2)
        payload = {"lease_id": issued["lease_id"], "command_id": "not-admitted"}
        try:
            await socket.send(hello, "command", action="resume", payload=payload)
            rejected = await socket.message("error")
            self.assertEqual(rejected["code"], "busy")
            self.assertEqual(self.executions, [])
            self.assertFalse(self.hub.health()["command_in_flight"])
            release.set()
            await blocked
            # Submission rejected before dispatch: the same still-current lease
            # and command ID have not been consumed and may be submitted once.
            await socket.send(hello, "command", action="resume", payload=payload)
            response = await socket.message("result")
            self.assertTrue(response["response"]["ok"])
            self.assertEqual(self.executions, [Action.RESUME])
            self.assertFalse(self.hub.health()["command_in_flight"])
        finally:
            release.set()
            await blocked

    async def test_slow_reader_disconnects_and_status_slot_replaces(self):
        socket, task, _ = await self.connect(Socket(block_type="status"))
        await socket.send_entered.wait()
        await until(lambda: self.hub.health()["status_samples"] >= 4)
        with self.hub._lock:
            self.assertIsNotNone(self.hub._owner.status)
            self.assertEqual(len(self.hub._owner.acknowledgements), 0)
        await asyncio.wait_for(task, .5)
        self.assertEqual(self.stop_count, 1)
        self.assertFalse(self.hub.has_owner)
        self.assertTrue(socket.closed)

    async def test_events_bounded_stop_preserved_and_no_replay(self):
        socket, task, _ = await self.connect(Socket(block_type="status"))
        await socket.send_entered.wait()
        event = lambda text: {"text": text, "timestamp": time.time(), "source": "dashboard"}
        for i in range(self.hub.EVENT_LIMIT):
            self.assertTrue(self.hub.publish_event(event(str(i)), control_epoch=self.control_epoch))
        self.assertFalse(self.hub.publish_event(event("full"), control_epoch=self.control_epoch))
        self.assertTrue(self.hub.publish_event(event("stop"), control_epoch=self.control_epoch, urgent=True))
        with self.hub._lock:
            self.assertEqual(len(self.hub._owner.events), 0)
        socket.send_release.set()
        self.assertEqual((await socket.message("event"))["event"]["text"], "stop")
        self.hub.publish_event(event("old hello"), control_epoch=self.control_epoch)
        await socket.incoming.put({"type": "websocket.disconnect"})
        await task
        other, _, _ = await self.connect()
        await other.message("status")
        with self.assertRaises(TimeoutError):
            await other.message("event", timeout=.05)

    async def test_disconnect_invalidates_admitted_resume_without_releasing_busy_early(self):
        self.before_guard = threading.Event()
        socket, task, hello = await self.connect()
        await self.command(socket, hello)
        await until(self.dispatch_entered.is_set)
        await socket.incoming.put({"type": "websocket.disconnect"})
        await task
        self.assertTrue(self.hub.health()["command_in_flight"])
        self.assertEqual(self.stop_count, 1)
        self.before_guard.set()
        await until(lambda: not self.hub.health()["command_in_flight"])
        self.assertEqual(self.executions, [])

    async def test_stop_generation_rejects_older_admission_then_allows_fresh_resume(self):
        self.before_guard = threading.Event()
        socket, _, hello = await self.connect()
        await self.command(socket, hello)
        await until(self.dispatch_entered.is_set)
        await asyncio.to_thread(self.hub.stop_fn)
        self.before_guard.set()
        response = (await socket.message("result"))["response"]
        self.assertFalse(response["ok"])
        self.assertIn("cancelled or stopped", response["detail"])
        self.assertEqual(self.executions, [])
        await self.command(socket, hello)
        self.assertTrue((await socket.message("result"))["response"]["ok"])

    async def test_old_sent_event_cannot_gain_lease_after_newer_stop_status(self):
        socket, _, hello = await self.connect()
        self.hub.publish_event({"text": "resume", "timestamp": time.time(), "source": "dashboard"},
                               control_epoch=self.control_epoch)
        old_event = await socket.message("event")
        await asyncio.to_thread(self.hub.stop_fn)
        while True:
            status = await socket.message("status")
            if status["control_epoch"] > old_event["control_epoch"]:
                break
        await socket.send(hello, "lease", control_epoch=old_event["control_epoch"])
        refused = await socket.message("lease")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["control_epoch"], status["control_epoch"])
        self.assertIsNone(refused["lease_id"])
        self.assertEqual(self.executions, [])

    async def test_cleanup_reserves_owner_until_stop_completes(self):
        self.stop_wait = threading.Event()
        first, task, _ = await self.connect()
        await first.incoming.put({"type": "websocket.disconnect"})
        await until(self.stop_entered.is_set)
        rejected = Socket()
        await self.hub.handle(rejected)
        self.assertEqual((await rejected.message("error"))["code"], "owner_exists")
        self.assertEqual(self.stop_count, 0)
        self.stop_wait.set()
        await task
        _, _, _ = await self.connect()
        self.assertEqual(self.stop_count, 1)


class ServerTransitionTests(unittest.TestCase):
    def setUp(self):
        from robot import server
        from robot.gait import GaitEngine
        from robot.control import CommandLeases
        self.server = server
        self.engine = GaitEngine(simulate=True)
        self.engine.battery_fn = lambda: 7.4
        patches = [patch.object(server, "engine", self.engine),
                   patch.object(server, "leases", CommandLeases()),
                   patch.object(server, "speaker", None),
                   patch.object(server, "command_lock", threading.Lock()),
                   patch.object(server, "control_transition_lock", threading.Lock()),
                   patch.object(server, "events_lock", threading.Lock()),
                   patch.object(server, "companion_events", queue.Queue(maxsize=32)),
                   patch.object(server, "companion_urgent", None),
                   patch.object(server, "companion_event_generation", 0)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_resume_result_updates_motion_without_refreshing_sensor_capture(self):
        self.server.stop(StopCommand())
        cached = RobotStatus(simulate=True, observed_at=1.0, motion_state={"stop_latched": True},
            inputs={"battery": InputHealth(available=True, provenance="simulated", captured_at=1.0)})
        issued = self.server.leases.issue()
        cmd = ResumeCommand(lease_id=issued["lease_id"], command_id="resume")
        with patch.object(self.server, "_status_with_distance", side_effect=AssertionError("extra sensor poll")):
            result = self.server._dispatch(Action.RESUME, cmd, status_provider=lambda: cached)
        self.assertTrue(result.ok)
        self.assertFalse(result.status.motion_state["stop_latched"])
        self.assertGreater(result.status.observed_at, cached.observed_at)
        self.assertEqual(result.status.inputs["battery"].captured_at, 1.0)
        self.assertTrue(cached.motion_state["stop_latched"])

    def test_http_stop_fences_in_flight_old_event_and_clears_queued_resume(self):
        self.server.companion_event(self.server.CompanionEvent(text="resume"))
        entered, release = threading.Event(), threading.Event()
        responses = []
        interpret = self.server.interpret
        def pause(text):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release event")
            return interpret(text)
        with patch.object(self.server, "interpret", pause):
            worker = threading.Thread(target=lambda: responses.append(
                self.server.companion_event(self.server.CompanionEvent(text="resume"))))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.server.stop(StopCommand())
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(responses[0]["ok"])
        self.assertEqual(self.server.read_companion_events()["events"], [])
        self.assertTrue(self.engine.motion_state()["stop_latched"])

    def test_epoch_is_boot_scoped_and_lease_issue_rejects_old_stop_authority(self):
        original_epoch = self.server.leases.control_epoch
        self.server.leases.invalidate()  # audio cancellation does not invalidate the event's meaning
        self.assertEqual(self.server.leases.control_epoch, original_epoch)
        self.server.stop(StopCommand())
        registered = []
        result = self.server._session_lease(original_epoch, lambda issued: registered.append(issued) or True)
        self.assertFalse(result["ok"])
        self.assertEqual(registered, [])
        self.assertGreater(result["control_epoch"], original_epoch)
        fresh = self.server._session_lease(result["control_epoch"], lambda issued: registered.append(issued) or True)
        self.assertTrue(fresh["ok"])
        self.assertEqual(len(registered), 1)
        self.assertEqual(type(self.server.leases)().control_epoch, 0)


if __name__ == "__main__":
    unittest.main()
