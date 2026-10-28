"""Crab-owned open-loop procedural targets, without simulator state inputs.

The independent analytic IK is a conventional yaw plus planar two-link chain;
it is not copied from the vendor kinematics or motion frame implementation.
There is no balance controller or claim of hardware deployability.
"""
from __future__ import annotations

import math
import numpy as np

from .model import LEG_ANGLES, SPECS

MODES = ("stand", "crawl", "stop")


class LegKinematics:
    def __init__(self):
        geometry = SPECS["geometry_m"]
        self.a, self.b, self.c = (geometry[key] for key in ("coxa", "femur", "tibia"))
        self.limits = np.array([SPECS["estimated_joint_limits_rad"][key] for key in ("yaw", "hip", "knee")])

    def forward(self, angles) -> np.ndarray:
        q = np.asarray(angles, dtype=float)
        if q.shape != (3,) or not np.all(np.isfinite(q)):
            raise ValueError("three finite joint angles required")
        yaw, hip, knee = q
        radius = self.a + self.b * math.cos(hip) + self.c * math.cos(hip + knee)
        return np.array([radius * math.cos(yaw), radius * math.sin(yaw), -self.b * math.sin(hip) - self.c * math.sin(hip + knee)])

    def inverse(self, foot) -> np.ndarray:
        p = np.asarray(foot, dtype=float)
        if p.shape != (3,) or not np.all(np.isfinite(p)):
            raise ValueError("three finite foot coordinates required")
        x, y, z = p
        radius = math.hypot(x, y)
        horizontal = radius - self.a
        if radius < 1e-8 or horizontal <= 0:
            raise ValueError("foot crosses coxa singularity")
        cosine = (horizontal * horizontal + z * z - self.b ** 2 - self.c ** 2) / (2 * self.b * self.c)
        if not -1 + 1e-8 < cosine < 1 - 1e-8:
            raise ValueError("unreachable or singular foot target")
        knee = math.acos(cosine)
        hip = math.atan2(-z, horizontal) - math.atan2(self.c * math.sin(knee), self.b + self.c * math.cos(knee))
        angles = np.array([math.atan2(y, x), hip, knee])
        if np.any(angles < self.limits[:, 0]) or np.any(angles > self.limits[:, 1]):
            raise ValueError("foot target exceeds estimated simulation joint workspace")
        return angles


class ProceduralController:
    """Bounded sequential gait using time, commanded phase and forward sonar only."""

    def __init__(self):
        self.leg = LegKinematics()
        self.joint_limits = np.tile(self.leg.limits, (4, 1))
        self.nominal_targets = np.tile(self.leg.inverse(SPECS["controller"]["nominal_foot_local_m"]), 4)
        half = SPECS["geometry_m"]["body_side"] / 2
        self.anchors = np.array([[half, half, 0], [half, -half, 0], [-half, half, 0], [-half, -half, 0]])
        self.rotations = np.array([[[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]] for a in LEG_ANGLES])
        self.nominal_feet = np.array([anchor + rotation @ np.array(SPECS["controller"]["nominal_foot_local_m"]) for anchor, rotation in zip(self.anchors, self.rotations)])
        self.reset()

    def reset(self, seed: int = 1, model=None) -> None:
        # Seed/model are explicit interface arguments; this open-loop controller
        # has no random or privileged model-state dependency.
        self.elapsed_s = 0.
        self.crawl_time_s = 0.
        self.phase = 0.
        self.targets = self.nominal_targets.copy()
        self.feet_body = self.nominal_feet.copy()
        self.state, self.reason = "standing", ""
        self.saturation_count = 0

    def _clearance_reason(self, observation: dict) -> str:
        sonar = observation.get("sonar", {})
        try:
            capture = float(observation["capture_time_s"])
            deadline = float(observation["deadline_s"])
            age = float(sonar["age_s"])
            distance = sonar.get("distance_m")
            if not all(math.isfinite(v) for v in (capture, deadline, age)):
                return "invalid sensor timing"
            if capture < 0 or capture > self.elapsed_s + 1e-9:
                return "capture timestamp is outside the controller simulation clock"
            if deadline < self.elapsed_s:
                return "goal deadline expired"
            if age < 0 or age > .15 or self.elapsed_s - capture > .15:
                return "stale sonar"
            if sonar.get("valid") is not True or sonar.get("no_echo") is not False:
                return "sonar unavailable; no echo is unknown"
            if isinstance(distance, bool) or distance is None or not math.isfinite(distance) or distance <= 0:
                return "invalid sonar"
            if distance < .22:
                return "forward clearance blocked"
        except (KeyError, TypeError, ValueError):
            return "sensor timing unavailable"
        return ""

    def step(self, observation: dict, bounded_goal: str, dt: float) -> np.ndarray:
        if bounded_goal not in MODES or not math.isfinite(dt) or not 0 < dt <= .02:
            raise ValueError("unsupported mode or controller timestep")
        self.elapsed_s += dt
        if bounded_goal == "stop":
            self.state, self.reason = "stopped", "hold last commanded joint targets; physical momentum may persist"
            return self.targets.copy()
        reason = self._clearance_reason(observation) if bounded_goal == "crawl" else ""
        if reason:
            self.state, self.reason = "inhibited", reason
            return self.targets.copy()
        self.state, self.reason = ("crawling" if bounded_goal == "crawl" else "standing"), ""
        feet = self.nominal_feet.copy()
        if bounded_goal == "crawl":
            self.crawl_time_s += dt
            active = max(0., self.crawl_time_s - SPECS["controller"]["startup_stand_s"])
            self.phase = (active / SPECS["controller"]["period_s"]) % 1.
            if active > 0:
                # Sequential FL, RR, FR, RL swing order; 80% stance duty.
                slots = (0., .5, .75, .25)
                for i, slot in enumerate(slots):
                    phase = (self.phase - slot) % 1.
                    if phase < .2:
                        swing = phase / .2
                        smooth = swing * swing * (3 - 2 * swing)
                        feet[i, 0] += SPECS["controller"]["stride_m"] * (smooth - .5)
                        feet[i, 2] += SPECS["controller"]["swing_height_m"] * math.sin(math.pi * swing) ** 2
                    else:
                        feet[i, 0] += SPECS["controller"]["stride_m"] * (.5 - (phase - .2) / .8)
                # Small scheduled body shift away from the swinging corner.
                # This uses commanded phase only, never measured support/pose.
                slot_index = int(self.phase * 4)
                swing_leg = (0, 3, 1, 2)[slot_index]
                local_phase = self.phase * 4 - slot_index
                shift = -.010 * np.sign(self.anchors[swing_leg, :2]) * math.sin(math.pi * local_phase) ** 2
                feet[:, :2] -= shift
        try:
            requested = np.concatenate([self.leg.inverse(rotation.T @ (foot - anchor)) for foot, anchor, rotation in zip(feet, self.anchors, self.rotations)])
        except ValueError:
            self.state, self.reason = "inhibited", "procedural target outside estimated workspace"
            return self.targets.copy()
        bounded = np.clip(requested, self.joint_limits[:, 0], self.joint_limits[:, 1])
        self.saturation_count += int(np.count_nonzero(requested != bounded))
        slew = SPECS["estimated_actuator"]["target_slew_rad_s"] * dt
        self.targets += np.clip(bounded - self.targets, -slew, slew)
        self.feet_body = feet
        return self.targets.copy()
