"""Pi software shutdown receipts using fakes and owned loopback processes only."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from websockets.sync.client import connect

from robot import config
from robot.control import CommandLeases
from robot.gait import GaitEngine
from robot.lifecycle import DaemonWorkers, OwnedRoute
from shared import (
    ACTION_PATHS, AUDIO_LEASE_HEADER, AUDIO_PLAY_PATH, AUDIO_STOP_PATH, AUDIO_SEQUENCE_HEADER,
    AUDIO_SESSION_HEADER, CONTROL_LEASE_PATH, Action, ResumeCommand,
)
from shared.realtime import WS_PATH


class WorkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_thread_start_cancels_its_future_and_releases_capacity(self):
        workers = DaemonWorkers(capacity=1)
        future = asyncio.get_running_loop().create_future()
        with patch.object(asyncio.get_running_loop(), "create_future", return_value=future), \
             patch.object(threading.Thread, "start", side_effect=RuntimeError("fixture start failed")):
            with self.assertRaisesRegex(RuntimeError, "fixture start failed"):
                workers.submit(lambda: "never started")
        self.assertTrue(future.cancelled(), "failed submission must not leave an orphan pending future")
        self.assertEqual(await workers.submit(lambda: "capacity recovered"), "capacity recovered")

    async def test_request_flood_is_rejected_while_reserved_stop_lane_remains_responsive(self):
        from fastapi import FastAPI
        from robot import lifecycle
        application = FastAPI()
        application.router.route_class = OwnedRoute
        release = threading.Event()
        entered = []
        normal = DaemonWorkers(capacity=2)
        with patch.object(lifecycle, "request_workers", normal):
            @application.get("/blocked")
            def blocked():
                entered.append(threading.get_ident())
                release.wait(2)
                return {"ok": True}
            @application.post("/stop")
            def stop():
                return {"stopped": True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://fixture") as client:
            running = [asyncio.create_task(client.get("/blocked")) for _ in range(2)]
            try:
                deadline = time.monotonic()+1
                while len(entered) < 2 and time.monotonic() < deadline:
                    await asyncio.sleep(.005)
                self.assertEqual(len(entered), 2)
                flooded = await asyncio.gather(*(client.get("/blocked") for _ in range(12)))
                self.assertEqual([response.status_code for response in flooded], [503]*12)
                stopped = await asyncio.wait_for(client.post("/stop"), .3)
                self.assertEqual(stopped.json(), {"stopped": True})
                self.assertFalse(release.is_set())
                self.assertEqual(len(entered), 2, "rejected work must never be queued")
            finally:
                release.set()
                await asyncio.gather(*running)


class LifespanShutdownTests(unittest.TestCase):
    def setUp(self):
        with patch.object(config, "SIMULATE", True), patch.object(config, "ACTUATORS_ENABLED", False):
            from robot import server
        self.server = server
        self.stack = self.enterContext(ExitStack())
        self.engine = GaitEngine(simulate=True)
        self.leases = CommandLeases()
        self.vitals = Mock()
        for name, value in {
            "engine": self.engine, "leases": self.leases, "speaker": None,
            "camera": None, "_host_vitals": self.vitals,
            "realtime_hub": server._make_realtime_hub(),
        }.items():
            self.stack.enter_context(patch.object(server, name, value))

    def test_lifespan_without_websocket_latches_stop_and_revokes_prior_authority(self):
        lease = self.leases.issue()

        async def run():
            async with self.server.lifespan(self.server.app):
                self.assertFalse(self.engine.motion_state()["stop_latched"])

        asyncio.run(run())
        self.assertTrue(self.engine.motion_state()["stop_latched"])
        self.assertIsNotNone(self.leases.consume(lease["lease_id"], "old-command"))
        self.assertGreater(self.leases.control_epoch, 0)
        self.vitals.close.assert_called_once_with()

    def test_partial_startup_failure_still_stops_and_attempts_every_owner(self):
        camera = Mock()
        camera.start.side_effect = RuntimeError("camera startup fixture")
        camera.stop.side_effect = RuntimeError("camera cleanup fixture")
        speaker = Mock()
        speaker.close.side_effect = RuntimeError("speaker cleanup fixture")

        async def run():
            async with self.server.lifespan(self.server.app):
                self.fail("startup must fail before serving")

        with patch.object(self.server, "camera", camera), patch.object(self.server, "speaker", speaker):
            with self.assertRaisesRegex(RuntimeError, "camera startup fixture"):
                asyncio.run(run())
        self.assertTrue(self.engine.motion_state()["stop_latched"])
        speaker.close.assert_called_once()
        camera.stop.assert_called_once()
        self.vitals.close.assert_called_once_with()

    def test_terminal_shutdown_rejects_late_resume_and_new_http_or_session_leases(self):
        entered, release = threading.Event(), threading.Event()
        results = []

        async def run():
            async with self.server.lifespan(self.server.app):
                lease = self.server.command_lease()
                command = ResumeCommand(lease_id=lease["lease_id"], command_id="late-resume")
                def delayed():
                    entered.set()
                    release.wait(2)
                    results.append(self.server.resume(command))
                worker = threading.Thread(target=delayed, daemon=True)
                worker.start()
                self.assertTrue(entered.wait(1))
                self.server._shutdown.request()
                self.assertFalse(self.server.command_lease()["ok"])
                await self.server._shutdown.close()
                current_epoch = self.leases.control_epoch
                self.assertFalse(self.server._session_lease(current_epoch, lambda lease: True)["ok"])
                release.set()
                await asyncio.to_thread(worker.join, 1)
                self.assertFalse(worker.is_alive())
                self.assertFalse(results[0].ok)
                self.assertTrue(self.engine.motion_state()["stop_latched"])
        try:
            asyncio.run(run())
        finally:
            release.set()

    def test_shutdown_closes_sse_without_waiting_for_a_blocked_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        def snapshot():
            entered.set()
            release.wait(2)
            return {}
        async def run():
            async with self.server.lifespan(self.server.app):
                stream = self.server._dashboard_events(Mock(is_disconnected=AsyncMock(return_value=False)))
                pending = asyncio.create_task(anext(stream))
                while not entered.is_set():
                    await asyncio.sleep(.005)
                await self.server._shutdown.close()
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(pending, .3)
                self.assertFalse(release.is_set())
                self.assertTrue(self.engine.motion_state()["stop_latched"])
        try:
            with patch.object(self.server, "_dashboard_state", snapshot):
                asyncio.run(run())
        finally:
            release.set()

    def test_shutdown_signal_during_lease_consumption_rejects_the_older_resume(self):
        entered, release = threading.Event(), threading.Event()
        consume = self.leases.consume
        def paused_consume(*args):
            result = consume(*args)
            entered.set()
            release.wait(2)
            return result
        results = []
        async def run():
            async with self.server.lifespan(self.server.app):
                lease = self.server.command_lease()
                command = ResumeCommand(lease_id=lease["lease_id"], command_id="in-flight-resume")
                worker = threading.Thread(target=lambda: results.append(self.server.resume(command)), daemon=True)
                worker.start()
                self.assertTrue(entered.wait(1))
                self.server._shutdown.request()
                release.set()
                await self.server._shutdown.close()
                worker.join(1)
                self.assertFalse(results[0].ok)
                self.assertTrue(self.engine.motion_state()["stop_latched"])
        try:
            with patch.object(self.leases, "consume", paused_consume):
                asyncio.run(run())
        finally:
            release.set()

    def test_a_late_camera_start_cannot_reopen_the_owner_after_shutdown(self):
        entered, release, closed_after_start = threading.Event(), threading.Event(), threading.Event()
        running = []
        def start():
            entered.set()
            release.wait(2)
            running.append(True)
        def stop():
            if running:
                running.clear()
                closed_after_start.set()
        camera = Mock(start=start, stop=stop)
        async def run():
            lifecycle = self.server.lifespan(self.server.app)
            starting = asyncio.create_task(lifecycle.__aenter__())
            while not entered.is_set():
                await asyncio.sleep(.005)
            self.server._shutdown.request()
            with self.assertRaisesRegex(RuntimeError, "shutdown requested during startup"):
                await asyncio.wait_for(starting, 1.5)
            release.set()
            deadline = time.monotonic()+.5
            while not closed_after_start.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            self.assertTrue(closed_after_start.is_set())
            self.assertEqual(running, [])
        try:
            with patch.object(self.server, "camera", camera):
                asyncio.run(run())
        finally:
            release.set()

    def test_self_test_delayed_in_diagnostics_cannot_admit_playback_after_the_terminal_fence(self):
        from robot import audio
        from robot.lifecycle import request_workers
        entered, release = threading.Event(), threading.Event()
        playback = []
        def run_process(command, **kwargs):
            if command[-1] == "-l":
                entered.set()
                release.wait(2)
                return 0, "fake device diagnostics"
            playback.append(command)
            return 0, ""
        speaker = audio.PiSpeaker(simulate=False)
        async def run():
            async with self.server.lifespan(self.server.app):
                pending = request_workers.submit(self.server.audio_self_test, self.server.AudioSelfTest(play=True))
                while not entered.is_set():
                    await asyncio.sleep(.005)
                await self.server._shutdown.close()
                release.set()
                await pending
                self.assertEqual(playback, [], "late self-test must never invoke the player")
                self.assertFalse(speaker.last_result["played"])
                self.assertIn("shutting down", speaker.last_result["reason"])
        try:
            with patch.object(self.server, "speaker", speaker), patch.object(audio, "_APLAY", "fake-aplay"), \
                 patch.object(audio, "_ARECORD", None), patch.object(audio, "run_audio_process", run_process), \
                 patch.object(speaker, "close", return_value={"ok": False, "reason": "fixture cleanup pending"}), \
                 patch.object(audio.Path, "read_text", return_value=""), patch.object(audio.glob, "glob", return_value=[]):
                asyncio.run(run())
        finally:
            release.set()


CHILD = r'''
import json, os, threading, time
from robot import lifecycle
if os.environ.get("BLOCK_STATS"):
    lifecycle.request_workers = lifecycle.DaemonWorkers(capacity=2)
from robot import server
def receipt(name, **fields):
    with open(os.environ["SHUTDOWN_TRACE"], "a") as out:
        out.write(json.dumps({"name": name, "at": time.monotonic(), **fields}) + "\n")
original_stop = server.engine.stop
def stop():
    result = original_stop()
    receipt("stop", latched=server.engine.motion_state()["stop_latched"])
    return result
server.engine.stop = stop
class Camera:
    def start(self): receipt("camera_start")
    def stop(self): receipt("camera_close")
    def stats(self):
        if os.environ.get("BLOCK_STATS"):
            receipt("stats_entered")
            threading.Event().wait(30)
        return {"available": False}
    def input_health(self):
        return {"available": False, "provenance": "simulated", "failure_reason": "fixture camera unavailable"}
class Vitals:
    def start(self): receipt("vitals_start")
    def close(self): receipt("vitals_close")
    def snapshot(self): return {}
class Speaker:
    simulate = True
    last_result = None
    def health(self): return {"available": False, "provenance": "simulated"}
    def stop(self, **kwargs):
        receipt("speaker_stop")
        if os.environ.get("BLOCK_SPEAKER_STOP"):
            until = time.monotonic()+30
            while not os.path.exists(os.environ["SHUTDOWN_TRACE"] + ".speaker-release") and time.monotonic() < until:
                time.sleep(.01)
        return {"ok": True}
    def close(self, **kwargs):
        receipt("speaker_close")
        if os.environ.get("HOLD_CLEANUP"):
            until = time.monotonic()+30
            while not os.path.exists(os.environ["SHUTDOWN_TRACE"] + ".cleanup-release") and time.monotonic() < until:
                time.sleep(.01)
        if os.environ.get("FAIL_CLEANUP"):
            raise RuntimeError("fake amplifier/GPIO cleanup failed")
        return {"ok": True}
    def play_result(self, data, **kwargs):
        reason = kwargs["authorize"]()
        if reason: return {"ok": False, "reason": reason}
        receipt("playback_entered")
        threading.Event().wait(30)
        return {"ok": False, "played": False}
server.camera, server._host_vitals, server.speaker = Camera(), Vitals(), Speaker()
def blocked_stand():
    receipt("dispatch_entered")
    threading.Event().wait(30)
    return server.engine.stop()
server.engine.stand = blocked_stand
original_dispatch = server._dispatch
def delayed_dispatch(action, command, guard, status):
    if os.environ.get("BLOCK_WS") and action == server.Action.RESUME:
        receipt("ws_resume_entered")
        while not os.path.exists(os.environ["SHUTDOWN_TRACE"] + ".release"):
            time.sleep(.01)
        result = original_dispatch(action, command, guard, status)
        receipt("late_resume", ok=result.ok, latched=server.engine.motion_state()["stop_latched"])
        return result
    return original_dispatch(action, command, guard, status)
server.realtime_hub.dispatch_fn = delayed_dispatch
receipt("boot", simulate=server.engine.simulate, enabled=server.engine.enabled,
        driver_constructed=server.engine._crawler is not None, homing=server.config.HOME_ON_START)
server.main()
'''


class OwnedServerShutdownTests(unittest.TestCase):
    def start_child(self, *, cleanup_failure=False, simulate=True, blocked_ws=False, hold_cleanup=False, blocked_stats=False,
                    blocked_speaker_stop=False):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.trace = Path(temporary) / "trace.jsonl"
        log = self.enterContext(open(Path(temporary) / "server.log", "w+"))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = dict(os.environ)
        environment.update(
            PICRAWLER_HOST="127.0.0.1", PICRAWLER_PORT=str(port),
            PICRAWLER_SIMULATE="1" if simulate else "0", PICRAWLER_ACTUATORS_ENABLED="0",
            PICRAWLER_HOME_ON_START="none", PICRAWLER_CAMERA_ENABLED="0",
            PICRAWLER_AUDIO_ENABLED="0", PICRAWLER_ULTRASONIC_ENABLED="0",
            PICRAWLER_BATTERY_ENABLED="0", SHUTDOWN_TRACE=str(self.trace),
        )
        if cleanup_failure:
            environment["FAIL_CLEANUP"] = "1"
        if blocked_ws:
            environment["BLOCK_WS"] = "1"
        if hold_cleanup:
            environment["HOLD_CLEANUP"] = "1"
        if blocked_stats:
            environment["BLOCK_STATS"] = "1"
        if blocked_speaker_stop:
            environment["BLOCK_SPEAKER_STOP"] = "1"
        self.child = subprocess.Popen([sys.executable, "-c", CHILD], env=environment,
                                      stdout=log, stderr=subprocess.STDOUT)
        def terminate():
            if self.child.poll() is None:
                self.child.kill()
            self.child.wait(timeout=3)
        self.addCleanup(terminate)
        self.url = f"http://127.0.0.1:{port}"
        self.http = self.enterContext(httpx.Client(base_url=self.url, timeout=5, trust_env=False))
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            try:
                if self.http.get("/sim/clock").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if self.child.poll() is not None:
                break
            time.sleep(.01)
        log.seek(0)
        self.fail("owned loopback server failed to start: " + log.read())

    def receipts(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def wait_receipts(self, *names):
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if set(names) <= {item["name"] for item in self.receipts()}:
                return
            time.sleep(.01)
        self.fail(f"missing receipts {names}: {self.receipts()}")

    def fill_stop_lane(self, path):
        for index in range(8):
            def post(index=index):
                try:
                    self.http.post(path, json={}, headers={AUDIO_SESSION_HEADER: f"stop-lane-{index}",
                                                          AUDIO_SEQUENCE_HEADER: "1"})
                except httpx.TransportError:
                    pass
            worker = threading.Thread(target=post, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 1)
        self.addCleanup(lambda: Path(str(self.trace)+".speaker-release").touch())
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if sum(item["name"] == "speaker_stop" for item in self.receipts()) == 8:
                return
            time.sleep(.005)
        self.fail(f"fake speaker cancellation did not fill its bounded lane: {self.receipts()}")

    def motion_latched(self):
        return self.http.get("/sim/state").json()["robot_runtime"]["motion"]["stop_latched"]

    def fresh_resume(self):
        lease = self.http.post(CONTROL_LEASE_PATH).json()["lease_id"]
        response = self.http.post(ACTION_PATHS[Action.RESUME], json={"lease_id": lease,
                                                                    "command_id": f"resume-{time.monotonic_ns()}"})
        self.assertTrue(response.json()["ok"], response.text)
        self.assertFalse(self.motion_latched())

    def assert_bounded_exit(self, sig=signal.SIGTERM):
        signalled = time.monotonic()
        self.child.send_signal(sig)
        self.wait_receipts("stop", "speaker_close", "camera_close", "vitals_close")
        receipts = self.receipts()
        first_stop = next(item for item in receipts if item["name"] == "stop" and item["at"] >= signalled)
        self.assertTrue(first_stop["latched"])
        self.assertLess(first_stop["at"]-signalled, .5, receipts)
        try:
            self.child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.fail(f"owned server did not exit within software shutdown bound: {receipts}")
        self.assertLess(time.monotonic()-signalled, 4)

    def test_sigterm_stops_with_open_sse_and_websocket_before_draining(self):
        self.start_child()
        websocket = self.enterContext(connect(self.url.replace("http:", "ws:")+WS_PATH,
                                                proxy=None, close_timeout=.2, ping_interval=None))
        self.assertEqual(json.loads(websocket.recv(timeout=1))["type"], "hello")
        response = self.enterContext(self.http.stream("GET", "/sim/events"))
        lines = response.iter_lines()
        self.assertEqual(next(lines), "event: state")
        self.assert_bounded_exit()

    def test_sigterm_bypasses_blocked_http_dispatch_and_playback_and_isolates_cleanup_failure(self):
        self.start_child(cleanup_failure=True)
        def post(path, **kwargs):
            try:
                self.http.post(path, **kwargs)
            except httpx.TransportError:
                pass
        for path, kwargs in (
            (ACTION_PATHS[Action.STAND], {"json": {"lease_id": self.http.post(CONTROL_LEASE_PATH).json()["lease_id"], "command_id": "blocked-motion"}}),
            (AUDIO_PLAY_PATH, {"content": b"fake-wav", "headers": {
                AUDIO_SESSION_HEADER: "shutdown-fixture", AUDIO_SEQUENCE_HEADER: "1",
                AUDIO_LEASE_HEADER: self.http.post(CONTROL_LEASE_PATH).json()["lease_id"]}}),
        ):
            worker = threading.Thread(target=post, args=(path,), kwargs=kwargs, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 1)
        self.wait_receipts("dispatch_entered", "playback_entered")
        self.assert_bounded_exit()

    def test_sigint_without_websocket_preserves_hardware_disabled_default(self):
        self.start_child(simulate=False)
        boot = self.receipts()[0]
        self.assertEqual({key: boot[key] for key in ("simulate", "enabled", "driver_constructed", "homing")},
                         {"simulate": False, "enabled": False, "driver_constructed": False, "homing": "none"})
        response = self.enterContext(self.http.stream("GET", "/sim/events"))
        lines = response.iter_lines()
        self.assertEqual(next(lines), "event: state")
        self.assert_bounded_exit(signal.SIGINT)

    def test_sigterm_with_open_sse_and_blocked_websocket_dispatch_exits(self):
        self.start_child(blocked_ws=True)
        websocket = self.enterContext(connect(self.url.replace("http:", "ws:")+WS_PATH,
                                              proxy=None, close_timeout=.2, ping_interval=None))
        hello = json.loads(websocket.recv(timeout=1))
        envelope = {"boot_id": hello["boot_id"], "session_id": hello["session_id"]}
        websocket.send(json.dumps({"version": 1, "type": "lease", **envelope,
                                   "request_id": "shutdown-lease", "control_epoch": hello["control_epoch"]}))
        while True:
            lease = json.loads(websocket.recv(timeout=1))
            if lease["type"] == "lease": break
        websocket.send(json.dumps({"version": 1, "type": "command", **envelope,
            "request_id": "blocked-resume", "action": "resume",
            "payload": {"lease_id": lease["lease_id"], "command_id": "blocked-resume"}}))
        self.wait_receipts("ws_resume_entered")
        response = self.enterContext(self.http.stream("GET", "/sim/events"))
        lines = response.iter_lines()
        self.assertEqual(next(lines), "event: state")
        self.assert_bounded_exit()

    def test_late_websocket_resume_after_sigterm_cannot_clear_stop_or_acquire_authority(self):
        self.start_child(blocked_ws=True, hold_cleanup=True)
        websocket = self.enterContext(connect(self.url.replace("http:", "ws:")+WS_PATH,
                                              proxy=None, close_timeout=.2, ping_interval=None))
        hello = json.loads(websocket.recv(timeout=1))
        envelope = {"boot_id": hello["boot_id"], "session_id": hello["session_id"]}
        websocket.send(json.dumps({"version": 1, "type": "lease", **envelope,
                                   "request_id": "shutdown-lease", "control_epoch": hello["control_epoch"]}))
        while True:
            lease = json.loads(websocket.recv(timeout=1))
            if lease["type"] == "lease": break
        websocket.send(json.dumps({"version": 1, "type": "command", **envelope,
            "request_id": "blocked-resume", "action": "resume",
            "payload": {"lease_id": lease["lease_id"], "command_id": "blocked-resume"}}))
        self.wait_receipts("ws_resume_entered")
        started = time.monotonic()
        self.child.send_signal(signal.SIGTERM)
        self.wait_receipts("stop", "speaker_close")
        self.assertFalse(self.http.post(CONTROL_LEASE_PATH).json()["ok"])
        Path(str(self.trace)+".release").touch()
        self.wait_receipts("late_resume")
        late = next(item for item in self.receipts() if item["name"] == "late_resume")
        self.assertFalse(late["ok"])
        self.assertTrue(late["latched"])
        Path(str(self.trace)+".cleanup-release").touch()
        self.child.wait(timeout=4)
        self.assertLess(time.monotonic()-started, 4)

    def test_blocked_amplifier_cleanup_does_not_hold_other_owners_or_process_exit(self):
        self.start_child(hold_cleanup=True)
        self.assert_bounded_exit()

    def test_real_http_stop_and_sigterm_bypass_a_saturated_request_pool(self):
        self.start_child(blocked_stats=True)
        def blocked_request():
            try:
                self.http.get("/sim/state")
            except httpx.TransportError:
                pass
        for _ in range(2):
            worker = threading.Thread(target=blocked_request, daemon=True)
            worker.start()
            self.addCleanup(worker.join, 1)
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if sum(item["name"] == "stats_entered" for item in self.receipts()) == 2:
                break
            time.sleep(.005)
        self.assertEqual(sum(item["name"] == "stats_entered" for item in self.receipts()), 2)
        for _ in range(12):
            self.assertEqual(self.http.get("/sim/clock").status_code, 503)
        started = time.monotonic()
        stopped = self.http.post(ACTION_PATHS[Action.STOP], json={})
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(stopped.json()["status"]["motion_state"]["stop_latched"])
        self.assertLess(time.monotonic()-started, .5)
        self.assertEqual(sum(item["name"] == "stats_entered" for item in self.receipts()), 2)
        self.assert_bounded_exit()

    def test_audio_cancellation_lane_cannot_prevent_a_motion_stop_latch(self):
        self.start_child(blocked_speaker_stop=True)
        self.fill_stop_lane(AUDIO_STOP_PATH)
        self.assertFalse(self.motion_latched())
        response = self.http.post(AUDIO_STOP_PATH, headers={AUDIO_SESSION_HEADER: "overflow", AUDIO_SEQUENCE_HEADER: "1"})
        self.assertEqual(response.status_code, 503)
        result = []
        request = threading.Thread(target=lambda: result.append(self.http.post(ACTION_PATHS[Action.STOP], json={})), daemon=True)
        request.start()
        self.addCleanup(request.join, 1)
        deadline = time.monotonic()+.5
        while time.monotonic() < deadline and not self.motion_latched():
            time.sleep(.005)
        self.assertTrue(self.motion_latched(), "audio cancellation capacity must not gate the motion latch")
        Path(str(self.trace)+".speaker-release").touch()
        request.join(1)
        self.assertEqual(result[0].status_code, 200)

    def test_http_motion_stop_latches_with_its_completion_lane_full_after_resume(self):
        self.start_child(blocked_speaker_stop=True)
        self.fill_stop_lane(ACTION_PATHS[Action.STOP])
        self.fresh_resume()
        response = self.http.post(ACTION_PATHS[Action.STOP], json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIsNone(response.json()["status"], "fallback must not claim status collection completed")
        self.assertTrue(self.motion_latched())

    def test_websocket_stop_and_disconnect_latch_with_completion_lane_full_after_resume(self):
        self.start_child(blocked_speaker_stop=True)
        self.fill_stop_lane(ACTION_PATHS[Action.STOP])
        self.fresh_resume()
        websocket = self.enterContext(connect(self.url.replace("http:", "ws:")+WS_PATH,
                                              proxy=None, close_timeout=.2, ping_interval=None))
        hello = json.loads(websocket.recv(timeout=1))
        websocket.send(json.dumps({"version": 1, "type": "command", "boot_id": hello["boot_id"],
            "session_id": hello["session_id"], "request_id": "saturated-stop", "action": "stop",
            "payload": {"command_id": "saturated-stop"}}))
        while True:
            result = json.loads(websocket.recv(timeout=1))
            if result.get("request_id") == "saturated-stop":
                break
        self.assertEqual(result["type"], "result", result)
        self.assertTrue(result["response"]["ok"])
        self.assertIsNone(result["response"]["status"])
        self.assertTrue(self.motion_latched())
        self.fresh_resume()
        websocket.close()
        deadline = time.monotonic()+.5
        while time.monotonic() < deadline and not self.motion_latched():
            time.sleep(.005)
        self.assertTrue(self.motion_latched(), "disconnect must latch before completion worker admission")

    def test_websocket_stop_and_disconnect_latch_while_a_prior_stop_completion_is_pending(self):
        self.start_child(blocked_speaker_stop=True)
        websocket = self.enterContext(connect(self.url.replace("http:", "ws:")+WS_PATH,
                                              proxy=None, close_timeout=.2, ping_interval=None))
        self.addCleanup(lambda: Path(str(self.trace)+".speaker-release").touch())
        hello = json.loads(websocket.recv(timeout=1))
        def send_stop(request_id):
            websocket.send(json.dumps({"version": 1, "type": "command", "boot_id": hello["boot_id"],
                "session_id": hello["session_id"], "request_id": request_id, "action": "stop",
                "payload": {"command_id": request_id}}))
        send_stop("first-stop")
        self.wait_receipts("speaker_stop")
        self.fresh_resume()
        send_stop("second-stop")
        while True:
            result = json.loads(websocket.recv(timeout=1))
            if result.get("request_id") == "second-stop":
                break
        self.assertEqual(result["type"], "result", result)
        self.assertTrue(result["response"]["ok"])
        self.assertIsNone(result["response"]["status"])
        self.assertTrue(self.motion_latched())
        self.fresh_resume()
        websocket.close()
        deadline = time.monotonic()+.5
        while time.monotonic() < deadline and not self.motion_latched():
            time.sleep(.005)
        self.assertTrue(self.motion_latched(), "disconnect cannot await an earlier optional STOP completion before latching")
        self.assertEqual(sum(item["name"] == "speaker_stop" for item in self.receipts()), 1,
                         "a pending optional cancellation must not be queued again")


if __name__ == "__main__":
    unittest.main()
