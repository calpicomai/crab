"""Companion faults use fake I/O only; no robot, capture, or playback."""
import signal
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from brain.pet.coordinator import Coordinator
from brain.pet.inputs import InputWorker
from brain.pet.voice import Voice
from brain.realtime import RealtimeClient, RealtimeUnavailable
from brain.pet.lifecycle import CleanupStack, FaultLatch, ProgressGuard
from brain.pet.persistence import PersistenceWorker


def until(predicate, timeout=1):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.002)
    raise AssertionError("condition did not complete within budget")


class PartialStartTests(unittest.TestCase):
    def test_realtime_second_thread_start_failure_closes_first(self):
        client = RealtimeClient("http://localhost:8000")
        with patch.object(client._reader, "start", side_effect=RuntimeError("start failed")):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                client.start()
        client.close()  # must not join the unstarted reader
        self.assertFalse(client._sender.is_alive())

    def test_input_partial_start_closes_started_threads_and_realtime(self):
        import queue
        worker = InputWorker("http://localhost:8000", "http://localhost:8100", queue.Queue())
        worker.threads[0]._target = lambda: worker.stop_event.wait()
        with patch.object(worker.threads[1], "start", side_effect=RuntimeError("start failed")):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                worker.start()
        worker.close()
        self.assertFalse(worker.threads[0].is_alive())

    def test_terminal_transport_fence_rejects_new_commands(self):
        client = RealtimeClient("http://localhost:8000")
        self.addCleanup(client.close)
        client.fence_commands("main progress expired")
        with self.assertRaisesRegex(RealtimeUnavailable, "progress expired"):
            client.command("resume", {})
        self.assertEqual(client.health()["command_fence"], "main progress expired")


class StopIsolationTests(unittest.TestCase):
    def test_process_exit_race_cannot_skip_remote_audio_stop(self):
        voice = Voice(False, None, stop_url="http://localhost:8000/audio/stop")
        process = unittest.mock.Mock()
        process.poll.return_value = None
        process.terminate.side_effect = ProcessLookupError("already exited")
        voice._process = process
        stopped = threading.Event()
        with patch.object(voice, "_stop_remote", stopped.set):
            voice.stop()
            self.assertTrue(stopped.wait(.5))
            voice.close()

    def test_optional_voice_exception_cannot_skip_fence_or_stop(self):
        voice = unittest.mock.Mock()
        voice.stop.side_effect = RuntimeError("audio failed")
        robot = unittest.mock.Mock()
        stopped = threading.Event()
        robot.emergency_stop.side_effect = lambda: (stopped.set() or {"ok": True, "action": "stop"})
        coordinator = Coordinator(robot, voice)
        self.addCleanup(coordinator.close)
        coordinator.request("stop", reason="operator stop")
        self.assertTrue(stopped.wait(.5))
        robot.cancel_commands.assert_called()
        self.assertTrue(coordinator.stopped)

    def test_voice_close_reports_unconfirmed_remote_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        voice = Voice(False, None, stop_url="http://localhost:8000/audio/stop")
        response = unittest.mock.Mock()
        response.json.return_value = {"ok": True}
        def post(*args, **kwargs):
            entered.set()
            release.wait(2)
            return response
        with patch("brain.pet.voice.httpx.post", post):
            started = time.monotonic()
            voice.close()
            self.assertLess(time.monotonic()-started, 1.5)
            self.assertTrue(entered.is_set())
            self.assertTrue(voice.health()["closed"])
            self.assertTrue(voice.health()["cancel_pending"])
            release.set()
            until(lambda: not voice.health()["cancel_pending"])


