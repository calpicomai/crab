"""HTTP and companion-runtime contracts, without physical devices or services."""
from __future__ import annotations

import asyncio
import json
import queue
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from robot import config
from robot.control import CommandLeases
from robot.gait import GaitEngine
from shared import (ACTION_PATHS, AUDIO_HEALTH_PATH, AUDIO_PLAY_PATH,
                    AUDIO_SELF_TEST_PATH, CONTROL_LEASE_PATH, Action)
from shared.health import InputHealth


class Sample:
    def __init__(self, value):
        self.value = value

    def read_v(self):
        return self.value

    read_cm = read_v

    def health(self):
        return InputHealth(available=self.value is not None, provenance="simulated",
                           captured_at=time.time() if self.value is not None else None,
                           source="explicit test fixture",
                           failure_reason=None if self.value is not None else "sensor disconnected")


class ASGIClient:
    """Existing httpx transport: installed Starlette TestClient needs httpx2."""
    def __init__(self, app):
        self.app = app

    def request(self, method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                         base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)


class RuntimeHTTPTests(unittest.TestCase):
    def setUp(self):
        # Even on an equipped developer machine importing the test target cannot
        # arm or instantiate the actual crawler.
        with patch.object(config, "SIMULATE", True), patch.object(config, "ACTUATORS_ENABLED", False):
            from robot import server
        self.server = server
        self.engine = GaitEngine(simulate=True)
        self.battery, self.sonar = Sample(7.4), Sample(100.0)
        self.engine.battery_fn = self.battery.read_v
        self.engine.clearance_fn = self.sonar.read_cm
        self.now = 100.0
        self.leases = CommandLeases(clock=lambda: self.now, ttl_s=2.0)
        self.patches = [patch.object(server, "engine", self.engine),
                        patch.object(server, "leases", self.leases),
                        patch.object(server, "battery_sensor", self.battery),
                        patch.object(server, "distance_sensor", self.sonar),
                        patch.object(server, "camera", None), patch.object(server, "speaker", None),
                        patch.object(server, "mic", None),
                        patch.object(server, "command_lock", threading.Lock()),
                        patch.object(server, "control_transition_lock", threading.Lock()),
                        patch.object(config, "HOME_ON_START", "none")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = ASGIClient(server.app)
        lifespan = server.lifespan(server.app)
        asyncio.run(lifespan.__aenter__())
        self.addCleanup(lambda: asyncio.run(lifespan.__aexit__(None, None, None)))

    def command(self, **kwargs):
        lease = self.client.post(CONTROL_LEASE_PATH).json()
        self.assertTrue(lease["ok"])
        return {"lease_id": lease["lease_id"], "command_id": uuid.uuid4().hex, **kwargs}

    def test_startup_is_stationary_and_status_reports_provenance(self):
        data = self.client.post(ACTION_PATHS[Action.GET_STATUS], json={}).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"]["motion_state"]["sequence"], 0)
        self.assertEqual(data["status"]["pose"], "unknown")
        self.assertEqual(data["status"]["boot_id"], self.leases.boot_id)
        self.assertEqual(data["status"]["inputs"]["battery"]["provenance"], "simulated")

    def test_missing_replayed_expired_and_previous_boot_leases_never_move(self):
        path = ACTION_PATHS[Action.WALK]
        self.assertFalse(self.client.post(path, json={"steps": 1}).json()["ok"])
        payload = self.command(steps=1)
        first = self.client.post(path, json=payload).json()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["motion"]["completed_cycles"], 1)
        self.assertFalse(self.client.post(path, json=payload).json()["ok"])
        expired = self.command(steps=1)
        self.now += 3
        self.assertFalse(self.client.post(path, json=expired).json()["ok"])
        previous_boot = self.command(steps=1)
        with patch.object(self.server, "leases", CommandLeases(clock=lambda: self.now)):
            self.assertFalse(self.client.post(path, json=previous_boot).json()["ok"])
        self.assertEqual(self.engine.motion_state()["sequence"], 1)

    def test_duplicate_command_id_with_fresh_lease_is_rejected(self):
        payload = self.command(direction="left")
        first = self.client.post(ACTION_PATHS[Action.TURN_STEPS], json=payload).json()
        self.assertTrue(first["ok"], first)
        replay = self.command(direction="left")
        replay["command_id"] = payload["command_id"]
        result = self.client.post(ACTION_PATHS[Action.TURN_STEPS], json=replay).json()
        self.assertFalse(result["ok"])
        self.assertIn("already executed", result["detail"])
        self.assertEqual(self.engine.motion_state()["sequence"], 1)

    def test_extra_fields_invalid_bounds_and_nonfinite_values_are_client_errors(self):
        for payload in ({"steps": 1, "servo_angles": [0] * 12}, {"steps": 100},
                        {"steps": -1}, {"speed": 101}, {"mode": "custom"}):
            response = self.client.post(ACTION_PATHS[Action.WALK], json=payload)
            self.assertEqual(response.status_code, 422, response.text)
        for raw in ('{"degrees":NaN}', '{"degrees":Infinity}', '{"degrees":-Infinity}'):
            response = self.client.post(ACTION_PATHS[Action.TURN], content=raw,
                                        headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.engine.motion_state()["sequence"], 0)

    def test_unavailable_inputs_are_visible_and_inhibit_execution(self):
        self.battery.value = None
        self.sonar.value = None
        status = self.client.post(ACTION_PATHS[Action.GET_STATUS], json={}).json()["status"]
        self.assertIsNone(status["battery_v"])
        self.assertIsNone(status["distance_cm"])
        for name in ("battery", "ultrasonic"):
            self.assertFalse(status["inputs"][name]["available"])
            self.assertIn("disconnected", status["inputs"][name]["failure_reason"])
        result = self.client.post(ACTION_PATHS[Action.WALK], json=self.command(steps=1)).json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["motion"]["completed_cycles"], 0)
        self.assertEqual(self.engine.motion_state()["sequence"], 0)

    def test_disabled_audio_never_reports_playback_success(self):
        health = self.client.get(AUDIO_HEALTH_PATH).json()
        self.assertFalse(health["speaker"]["available"])
        play = self.client.post(AUDIO_PLAY_PATH, content=b"not a wav")
        self.assertEqual(play.status_code, 503)
        self.assertFalse(play.json()["played"])
        test = self.client.post(AUDIO_SELF_TEST_PATH, json={}).json()
        self.assertFalse(test["ok"])
        self.assertFalse(test["played"])

    def test_audio_http_requires_current_lease_and_never_claims_simulated_sound(self):
        from robot.audio import PiSpeaker
        from shared.audio import sound_wav
        from shared import AUDIO_LEASE_HEADER, AUDIO_SESSION_HEADER, AUDIO_SEQUENCE_HEADER, AUDIO_STOP_PATH
        with patch.object(self.server, "speaker", PiSpeaker(simulate=True)):
            self.assertEqual(self.client.post(AUDIO_PLAY_PATH, content=sound_wav("acknowledge")).status_code, 422)
            lease = self.client.post(CONTROL_LEASE_PATH).json()["lease_id"]
            headers = {AUDIO_LEASE_HEADER: lease, AUDIO_SESSION_HEADER: "runtime-test", AUDIO_SEQUENCE_HEADER: "1"}
            result = self.client.post(AUDIO_PLAY_PATH, content=sound_wav("acknowledge"), headers=headers).json()
            self.assertFalse(result["played"])
            self.assertFalse(result["ok"])
            self.assertTrue(result["simulate"])
            late = self.client.post(CONTROL_LEASE_PATH).json()["lease_id"]
            self.client.post(AUDIO_STOP_PATH, headers={AUDIO_SESSION_HEADER: "runtime-test", AUDIO_SEQUENCE_HEADER: "3"})
            result = self.client.post(AUDIO_PLAY_PATH, content=sound_wav("acknowledge"), headers={
                **headers, AUDIO_LEASE_HEADER: late, AUDIO_SEQUENCE_HEADER: "2"}).json()
            self.assertFalse(result["played"])
            self.assertFalse(result["ok"])
            previous_boot = self.client.post(CONTROL_LEASE_PATH).json()["lease_id"]
            with patch.object(self.server, "leases", CommandLeases(clock=lambda: self.now)), \
                 patch.object(self.server, "speaker", PiSpeaker(simulate=True)):
                result = self.client.post(AUDIO_PLAY_PATH, content=sound_wav("acknowledge"), headers={
                    **headers, AUDIO_LEASE_HEADER: previous_boot, AUDIO_SEQUENCE_HEADER: "4"}).json()
            self.assertFalse(result["played"])
            self.assertIn("lease", result["reason"])

    def test_dashboard_stop_preempts_full_queue_and_discards_old_resume(self):
        from shared import COMPANION_EVENTS_PATH
        with patch.object(self.server, "companion_events", queue.Queue(maxsize=2)), \
             patch.object(self.server, "companion_urgent", None):
            self.client.post(COMPANION_EVENTS_PATH, json={"text": "resume"})
            self.client.post(COMPANION_EVENTS_PATH, json={"text": "hello"})
            self.assertFalse(self.client.post(COMPANION_EVENTS_PATH, json={"text": "hello"}).json()["ok"])
            self.assertTrue(self.client.post(COMPANION_EVENTS_PATH, json={"text": "good crab, stop"}).json()["ok"])
            self.assertTrue(self.engine.motion_state()["stop_latched"])
            events = self.client.get(COMPANION_EVENTS_PATH).json()["events"]
            self.assertEqual([e["text"] for e in events], ["good crab, stop"])

    def test_http_stop_reaches_engine_while_vendor_request_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        original = self.engine._do_action
        def blocking_vendor(action, speed):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test did not release fake vendor")
            original(action, speed)
        self.engine._do_action = blocking_vendor
        payload = self.command(steps=1)
        responses = []
        worker = threading.Thread(target=lambda: responses.append(
            self.client.post(ACTION_PATHS[Action.WALK], json=payload)))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            stop = self.client.post(ACTION_PATHS[Action.STOP], json={})
            self.assertEqual(stop.status_code, 200)
            self.assertTrue(stop.json()["ok"])
            self.assertTrue(worker.is_alive())  # fake vendor still executing
            self.assertTrue(self.engine.motion_state()["stop_latched"])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        result = responses[0].json()
        self.assertFalse(result["ok"])
        self.assertTrue(result["motion"]["cancelled"])
        self.assertEqual(result["motion"]["completed_cycles"], 1)
        self.assertEqual(result["motion"]["sequence"], 1)
        self.assertFalse(self.client.post(ACTION_PATHS[Action.WALK], json=self.command(steps=1)).json()["ok"])


