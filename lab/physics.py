"""Headless MuJoCo simulation with a stock-shaped policy observation boundary.

Only this module reads privileged dynamics. Controller observations have no
pose, joints, velocity or contacts. There are no hardware/network/capture paths.
Calling snapshot never samples sensors, advances time or fabricates movement.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math

import mujoco
import numpy as np

from .controller import MODES, ProceduralController
from .model import DT, JOINT_NAMES, LEG_NAMES, MODEL_VERSION, SCENARIOS, SPECS, build_mjcf

MAX_STEPS = 5000


class LabWorld:
    def __init__(self, seed: int = 1, scenario: str = "flat", mode: str = "stand"):
        self.reset(seed=seed, scenario=scenario, mode=mode)

    @staticmethod
    def _validate(seed, scenario, mode):
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2 ** 32 - 1:
            raise ValueError("seed must be an integer in 0..4294967295")
        if scenario not in SCENARIOS or mode not in MODES:
            raise ValueError("unsupported lab scenario or mode")

    def reset(self, seed: int = 1, scenario: str = "flat", mode: str = "stand") -> dict:
        self._validate(seed, scenario, mode)
        self.seed, self.scenario, self.mode = seed, scenario, mode
        self.rng = np.random.default_rng(seed)
        self.model = mujoco.MjModel.from_xml_string(build_mjcf(scenario))
        self.data = mujoco.MjData(self.model)
        self.controller = ProceduralController()
        self.controller.reset(seed)
        self.data.qpos[7:] = self.controller.nominal_targets
        self.data.ctrl[:] = self.controller.targets
        self._crab_id = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, "crab")
        self._foot_sites = [mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, f"{name}_foot_site") for name in LEG_NAMES]
        self._delay_steps = round(SPECS["estimated_actuator"]["command_delay_s"] / DT)
        self._commands = deque([self.controller.targets.copy() for _ in range(self._delay_steps)])
        self._applied = self.controller.targets.copy()
        self._sensor_pending = deque()
        self._sonar = {"distance_m": None, "valid": False, "no_echo": False, "age_s": 0., "capture_time_s": 0.}
        self._sequence = 0
        self._steps = 0
        self._actuator_saturations = 0
        self._fall_latched = False
        self._last_decision = None
        mujoco.mj_forward(self.model, self.data)
        self._initial_root = self.data.qpos[:3].copy()
        # Start with unavailable delayed evidence; no synthesized healthy default.
        self._sample_sonar()
        self._observation = self._make_observation()
        return self.snapshot()

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise ValueError("unsupported lab mode")
        self.mode = mode
        if mode == "stop":
            # Flush pending motion commands; stop holds the last issued target.
            self._commands = deque([self.controller.targets.copy() for _ in range(self._delay_steps)])
            self._applied = self.controller.targets.copy()
            self.data.ctrl[:] = self._applied
            self.controller.state = "stopped"
            self.controller.reason = "hold last commanded joint targets; physical momentum may persist"
        self._observation = self._make_observation()
        return self.snapshot()

    def _sample_sonar(self):
        rotation = self.data.xmat[self._crab_id].reshape(3, 3)
        origin = self.data.xpos[self._crab_id] + rotation @ np.array([.055, 0., .020])
        direction = rotation[:, 0].copy()
        geometry_id = np.array([-1], dtype=np.int32)
        distance = mujoco.mj_ray(self.model, self.data, origin, direction, np.array([1, 0, 0, 0, 0, 0], dtype=np.uint8), 1, -1, geometry_id)
        dropout = self.scenario == "sonar_dropout" and .5 <= self.data.time % 2.0 < 1.5
        valid = 0 < distance <= SPECS["estimated_sensor"]["sonar_max_range_m"] and not dropout
        noisy = float(np.clip(distance + self.rng.normal(0., SPECS["estimated_sensor"]["sonar_noise_std_m"]), .001, 2.)) if valid else None
        sample = {"distance_m": noisy, "valid": bool(valid), "no_echo": bool(not valid), "capture_time_s": float(self.data.time)}
        self._sensor_pending.append((self._steps + round(SPECS["estimated_sensor"]["sonar_delay_s"] / DT), sample))

    def _make_observation(self) -> dict:
        sonar = {key: self._sonar[key] for key in ("distance_m", "valid", "no_echo")}
        sonar["age_s"] = max(0., float(self.data.time - self._sonar["capture_time_s"]))
        return {
            "provenance": "simulated_stock_like", "sequence": self._sequence,
            "capture_time_s": self._sonar["capture_time_s"], "sonar": sonar,
            "phase": self.controller.phase,
            "goal": {"mode": self.mode, "forward_speed_m_s": SPECS["controller"]["stride_m"] / SPECS["controller"]["period_s"] if self.mode == "crawl" else 0.},
            "deadline_s": float(self.data.time + .1),
        }

    def step(self, steps: int = 1) -> dict:
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= MAX_STEPS:
            raise ValueError(f"steps must be an integer in 1..{MAX_STEPS}")
        for _ in range(steps):
            self._observation = self._make_observation()
            targets = self.controller.step(self._observation, self.mode, DT)
            self._last_decision = {
                "provenance": "procedural_controller", "sequence": self._steps + 1,
                "simulation_time_s": float(self.data.time), "mode": self.mode,
                "state": self.controller.state, "reason": self.controller.reason,
                "observation": deepcopy(self._observation),
                "requested_targets_rad": targets.tolist(),
            }
            self._commands.append(targets)
            delayed = self._commands.popleft()
            changed = np.abs(delayed - self._applied) >= SPECS["estimated_actuator"]["command_deadband_rad"]
            self._applied[changed] = delayed[changed]
            self.data.ctrl[:] = self._applied
            mujoco.mj_step(self.model, self.data)
            mujoco.mj_forward(self.model, self.data)
            self._steps += 1
            if self._steps % round(SPECS["estimated_sensor"]["sonar_period_s"] / DT) == 0:
                self._sample_sonar()
            while self._sensor_pending and self._sensor_pending[0][0] <= self._steps:
                _, self._sonar = self._sensor_pending.popleft()
                self._sequence += 1
            self._actuator_saturations += int(np.count_nonzero(np.abs(self.data.actuator_force) >= SPECS["estimated_actuator"]["torque_limit_nm"] * .99))
            self._fall_latched = self._fall_latched or self._is_fallen()
        self._observation = self._make_observation()
        return self.snapshot()

    def _is_fallen(self) -> bool:
        return bool(self.data.xpos[self._crab_id, 2] < .045 or self.data.xmat[self._crab_id, 8] < math.cos(.8))

    def snapshot(self) -> dict:
        contacts = []
        force = np.zeros(6)
        for i in range(self.data.ncon):
            contact = self.data.contact[i]
            mujoco.mj_contactForce(self.model, self.data, i, force)
            contacts.append({
                "geom1": mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, int(contact.geom1)),
                "geom2": mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, int(contact.geom2)),
                "position_m": contact.pos.tolist(), "normal_force_n": max(0., float(force[0])),
            })
        root = self.data.qpos[:3]
        displacement = root[:2] - self._initial_root[:2]
        tilt = math.acos(float(np.clip(self.data.xmat[self._crab_id, 8], -1., 1.)))
        limits = self.controller.joint_limits
        at_limits = (self.data.qpos[7:] <= limits[:, 0] + .015) | (self.data.qpos[7:] >= limits[:, 1] - .015)
        fallen = self._fall_latched or self._is_fallen()
        geoms = []
        types = {int(mujoco.mjtGeom.mjGEOM_BOX): "box", int(mujoco.mjtGeom.mjGEOM_CAPSULE): "capsule", int(mujoco.mjtGeom.mjGEOM_SPHERE): "sphere"}
        for i in range(self.model.ngeom):
            geoms.append({
                "name": mujoco.mj_id2name(self.model, mujoco.mjtObj.mjOBJ_GEOM, i),
                "type": types[int(self.model.geom_type[i])], "size": self.model.geom_size[i].tolist(),
                "position": self.data.geom_xpos[i].tolist(), "rotation": self.data.geom_xmat[i].tolist(),
                "rgba": self.model.geom_rgba[i].tolist(),
            })
        return {
            "schema_version": 1, "provenance": "mujoco_simulation", "model_version": MODEL_VERSION,
            "controller_version": SPECS["controller"]["version"], "engine_version": mujoco.__version__,
            "seed": self.seed, "scenario": self.scenario, "mode": self.mode,
            "sim_time_s": float(self.data.time), "dt_s": DT,
            "status": {"controller": "procedural_open_loop", "state": "fallen" if fallen else self.controller.state,
                       "reason": "simulator evaluator detected fall; no recovery controller" if fallen else self.controller.reason,
                       "model_status": "provisional_estimates", "hardware_verified": False},
            "targets": {"joint_names": list(JOINT_NAMES), "position_rad": self.controller.targets.tolist(),
                        "velocity_limit_rad_s": SPECS["estimated_actuator"]["target_slew_rad_s"],
                        "feet_body_m": self.controller.feet_body.tolist()},
            "metrics": {"displacement_m": float(np.linalg.norm(displacement)), "forward_displacement_m": float(displacement[0]),
                        "root_height_m": float(root[2]), "tilt_rad": tilt, "contacts": len(contacts), "fallen": bool(fallen),
                        "joint_limit_count": int(np.count_nonzero(at_limits)), "target_saturation_count": self.controller.saturation_count,
                        "actuator_saturation_count": self._actuator_saturations},
            "policy_observation": deepcopy(self._observation),
            "controller_decision": deepcopy(self._last_decision),
            "evaluation_truth": {"provenance": "privileged_simulator_truth", "joint_positions_rad": self.data.qpos[7:].tolist(),
                                 "joint_velocities_rad_s": self.data.qvel[6:].tolist(), "root_position_m": root.tolist(),
                                 "root_quaternion_wxyz": self.data.qpos[3:7].tolist(), "contacts": contacts,
                                 "feet_world_m": self.data.site_xpos[self._foot_sites].tolist()},
            "render_geoms": geoms,
        }