class ProgressTests(unittest.TestCase):
    def test_guard_uses_fresh_independent_http_after_regular_client_close(self):
        import httpx
        from brain.client import RobotClient
        link = RealtimeClient("http://localhost:8000")
        self.addCleanup(link.close)
        client = RobotClient(link.base_url, timeout=5, realtime=link)
        client.close()
        paths, options = [], []
        factory = httpx.Client
        def fresh(**kwargs):
            options.append(kwargs)
            return factory(**kwargs, transport=httpx.MockTransport(lambda request: (
                paths.append(request.url.path) or httpx.Response(200, json={"ok": True, "action": "stop"}))))
        guard = ProgressGuard(link.fence_commands, client.emergency_stop,
                              timeout_s=.02, check_s=.005)
        self.addCleanup(guard.close)
        with patch("brain.client.httpx.Client", fresh):
            guard.start()
            until(lambda: guard.health()["stop"]["state"] == "acknowledged")
        self.assertEqual(paths, ["/stop"])
        self.assertEqual(options[0]["timeout"], 1)
        self.assertFalse(options[0]["trust_env"])
        self.assertFalse(options[0]["follow_redirects"])

    def test_real_main_stall_latches_fault_publishes_it_and_stops_once(self):
        import queue
        from brain.pet import loop
        release, stopped = threading.Event(), threading.Event()
        calls, states = [], []
        class Client:
            base_url = "http://localhost:8000"
            def __init__(self, *args, **kw):
                self.realtime = kw.get("realtime")
            def cancel_commands(self, *args):
                self.realtime.cancel_commands()
            def stop(self):
                calls.append("stop")
                stopped.set()
                release.set()  # main owner recovers after the independent STOP
                return {"ok": True, "action": "stop"}
            emergency_stop = stop
            def close(self): pass
        class BlockedSamples:
            def get_nowait(self):
                self.assert_released = release.wait(3)
                raise queue.Empty
        class Inputs:
            def __init__(self, *args, **kw): self.samples = BlockedSamples()
            def start(self): pass
            def close(self): pass
        voice = SimpleNamespace(enabled=False, busy=False, stop=lambda: None,
            close=lambda: None, health=lambda: {}, listening_suppressed=lambda: False)
        dashboard = SimpleNamespace(push=states.append, health=lambda: {}, close=lambda: None)
        with patch.object(loop, "RobotClient", Client), patch.object(loop, "InputWorker", Inputs), \
             patch.object(loop, "Voice", return_value=voice), patch.object(loop, "Dashboard", return_value=dashboard):
            result = loop.main(["--no-llm", "--no-stt", "--no-camera", "--max-ticks", "3",
                "--memory-db", ":memory:", "--identity-file", ":memory:"])
        self.assertEqual(result, 1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(calls, ["stop"])
        self.assertTrue(states[-1]["progress"]["requires_restart"])
        self.assertTrue(states[-1]["stopped"])
        self.assertIn("progress expired", states[-1]["runtime_fault"])

    def test_stalled_owner_stops_once_despite_healthy_websocket(self):
        from tests.test_realtime_client import FakeServer
        server = FakeServer()
        link = RealtimeClient("http://localhost:8000", status_timeout_s=2)
        self.addCleanup(link.close)
        calls = []
        guard = ProgressGuard(link.fence_commands,
            lambda: (calls.append("stop") or {"ok": True, "action": "stop"}),
            timeout_s=.06, check_s=.005)
        self.addCleanup(guard.close)
        with patch("brain.realtime.connect", server.connect):
            link.start()
            self.assertTrue(link._ready.wait(1))
            self.assertTrue(link._reader.is_alive())
            guard.start()
            until(lambda: guard.health()["stop"]["state"] == "acknowledged")
            self.assertFalse(link.health()["ready"])
            self.assertTrue(link.health()["closed"])
            self.assertTrue(server.connections[0].closed.is_set())
            guard.checkpoint()  # recovery cannot rearm the guard or transport
            with self.assertRaisesRegex(RealtimeUnavailable, "progress expired"):
                link.command("resume", {})
            time.sleep(.08)
            self.assertEqual(calls, ["stop"])
            self.assertTrue(guard.health()["requires_restart"])

    def test_failed_fence_cannot_suppress_stop_and_deadline_is_unknown(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def stop():
            entered.set()
            release.wait()
            return {"ok": True, "action": "stop"}
        def fence(reason):
            raise RuntimeError("broken fence")
        guard = ProgressGuard(fence, stop, timeout_s=.02, check_s=.005, stop_timeout_s=.03)
        self.addCleanup(guard.close)
        guard.start()
        self.assertTrue(entered.wait(.5))
        until(lambda: guard.health()["stop"]["state"] == "unknown")
        self.assertIn("broken fence", guard.health()["fence_error"])
        self.assertTrue(guard.fault.reason)

    def test_fault_dominates_pending_and_fresh_resume_and_late_success(self):
        fault = FaultLatch()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        robot = unittest.mock.Mock()
        def resume():
            entered.set()
            release.wait(2)
            return {"ok": True, "action": "resume"}
        robot.resume.side_effect = resume
        robot.emergency_stop.side_effect = lambda: (stopped.set() or {"ok": True, "action": "stop"})
        voice = unittest.mock.Mock(busy=False)
        voice.health.return_value = {}
        coordinator = Coordinator(robot, voice, fault_latch=fault)
        self.addCleanup(coordinator.close)
        coordinator.observe({"boot_id": "fake", "inputs": {}})
        coordinator.request("resume")
        coordinator.tick()
        self.assertTrue(entered.wait(.5))
        fault.trip("owner progress expired; restart required")
        release.set()
        self.assertTrue(stopped.wait(.5))  # late resume cleanup runs on its owner
        until(lambda: coordinator._resume[1].done())
        coordinator.tick()
        result = coordinator.request("resume")
        coordinator.tick()
        self.assertEqual(result.status.value, "blocked")
        self.assertTrue(coordinator.stopped)
        self.assertEqual(robot.resume.call_count, 1)


class PersistenceTests(unittest.TestCase):
    def test_blocked_write_and_cache_refresh_never_block_behavior(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        store = unittest.mock.Mock()
        store.interaction_history.return_value = {"greetings": 2}
        store.summary.return_value = "cached evidence"
        store.remember.side_effect = lambda **kw: (entered.set(), release.wait())
        worker = PersistenceWorker(":memory:", capacity=2, store_factory=lambda path: store)
        self.addCleanup(worker.close)
        worker.start()
        self.assertTrue(worker.remember(observed_facts={"utterance": "hello"}, source="user"))
        self.assertTrue(entered.wait(.5))
        started = time.monotonic()
        for _ in range(100):
            worker.remember(observed_facts={"utterance": "data"}, source="user")
            self.assertEqual(worker.summary(), "cached evidence")
            self.assertEqual(worker.interaction_history()["greetings"], 2)
        self.assertLess(time.monotonic()-started, .1)
        self.assertEqual(worker.health()["pending_records"], 2)
        self.assertGreater(worker.health()["records_dropped"], 0)
        worker.close(timeout=.01)
        self.assertFalse(store.close.called)  # only the owner may close blocked SQLite
        release.set()
        until(lambda: store.close.called)

    def test_blocked_log_flush_replaces_pending_and_close_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        store = unittest.mock.Mock()
        store.interaction_history.return_value = {}
        store.summary.return_value = "cached"
        log = unittest.mock.Mock()
        log.flush.side_effect = lambda: (entered.set(), release.wait())
        worker = PersistenceWorker(":memory:", "fake.log", store_factory=lambda path: store,
                                   open_log=lambda *a, **kw: log)
        self.addCleanup(worker.close)
        worker.start()
        worker.log({"tick": 0})
        self.assertTrue(entered.wait(.5))
        started = time.monotonic()
        for tick in range(100):
            worker.log({"tick": tick})
        worker.close(timeout=.01)
        self.assertLess(time.monotonic()-started, .1)
        self.assertGreater(worker.health()["logs_replaced"], 0)
        release.set()
        until(lambda: log.close.called)
        self.assertEqual(log.write.call_args.args[0], '{"tick": 99}\n')

    def test_log_open_failure_closes_memory_owner(self):
        store = unittest.mock.Mock()
        worker = PersistenceWorker(":memory:", "bad.log", store_factory=lambda path: store,
            open_log=unittest.mock.Mock(side_effect=OSError("disk failed")))
        with self.assertRaisesRegex(RuntimeError, "disk failed"):
            worker.start()
        worker.close()
        store.close.assert_called_once()

    def test_constructor_schema_failure_closes_sqlite_connection(self):
        from brain.pet.memory import MemoryStore
        connection = unittest.mock.Mock()
        connection.execute.side_effect = RuntimeError("schema read failed")
        with patch("brain.pet.memory.sqlite3.connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "schema read failed"):
                MemoryStore()
        connection.close.assert_called_once()


class OwnershipTests(unittest.TestCase):
    def test_cleanup_failure_cannot_skip_other_owners(self):
        stack = CleanupStack()
        calls = []
        stack.own(None, close=lambda: calls.append("last"))
        def broken():
            calls.append("broken")
            raise RuntimeError("cleanup failed")
        stack.own(None, close=broken)
        stack.own(None, close=lambda: calls.append("first"))
        stack.close()
        self.assertEqual(calls, ["first", "broken", "last"])
        self.assertIn("cleanup failed", stack.errors[0])
        stack.close()
        self.assertEqual(len(calls), 3)

    def test_main_constructor_failure_closes_every_previous_owner(self):
        from brain.pet import loop
        closed = []
        def owner(name, **values):
            return SimpleNamespace(close=lambda: closed.append(name), **values)
        memory = owner("memory", start=lambda: None, interaction_history=lambda: {})
        realtime = owner("realtime")
        client = owner("client", base_url="http://localhost:8000")
        with patch.object(loop, "PetIdentity", return_value=object()), \
             patch.object(loop, "PersistenceWorker", return_value=memory), \
             patch.object(loop, "RealtimeClient", return_value=realtime), \
             patch.object(loop, "RobotClient", side_effect=[client, RuntimeError("control constructor failed")]):
            with self.assertRaisesRegex(RuntimeError, "control constructor failed"):
                loop.main(["--no-llm", "--no-stt"])
        self.assertCountEqual(closed, ["memory", "realtime", "client"])

    def test_dashboard_partial_constructor_cleans_vitals(self):
        from brain.dashboard import Dashboard
        vitals = unittest.mock.Mock()
        with patch("brain.dashboard.VitalsSampler", return_value=vitals), \
             patch("brain.dashboard.threading.Thread.start", side_effect=RuntimeError("publisher start failed")):
            with self.assertRaisesRegex(RuntimeError, "publisher start failed"):
                Dashboard("http://localhost:8000", True)
        vitals.close.assert_called_once()


class ProcessExitTests(unittest.TestCase):
    def test_blocked_control_owner_does_not_hold_interpreter_exit(self):
        code = '''
import threading
from types import SimpleNamespace
from brain.pet.coordinator import Coordinator
resume_entered = threading.Event()
class Robot:
    realtime = SimpleNamespace(fence_commands=lambda reason: print("FENCED", flush=True))
    def cancel_commands(self): pass
    def stop(self):
        print("STOP_ATTEMPTED", flush=True)
        threading.Event().wait()
    emergency_stop = stop
    def resume(self):
        resume_entered.set()
        threading.Event().wait()
class Voice:
    def stop(self): pass
c = Coordinator(Robot(), Voice())
c.observe({"boot_id": "fake", "inputs": {}})
c.request("resume")
c.tick()
assert resume_entered.wait(.5)
c.close()
assert c.request("resume").status.value == "blocked"
assert c.stopped
assert sum(item.get("outcome") == "unknown" for item in c._movements) >= 2
print("CLOSE_RETURNED", flush=True)
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FENCED", result.stdout)
        self.assertIn("STOP_ATTEMPTED", result.stdout)
        self.assertIn("CLOSE_RETURNED", result.stdout)

    def test_sigterm_unwinds_real_main_and_closes_remaining_owners(self):
        code = '''
import queue
from unittest.mock import patch
from brain.pet import loop
class Client:
    base_url = "http://localhost:8000"
    def __init__(self, *args, **kw): self.realtime = kw.get("realtime")
    def cancel_commands(self, *args): pass
    def stop(self):
        print("STOP_ACK", flush=True)
        return {"ok": True, "action": "stop"}
    emergency_stop = stop
    def close(self): print("CLIENT_CLOSED", flush=True)
class Inputs:
    def __init__(self, *args, **kw): self.samples = queue.Queue()
    def start(self): print("READY_FOR_TERM", flush=True)
    def close(self): print("INPUTS_CLOSED", flush=True)
class Voice:
    enabled = busy = False
    def __init__(self, **kw): pass
    def stop(self): pass
    def close(self): pass
    def health(self): return {}
    def listening_suppressed(self): return False
with patch.object(loop, "RobotClient", Client), patch.object(loop, "InputWorker", Inputs), patch.object(loop, "Voice", Voice):
    raise SystemExit(loop.main(["--no-llm", "--no-stt", "--no-camera", "--no-voice",
        "--memory-db", ":memory:", "--identity-file", ":memory:"]))
'''
        child = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            # A pipe reader avoids an unbounded readline if startup regresses.
            ready = threading.Event()
            lines = []
            def read():
                for line in child.stdout:
                    lines.append(line)
                    if "READY_FOR_TERM" in line:
                        ready.set()
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            self.assertTrue(ready.wait(3))
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=4)
            reader.join(1)
            output = "".join(lines)
            self.assertEqual(child.returncode, 128+signal.SIGTERM, child.stderr.read())
            self.assertIn("STOP_ACK", output)
            self.assertIn("INPUTS_CLOSED", output)
            self.assertEqual(output.count("CLIENT_CLOSED"), 2)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
            child.stderr.close()


if __name__ == "__main__":
    unittest.main()