class RuntimeMindTests(unittest.TestCase):
    def run_with_thought(self, *, request_started, generated_at):
        from brain.pet import loop
        from brain.pet.brain import PetThought
        from shared import CommandResponse

        published = []
        class FakeClient:
            base_url = "http://localhost:8000"
            def __init__(self, *args, **kwargs):
                pass
            def stop(self):
                return CommandResponse(ok=True, action=Action.STOP)
            def close(self):
                pass
        class FakeInputs:
            def __init__(self, *args, **kwargs):
                self.samples = queue.Queue()
                self.samples.put(({"boot_id": "first-boot", "simulate": True, "inputs": {}}, None, None))
            def start(self):
                pass
            def close(self):
                pass
        class FakeMind:
            def __init__(self, *args):
                self.results = queue.Queue()
                self.results.put((0, request_started, PetThought("stale model dialogue", generated_at=generated_at), None, 9000))
                self.busy = True
            def close(self):
                pass
        class FakeVoice:
            enabled = False
            busy = False
            def __init__(self, **kwargs):
                pass
            def stop(self):
                pass
            def close(self):
                pass
            def health(self):
                return {"available": False, "played": False}
            def listening_suppressed(self):
                return False
        dashboard = Mock()
        dashboard.push.side_effect = published.append
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(loop, "RobotClient", FakeClient), \
             patch.object(loop, "InputWorker", FakeInputs), \
             patch.object(loop, "MindWorker", FakeMind), \
             patch.object(loop, "Voice", FakeVoice), \
             patch.object(loop, "Dashboard", return_value=dashboard), \
             patch.object(loop.time, "monotonic", return_value=10.0), \
             patch.object(loop.time, "sleep"):
            rc = loop.main(["--sim", "--no-stt", "--no-camera", "--no-voice", "--max-ticks", "1",
                            "--memory-db", str(Path(temp) / "memory.db"),
                            "--identity-file", str(Path(temp) / "identity.json")])
        self.assertEqual(rc, 0)
        self.assertTrue(published)
        self.assertNotEqual(published[-1]["last_say"], "stale model dialogue")
        return published[-1]

    def test_runtime_expires_slow_inference_even_if_thought_is_fresh(self):
        state = self.run_with_thought(request_started=0.0, generated_at=time.time())
        self.assertIn("expired", state["mind"]["failure_reason"])

    def test_runtime_explains_expired_thought_even_if_worker_was_fast(self):
        state = self.run_with_thought(request_started=9.0, generated_at=time.time() - 20)
        self.assertIn("expired", state["mind"]["failure_reason"])

    def test_input_burst_cannot_evict_stop(self):
        from brain.pet.loop import EventInbox, _latest
        inbox = EventInbox()
        _latest(inbox, {"text": "stop", "timestamp": time.time(), "source": "microphone"})
        for index in range(100):
            _latest(inbox, {"text": f"hello {index}", "timestamp": time.time(), "source": "dashboard"})
        self.assertEqual(inbox.get_nowait()["text"], "stop")
        ordinary = []
        while True:
            try:
                ordinary.append(inbox.get_nowait())
            except queue.Empty:
                break
        self.assertLessEqual(len(ordinary), 32)


