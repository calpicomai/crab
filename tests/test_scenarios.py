"""Behavior acceptance tests use the same reproducible demo traces."""
import unittest

from brain.pet.scenarios import SCENARIOS, run_scenario


class CompanionScenariosTest(unittest.TestCase):
    def test_greeting_has_coherent_episode_and_returns_to_stillness(self):
        scenario = run_scenario("greeting")
        phases = [frame["companion"]["episode"]["phase"] for frame in scenario["trace"]]
        for phase in ("notice", "orient", "acknowledge", "listen", "settle", "settled"):
            self.assertIn(phase, phases)
        self.assertEqual(phases[-1], "settled")
        self.assertEqual(scenario["robot_commands"], [])
        self.assertEqual([event["name"] for event in scenario["audio_intentions"]],
                         ["acknowledge", "settle"])
        self.assertTrue(all(event["played"] is False for event in scenario["audio_intentions"]))

    def test_stop_precedes_praise_and_remains_latched(self):
        scenario = run_scenario("interruption")
        stop_index = next(i for i, frame in enumerate(scenario["trace"])
                          if frame["event"].startswith("stop takes"))
        self.assertTrue(all(frame["companion"]["stopped"]
                            for frame in scenario["trace"][stop_index:]))
        self.assertEqual(scenario["audio_intentions"], [])
        self.assertEqual(scenario["robot_commands"], ["stop"])

    def test_unknown_movement_is_explained_and_does_not_execute(self):
        scenario = run_scenario("ignored-command")
        final = scenario["trace"][-1]["companion"]
        self.assertEqual(scenario["robot_commands"], [])
        self.assertTrue(any(intent["result"].get("unsupported_request") == "unsupported"
                            for intent in final["intentions"]))
        self.assertIn("nearby", final["last_say"])

    def test_sensor_dropout_is_visible_and_blocks_posture(self):
        scenario = run_scenario("sensor-dropout")
        final = scenario["trace"][-1]["companion"]
        sonar = final["inputs"]["ultrasonic"]
        self.assertFalse(sonar["available"])
        self.assertFalse(sonar["used"])
        self.assertIn("dropout", sonar["ignored_reason"])
        self.assertTrue(any(i["status"] == "blocked" and "ultrasonic" in i["result"]["reason"]
                            for i in final["intentions"]))
        self.assertEqual(scenario["robot_commands"], [])

    def test_low_battery_stops_without_additional_sit(self):
        scenario = run_scenario("low-battery")
        self.assertTrue(scenario["trace"][-1]["companion"]["stopped"])
        self.assertEqual(scenario["robot_commands"], ["stop"])

    def test_blocked_route_never_invokes_pursuit(self):
        scenario = run_scenario("blocked-route")
        self.assertEqual(scenario["trace"][-1]["sensed"]["distance_cm"], 8.0)
        self.assertEqual(scenario["robot_commands"], [])
        self.assertIsNone(scenario["trace"][-1]["companion"]["target"])

    def test_model_timeout_discards_late_dialogue(self):
        scenario = run_scenario("model-timeout")
        final = scenario["trace"][-1]["companion"]
        self.assertTrue(any(i["status"] == "expired" for i in final["intentions"]))
        self.assertNotIn("late response", final["last_say"])
        self.assertEqual(scenario["robot_commands"], [])

    def test_repetition_extends_listening_without_spam(self):
        scenario = run_scenario("repeated-interaction")
        self.assertLessEqual(len(scenario["audio_intentions"]), 2)
        self.assertEqual(scenario["robot_commands"], [])
        self.assertEqual(scenario["trace"][-1]["companion"]["episode"]["phase"], "settled")

    def test_reconnect_does_not_replay_interrupted_intent(self):
        scenario = run_scenario("reconnect")
        self.assertTrue(scenario["trace"][-1]["companion"]["stopped"])
        self.assertEqual(scenario["robot_commands"], ["stop"])
        self.assertEqual(scenario["audio_intentions"], [])

    def test_greeting_is_reproducible(self):
        self.assertEqual(run_scenario("greeting"), run_scenario("greeting"))

    def test_catalog_contains_all_requested_cases(self):
        self.assertEqual(set(SCENARIOS), {"greeting", "interruption", "ignored-command",
            "sensor-dropout", "low-battery", "blocked-route", "model-timeout",
            "repeated-interaction", "reconnect"})


if __name__ == "__main__":
    unittest.main()
