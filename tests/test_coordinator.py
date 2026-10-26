"""Companion scheduling tests use fake I/O; no robot, audio, or model process."""
import threading
import time
import unittest
from types import SimpleNamespace

from brain.pet.coordinator import Coordinator
from brain.pet.mood import Mood
from shared.clock import CaptureClock


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Voice:
    busy = False
    enabled = False

    def __init__(self):
        self.calls = []

    def sound(self, name):
        self.calls.append(("sound", name))
        return True

    def say(self, text):
        self.calls.append(("say", text))
        return True

    def stop(self):
        self.calls.append(("stop", ""))

    def health(self):
        return {"provenance": "simulated", "available": False}


class Robot:
    def __init__(self):
        self.calls = []
        self.stand_started = threading.Event()
        self.stand_release = threading.Event()
        self.stand_release.set()
        self.resume_started = threading.Event()
        self.resume_release = threading.Event()
        self.resume_release.set()

    def stand(self):
        self.calls.append("stand")
        self.stand_started.set()
        self.stand_release.wait(1.0)
        return {"ok": True, "action": "stand", "status": {"pose": "standing"},
                "motion": {"completed_cycles": 1}}

    def sit(self):
        self.calls.append("sit")
        return {"ok": True, "action": "sit", "status": {"pose": "sitting"}}

    def stop(self):
        self.calls.append("stop")
        return {"ok": True, "action": "stop", "detail": "stopped at boundary"}

    def resume(self):
        self.calls.append("resume")
        self.resume_started.set()
        self.resume_release.wait(1.0)
        return {"ok": True, "action": "resume"}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.robot, self.voice = Clock(), Robot(), Voice()
        self.c = Coordinator(self.robot, self.voice, clock=self.clock, wall_clock=self.clock,
                             motion_enabled=True)
        self.observe()

    def tearDown(self):
        self.robot.stand_release.set()
        self.robot.resume_release.set()
        self.c.close()

    def observe(self, **overrides):
        health = {"available": True, "provenance": "simulated", "captured_at": self.clock(),
                  "max_age_s": 2.0}
        status = {"simulate": True, "boot_id": "one", "pose": "unknown", "battery_v": 7.6,
                  "distance_cm": 100.0, "inputs": {"battery": health, "ultrasonic": health}}
        status.update(overrides)
        self.c.observe(status)

    def test_bounded_pi_clock_keeps_original_captures_and_no_echo_unavailable(self):
        for offset in (.03, -.03):
            anchor = CaptureClock.measure(1000.006+offset, 1000., 1000., 1000.012, 1000.012)
            self.clock.now = 1000.012
            health = {"available": True, "provenance": "real", "captured_at": 1000+offset,
                      "clock": anchor.model_dump(), "max_age_s": 1.0}
            # A fresh time cannot turn a missing echo into a healthy distance.
            self.c._last_status_observed_at = None
            self.observe(observed_at=1000+offset, _clock=anchor.model_dump(),
                         inputs={"battery": health, "ultrasonic": {**health, "available": False,
                                 "failure_reason": "no echo"}}, distance_cm=None)
            state = self.c.snapshot()
            self.assertTrue(state["inputs"]["battery"]["fresh"])
            self.assertTrue(state["inputs"]["transport"]["fresh"])
            self.assertFalse(state["inputs"]["ultrasonic"]["available"])
            self.assertFalse(state["inputs"]["ultrasonic"]["used"])
            self.assertEqual(state["inputs"]["battery"]["captured_at"], 1000+offset)
            self.clock.now += 1.1
            self.assertFalse(self.c.snapshot()["inputs"]["battery"]["fresh"])

    def advance(self, seconds):
        self.clock.now += seconds
        self.observe()
        self.c.tick()

    def poll(self, predicate):
        for _ in range(100):
            self.c.tick()
            if predicate():
                return
            time.sleep(0.001)
        self.fail("worker did not complete")

    def test_stop_interrupts_motion_worker_and_accounts_completion(self):
        self.robot.stand_release.clear()
        movement = self.c.request("stand")
        self.c.tick()
        self.assertTrue(self.robot.stand_started.wait(0.1))
        self.c.hear("good boy, stop")
        self.assertTrue(self.c.stopped)
        self.poll(lambda: "stop" in self.robot.calls)
        self.assertFalse(self.robot.stand_release.is_set())
        self.robot.stand_release.set()
        self.poll(lambda: self.c._motion is None)
        self.assertEqual(movement.status.value, "cancelled")
        records = self.c.snapshot()["movements"]
        self.assertTrue(any(r.get("response", {}).get("action") == "stand" for r in records))
        self.assertTrue(any(r.get("response", {}).get("action") == "stop" for r in records))

    def test_stop_during_resume_cannot_reenable_social_behavior(self):
        self.c.hear("stop")
        self.poll(lambda: self.c._control is None)
        self.robot.resume_release.clear()
        resume = self.c.hear("resume")
        self.c.tick()
        self.assertTrue(self.robot.resume_started.wait(0.1))
        self.c.hear("stop")
        self.robot.resume_release.set()
        self.poll(lambda: self.c._control is None)
        self.assertTrue(self.c.stopped)
        self.assertEqual(resume.status.value, "cancelled")
        self.assertEqual(self.robot.calls[-1], "stop")

    def test_reconnect_and_restart_require_explicit_resume(self):
        self.c.connection_lost("link lost")
        self.observe()
        self.assertTrue(self.c.stopped)
        self.poll(lambda: self.c._control is None)
        self.c.hear("resume")
        self.poll(lambda: not self.c.stopped)
        self.observe(boot_id="two")
        self.assertTrue(self.c.stopped)

    def test_startup_pi_stop_latch_is_honored(self):
        self.observe(motion_state={"stop_latched": True})
        intent = self.c.request("stand")
        self.c.tick()
        self.assertEqual(intent.status.value, "blocked")
        self.assertNotIn("stand", self.robot.calls)

    def test_sensor_dropout_unknown_battery_and_close_obstacle_block_pose(self):
        for status, reason in [({"inputs": {}}, "battery"),
                               ({"battery_v": None}, "battery"),
                               ({"distance_cm": 5}, "obstacle")]:
            self.observe(**status)
            intent = self.c.request("stand")
            self.c.tick()
            self.assertEqual(intent.status.value, "blocked")
            self.assertIn(reason, intent.result["reason"])
        self.assertNotIn("stand", self.robot.calls)

    def test_stale_status_latches_stop_and_critical_battery_does_not_sit(self):
        self.clock.now += 3.0
        self.c.tick()
        self.assertTrue(self.c.stopped)
        self.assertNotIn("stand", self.robot.calls)
        self.observe(battery_v=6.0)
        self.assertNotIn("sit", self.robot.calls)

    def test_intention_expiry_does_not_execute(self):
        intent = self.c.request("stand", ttl_s=0.1)
        self.advance(0.2)
        self.assertEqual(intent.status.value, "expired")
        self.assertNotIn("stand", self.robot.calls)

    def test_exact_expiry_and_unbounded_model_ttl_are_rejected(self):
        intent = self.c.request("stand", ttl_s=1.0)
        self.advance(1.0)
        self.assertEqual(intent.status.value, "expired")
        for ttl, generated in [(1000.0, self.clock()), (8.0, self.clock() - 8.0),
                               (float("inf"), self.clock()), (8.0, self.clock() + 1.0)]:
            thought = SimpleNamespace(generated_at=generated, ttl_s=ttl)
            self.assertEqual(self.c.accept_thought(thought).status.value, "expired")

    def test_model_target_is_attention_only_then_expires(self):
        health = {"available": True, "provenance": "simulated", "captured_at": self.clock()}
        status = dict(self.c._status)
        self.c.observe(status, {"inputs": {"camera": health, "interpretation": health},
                                "detections": [{"label": "ball", "score": 0.9, "source": "simblob"}]})
        thought = SimpleNamespace(generated_at=self.clock(), ttl_s=2.0, intention="orient",
                                  say="", gesture="none", target="ball", mood_hint=None, observation="")
        self.c.accept_thought(thought)
        self.c.tick()
        self.assertEqual(self.c.target, "ball")
        self.assertEqual(self.robot.calls, [])
        self.advance(2.1)
        self.assertIsNone(self.c.target)
        expired = self.c.accept_thought(thought)
        self.assertEqual(expired.status.value, "expired")

    def test_dummy_and_malformed_perception_are_visible_but_not_engagement(self):
        health = {"available": True, "provenance": "simulated", "captured_at": self.clock()}
        self.c.observe(dict(self.c._status), {"inputs": {"camera": health, "interpretation": health},
            "detections": [{"source": "dummy", "label": "person", "score": 1.0}]})
        state = self.c.snapshot()
        self.assertEqual(state["observed_labels"], [])
        self.assertFalse(state["inputs"]["interpretation"]["used"])
        self.assertIn("dummy", state["ignored_detections"][0]["reason"])
        self.c.observe(dict(self.c._status), {"inputs": [], "detections": "bad"})
        self.assertIsNone(self.c.target)
        self.assertFalse(self.c.snapshot()["inputs"]["interpretation"]["available"])

    def test_same_visible_label_does_not_restart_episode(self):
        self.c.motion_enabled = False
        for _ in range(300):
            health = {"available": True, "provenance": "simulated", "captured_at": self.clock()}
            status = {**self.c._status, "inputs": {"battery": health, "ultrasonic": health}}
            self.c.observe(status, {"inputs": {"camera": health, "interpretation": health},
                "detections": [{"source": "simblob", "label": "person", "score": 0.9}]})
            self.c.tick()
            self.clock.now += 0.25
        sounds = [name for kind, name in self.voice.calls if kind == "sound"]
        self.assertEqual(sounds.count("acknowledge"), 1)
        self.assertEqual(self.c.phase, "settled")

    def test_new_user_engagement_cancels_queued_model_response(self):
        thought = SimpleNamespace(generated_at=self.clock(), ttl_s=8.0, intention="respond",
                                  say="An old answer", gesture="none", target=None,
                                  mood_hint=None, observation="")
        intent = self.c.accept_thought(thought)
        self.c.hear("hello Crab")
        self.c.tick()
        self.assertEqual(intent.status.value, "cancelled")
        self.assertNotIn(("say", "An old answer"), self.voice.calls)

    def test_new_greeting_cancels_queued_settle_transition(self):
        self.c.phase = "listen"
        self.c._phase_until = self.clock()
        self.c.tick()
        settle = next(i for i in self.c._pending if i.kind.value == "settle")
        self.c.hear("hello Crab")
        self.c.tick()
        self.assertEqual(settle.status.value, "cancelled")
        self.assertEqual(self.c.phase, "notice")

    def test_latest_posture_replaces_queued_posture(self):
        old = self.c.hear("sit")
        latest = self.c.hear("stand")
        self.c.tick()
        self.poll(lambda: self.c._motion is None)
        self.assertEqual(old.status.value, "cancelled")
        self.assertEqual(latest.status.value, "completed")
        self.assertNotIn("sit", self.robot.calls)

    def test_poll_older_than_resume_result_cannot_relatch(self):
        self.observe(observed_at=1000.0)
        self.c.hear("stop")
        self.poll(lambda: self.c._control is None)
        self.robot.resume = lambda: {"ok": True, "action": "resume", "status": {
            "boot_id": "one", "observed_at": 1001.0, "motion_state": {"stop_latched": False}}}
        self.c.hear("resume")
        self.poll(lambda: not self.c.stopped)
        self.observe(observed_at=1000.5, motion_state={"stop_latched": True})
        self.assertFalse(self.c.stopped)
        self.observe(observed_at=1002.0, motion_state={"stop_latched": True})
        self.assertTrue(self.c.stopped)

    def test_repeated_greeting_finishes_one_acknowledgement(self):
        self.c.motion_enabled = False
        for _ in range(5):
            self.c.hear("hello Crab")
            self.advance(0.1)
        for _ in range(30):
            self.advance(0.25)
        sounds = [name for kind, name in self.voice.calls if kind == "sound"]
        self.assertEqual(sounds.count("acknowledge"), 1)
        self.assertEqual(self.robot.calls, [])
        self.assertEqual(self.c.snapshot()["continuity"]["greetings"], 5)
        self.assertEqual(self.c.snapshot()["continuity"]["engagements"], 5)

    def test_quiet_mode_remains_responsive_without_audio(self):
        self.c.mode = "quiet"
        self.c.motion_enabled = False
        self.c.hear("hello Crab")
        for _ in range(30):
            self.advance(0.25)
        self.assertEqual(self.voice.calls, [])
        self.assertEqual(self.c.phase, "settled")

    def test_mood_is_elapsed_time_not_loop_frequency(self):
        a, b = Clock(), Clock()
        slow, fast = Mood(clock=a), Mood(clock=b)
        slow.event("engagement")
        fast.event("engagement")
        a.now += 20
        slow.advance()
        for _ in range(200):
            b.now += 0.1
            fast.advance()
        self.assertAlmostEqual(slow.engagement, fast.engagement, places=9)
        self.assertEqual(slow.current, fast.current)


if __name__ == "__main__":
    unittest.main()