class RuntimeClientTests(unittest.TestCase):
    def client(self, handler):
        from brain.client import RobotClient
        client = RobotClient("http://127.0.0.1:8000")
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=client.base_url, trust_env=False)
        self.addCleanup(client.close)
        return client

    def test_lost_motion_response_is_not_retried(self):
        requests = []
        def handler(request):
            requests.append(request.url.path)
            if request.url.path == CONTROL_LEASE_PATH:
                return httpx.Response(200, json={"ok": True, "lease_id": "one-use", "boot_id": "boot"})
            raise httpx.ReadTimeout("response lost after simulated execution", request=request)
        client = self.client(handler)
        with self.assertRaises(httpx.ReadTimeout):
            client.walk(1)
        self.assertEqual(requests, [CONTROL_LEASE_PATH, ACTION_PATHS[Action.WALK]])

    def test_response_for_another_command_is_rejected(self):
        def handler(request):
            if request.url.path == CONTROL_LEASE_PATH:
                return httpx.Response(200, json={"ok": True, "lease_id": "one-use", "boot_id": "boot"})
            body = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "action": "sit",
                                            "command_id": body["command_id"] + "wrong"})
        client = self.client(handler)
        with self.assertRaises((ValueError, RuntimeError)):
            client.walk(1)


if __name__ == "__main__":
    unittest.main()
