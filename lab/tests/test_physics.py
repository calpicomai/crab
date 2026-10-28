"""Real MuJoCo regression evidence for the provisional Crab model.

Each test catches a physical or boundary failure: absent gravity/contact,
unbounded controls, a fictitious render transform, privileged-input leakage,
nonrepeatable reset, stale evidence, or an unreported fall.
"""
import importlib
import json
import math
import unittest
from copy import deepcopy
from unittest.mock import patch

import mujoco
import numpy as np


class PhysicsTests(unittest.TestCase):
    def test_decision_evidence_is_the_exact_observation_used(self):
        world=self.world(mode='crawl')
        seen=[]
        original=world.controller.step
        def capture(observation,mode,dt):
            seen.append(deepcopy(observation))
            return original(observation,mode,dt)
        with patch.object(world.controller,'step',side_effect=capture):
            value=world.step(40)
        decision=value['controller_decision']
        self.assertEqual(decision['observation'],seen[-1])
        self.assertEqual(decision['provenance'],'procedural_controller')
        self.assertEqual(decision['requested_targets_rad'],value['targets']['position_rad'])
        self.assertAlmostEqual(decision['simulation_time_s']+value['dt_s'],value['sim_time_s'])
        self.assertNotIn('root_position_m',decision['observation'])
        self.assertNotIn('joint_positions_rad',decision['observation'])

    def setUp(self):
        # A missing implementation is a deliberate assertion failure in RED.
        try:
            self.physics = importlib.import_module("lab.physics")
            self.controller = importlib.import_module("lab.controller")
            self.model_module = importlib.import_module("lab.model")
        except ModuleNotFoundError:
            self.fail("Crab Lab CPU physics/controller/model implementation is absent")

    def world(self, **kwargs):
        return self.physics.LabWorld(**kwargs)

    def test_floating_body_has_twelve_dynamical_joints_and_engine_transforms(self):
        world = self.world()
        self.assertEqual(world.model.nq, 19)
        self.assertEqual(world.model.nu, 12)
        self.assertEqual(world.model.nv, 18)
        state = world.snapshot()
        for geom in state["render_geoms"]:
            index = mujoco.mj_name2id(world.model, mujoco.mjtObj.mjOBJ_GEOM, geom["name"])
            np.testing.assert_allclose(geom["position"], world.data.geom_xpos[index], atol=0)
            np.testing.assert_allclose(geom["rotation"], world.data.geom_xmat[index], atol=0)
        self.assertEqual(state["provenance"], "mujoco_simulation")
        json.dumps(state, allow_nan=False)

    def test_gravity_drop_and_real_contact_prevent_passing_through_floor(self):
        world = self.world(mode="stop")
        world.data.qpos[2] += .2
        world.data.qvel[:] = 0
        mujoco.mj_forward(world.model, world.data)
        height = world.data.qpos[2]
        world.step(50)
        self.assertLess(world.data.qpos[2], height - .025)
        world.step(950)
        truth = world.snapshot()["evaluation_truth"]
        self.assertGreater(world.data.qpos[2], .015)
        self.assertTrue(any("floor" in (c["geom1"], c["geom2"]) for c in truth["contacts"]))
        self.assertGreater(sum(c["normal_force_n"] for c in truth["contacts"]), .1)

    def test_stand_settles_on_feet_without_kinematic_root_motion(self):
        world = self.world()
        state = world.step(2000)
        self.assertFalse(state["metrics"]["fallen"])
        self.assertGreater(state["metrics"]["root_height_m"], .055)
        self.assertLess(state["metrics"]["displacement_m"], .025)
        self.assertGreaterEqual(state["metrics"]["contacts"], 3)
        feet = state["evaluation_truth"]["feet_world_m"]
        self.assertTrue(all(.003 < point[2] < .014 for point in feet))

    def test_crawl_changes_joint_targets_and_moves_under_real_dynamics(self):
        stand = self.world(seed=4, mode="stand")
        crawl = self.world(seed=4, mode="crawl")
        stand.step(4000)
        state = crawl.step(4000)
        self.assertGreater(state["metrics"]["forward_displacement_m"], .001)
        self.assertLess(state["metrics"]["displacement_m"], .20)
        self.assertFalse(state["metrics"]["fallen"])
        self.assertGreater(np.linalg.norm(crawl.data.qpos - stand.data.qpos), .01)
        self.assertEqual(state["status"]["controller"], "procedural_open_loop")

    def test_seed_reset_and_chunking_reproduce_actual_state(self):
        first = self.world(seed=12, mode="crawl")
        second = self.world(seed=12, mode="crawl")
        first.step(1200)
        for _ in range(12):
            second.step(100)
        np.testing.assert_array_equal(first.data.qpos, second.data.qpos)
        self.assertEqual(first.snapshot()["policy_observation"], second.snapshot()["policy_observation"])
        before = first.snapshot()
        first.reset(seed=12, mode="crawl")
        self.assertEqual(first.snapshot()["sim_time_s"], 0.)
        self.assertEqual(first.step(1200), before)

    def test_policy_observation_excludes_privileged_measurements(self):
        state = self.world().step(20)
        observation = state["policy_observation"]
        self.assertEqual(set(observation), {"provenance", "sequence", "capture_time_s", "sonar", "phase", "goal", "deadline_s"})
        self.assertIn("root_position_m", state["evaluation_truth"])
        self.assertIn("joint_positions_rad", state["evaluation_truth"])
        first = self.controller.ProceduralController()
        second = self.controller.ProceduralController()
        altered = dict(observation, evaluation_truth={"root_position_m": [900, 900, 900]}, joint_positions_rad=[999] * 12)
        np.testing.assert_array_equal(first.step(observation, "crawl", .002), second.step(altered, "crawl", .002))
        self.assertEqual(first.state, "crawling")

    def test_missing_stale_blocked_and_expired_observations_inhibit_crawl(self):
        good = self.world().step(20)["policy_observation"]
        baseline = self.controller.ProceduralController()
        baseline.step(good, "crawl", .002)
        self.assertEqual(baseline.state, "crawling")
        for changes in (
            {"sonar": {"distance_m": None, "valid": False, "no_echo": True, "age_s": 0}},
            {"sonar": {"distance_m": .5, "valid": True, "no_echo": False, "age_s": 1}},
            {"sonar": {"distance_m": .05, "valid": True, "no_echo": False, "age_s": 0}},
            {"deadline_s": -.01},
        ):
            controller = self.controller.ProceduralController()
            observation = dict(good, **changes)
            targets = controller.step(observation, "crawl", .002)
            self.assertEqual(controller.state, "inhibited")
            self.assertTrue(controller.reason)
            np.testing.assert_allclose(targets, controller.nominal_targets, atol=0)

    def test_sonar_dropout_is_unknown_and_inhibits_without_fabricating_clearance(self):
        world = self.world(scenario="sonar_dropout", mode="crawl")
        state = world.step(300)
        sonar = state["policy_observation"]["sonar"]
        self.assertFalse(sonar["valid"])
        self.assertIsNone(sonar["distance_m"])
        self.assertEqual(state["status"]["state"], "inhibited")

    def test_future_and_negative_simulation_capture_cannot_authorize_crawl(self):
        good=self.world().step(20)['policy_observation']
        for capture in (1000.,-.01):
            controller=self.controller.ProceduralController()
            observation={**good,'capture_time_s':capture}
            targets=controller.step(observation,'crawl',.002)
            self.assertEqual(controller.state,'inhibited')
            np.testing.assert_array_equal(targets,controller.nominal_targets)

    def test_stop_holds_last_command_instead_of_restarting_gait(self):
        world = self.world(mode="crawl")
        world.step(700)
        world.set_mode("stop")
        target = world.snapshot()["targets"]["position_rad"]
        state = world.step(800)
        np.testing.assert_array_equal(state["targets"]["position_rad"], target)
        self.assertEqual(state["status"]["state"], "stopped")
        self.assertEqual(state["mode"], "stop")

    def test_independent_leg_kinematics_reject_unreachable_and_singular_targets(self):
        leg = self.controller.LegKinematics()
        np.testing.assert_allclose(leg.forward([0, 0, 0]), [.159, 0, 0], atol=1e-12)
        point = [.095, 0, -.085]
        angles = leg.inverse(point)
        np.testing.assert_allclose(leg.forward(angles), point, atol=1e-10)
        for bad in ([.4, 0, -.08], [.159, 0, 0], [math.nan, 0, -.08], [0, 0, 0]):
            with self.subTest(point=bad), self.assertRaises(ValueError):
                leg.inverse(bad)

    def test_analytic_kinematics_agree_with_actual_engine_joint_axes(self):
        world=self.world()
        rng=np.random.default_rng(73)
        limits=world.controller.joint_limits
        for _ in range(40):
            world.data.qpos[7:]=rng.uniform(limits[:,0],limits[:,1])
            mujoco.mj_forward(world.model,world.data)
            for index,site in enumerate(world._foot_sites):
                local=world.controller.leg.forward(world.data.qpos[7+3*index:10+3*index])
                body=world.controller.anchors[index]+world.controller.rotations[index]@local
                expected=world.data.xpos[world._crab_id]+world.data.xmat[world._crab_id].reshape(3,3)@body
                np.testing.assert_allclose(expected,world.data.site_xpos[site],atol=1e-12)

    def test_generated_foot_clearance_joint_limits_and_slew_are_bounded(self):
        controller = self.controller.ProceduralController()
        observation = self.world().step(20)["policy_observation"]
        previous = controller.nominal_targets.copy()
        min_height, max_height = 1., -1.
        for step in range(2400):
            observation = dict(observation, capture_time_s=step * .002, deadline_s=step * .002 + .1)
            target = controller.step(observation, "crawl", .002)
            self.assertTrue(np.all(np.isfinite(target)))
            self.assertTrue(np.all(target >= controller.joint_limits[:, 0]))
            self.assertTrue(np.all(target <= controller.joint_limits[:, 1]))
            self.assertLessEqual(float(np.max(np.abs(target - previous))), .002 * 1.2 + 1e-12)
            min_height = min(min_height, float(np.min(controller.feet_body[:, 2])))
            max_height = max(max_height, float(np.max(controller.feet_body[:, 2])))
            previous = target
        self.assertGreaterEqual(min_height, -.085001)
        self.assertLessEqual(max_height, -.073)
        self.assertGreater(max_height, -.081)

    def test_actuator_torque_is_finite_saturated_and_joint_stops_are_physical(self):
        model = mujoco.MjModel.from_xml_string(self.model_module.build_mjcf(fixed_base=True))
        data = mujoco.MjData(model)
        # Deliberately bypass command-space clamps to load physical joint stops.
        # Force saturation and joint constraints must still bound the dynamics.
        model.actuator_ctrllimited[:] = 0
        data.ctrl[:] = 100
        for _ in range(1500):
            mujoco.mj_step(model, data)
        self.assertTrue(np.all(np.isfinite(data.qpos)))
        self.assertLessEqual(float(np.max(np.abs(data.actuator_force))), .45 + 1e-9)
        self.assertGreater(float(np.max(np.abs(data.actuator_force))), .44)
        self.assertTrue(np.all(data.qpos <= model.jnt_range[:, 1] + .035))
        self.assertTrue(np.all(data.qpos >= model.jnt_range[:, 0] - .035))

    def test_low_friction_and_fall_are_reported_from_truth(self):
        world = self.world(scenario="low_friction", mode="crawl")
        self.assertLess(world.model.geom_friction[0, 0], .1)
        world.data.qpos[3:7] = [0., 1., 0., 0.]
        mujoco.mj_forward(world.model, world.data)
        state = world.step(400)
        self.assertTrue(state["metrics"]["fallen"])
        self.assertEqual(state["status"]["state"], "fallen")
        json.dumps(state, allow_nan=False)

    def test_invalid_controls_rejected_without_mutating_world(self):
        world = self.world()
        initial = world.snapshot()
        for value in (0, -1, 5001, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                world.step(value)
        with self.assertRaises(ValueError):
            world.set_mode("hardware")
        for kwargs in ({"scenario": "../robot"}, {"seed": -1}, {"seed": True}, {"mode": "run"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                world.reset(**kwargs)
        self.assertEqual(world.snapshot(), initial)


if __name__ == "__main__":
    unittest.main()
