"""Crab-owned primitive MJCF; every physical parameter is provisional.

Frames: body +X forward, +Y left, +Z up, metres/radians/seconds.
Four local leg +X axes point out from the body's corners. Each local leg has
yaw about +Z, hip/knee about +Y (positive pitch bends +X toward -Z).
This mapping is our simulation convention, not a physical servo calibration.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

SPECS = json.loads(Path(__file__).with_name("specs.json").read_text(encoding="utf-8"))
MODEL_VERSION = SPECS["model_version"]
DT = SPECS["simulation"]["dt_s"]
SCENARIOS = ("flat", "low_friction", "sonar_dropout")
LEG_NAMES = ("front_left", "front_right", "rear_left", "rear_right")
LEG_ANGLES = (math.pi / 4, -math.pi / 4, 3 * math.pi / 4, -3 * math.pi / 4)
JOINT_NAMES = tuple(f"{leg}_{joint}" for leg in LEG_NAMES for joint in ("yaw", "hip", "knee"))


def build_mjcf(scenario: str = "flat", *, fixed_base: bool = False) -> str:
    """Build one bounded model, with an optional suspended base for leg checks."""
    if scenario not in SCENARIOS:
        raise ValueError("unsupported lab scenario")
    geometry = SPECS["geometry_m"]
    mass = SPECS["estimated_mass_kg"]
    servo = SPECS["estimated_actuator"]
    contact = SPECS["estimated_contact"]
    limits = SPECS["estimated_joint_limits_rad"]
    a, b, c = geometry["coxa"], geometry["femur"], geometry["tibia"]
    half = geometry["body_side"] / 2
    friction = contact["low_friction"] if scenario == "low_friction" else contact["flat_friction"]
    limbs = []
    actuators = []
    for index, (name, angle) in enumerate(zip(LEG_NAMES, LEG_ANGLES)):
        sx, sy = (1 if index < 2 else -1), (1 if index % 2 == 0 else -1)
        joints = []
        for joint in ("yaw", "hip", "knee"):
            lo, hi = limits[joint]
            axis = "0 0 1" if joint == "yaw" else "0 1 0"
            joints.append(f'<joint name="{name}_{joint}" axis="{axis}" range="{lo} {hi}"/>')
            actuators.append(f'<position name="{name}_{joint}_servo" joint="{name}_{joint}" kp="{servo["kp_nm_rad"]}" kv="{servo["kv_nm_s_rad"]}" ctrllimited="true" ctrlrange="{lo} {hi}" forcelimited="true" forcerange="-{servo["torque_limit_nm"]} {servo["torque_limit_nm"]}"/>')
        limbs.append(f'''
          <body name="{name}_coxa" pos="{sx * half} {sy * half} 0" euler="0 0 {angle}">
            {joints[0]}
            <geom name="{name}_coxa_geom" type="capsule" fromto="0 0 0 {a} 0 0" size="0.005" mass="{mass['coxa']}" rgba="0.25 0.65 0.72 1"/>
            <body name="{name}_femur" pos="{a} 0 0">
              {joints[1]}
              <geom name="{name}_femur_geom" type="capsule" fromto="0 0 0 {b} 0 0" size="{geometry['link_radius']}" mass="{mass['femur']}" rgba="0.84 0.55 0.25 1"/>
              <body name="{name}_tibia" pos="{b} 0 0">
                {joints[2]}
                <geom name="{name}_tibia_geom" type="capsule" fromto="0 0 0 {c} 0 0" size="{geometry['link_radius']}" mass="{mass['tibia']}" rgba="0.35 0.72 0.78 1"/>
                <geom name="{name}_foot" type="sphere" pos="{c} 0 0" size="{geometry['foot_radius']}" mass="{mass['foot']}" rgba="0.10 0.16 0.20 1"/>
                <site name="{name}_foot_site" pos="{c} 0 0" size="0.001"/>
              </body>
            </body>
          </body>''')
    free = "" if fixed_base else '<freejoint name="root"/>'
    height = .35 if fixed_base else .091
    return f'''<mujoco model="{MODEL_VERSION}">
      <compiler angle="radian" inertiafromgeom="true"/>
      <option timestep="{DT}" gravity="0 0 -9.81" integrator="implicitfast" solver="Newton" iterations="40" tolerance="1e-10"/>
      <default>
        <joint type="hinge" limited="true" solreflimit="0.005 1" solimplimit="0.95 0.99 0.001" damping="{servo['joint_damping_nm_s_rad']}" armature="{servo['armature_kg_m2']}" frictionloss="{servo['joint_frictionloss_nm']}"/>
        <geom group="1" friction="{friction} 0.005 0.0001" condim="3" solref="0.01 1" solimp="0.9 0.95 0.001"/>
      </default>
      <worldbody>
        <geom name="floor" type="box" group="0" pos="0 0 -0.02" size="1 1 0.02" rgba="0.16 0.21 0.25 1"/>
        <geom name="wall_front" type="box" group="0" pos="1.02 0 0.12" size="0.02 1.04 0.12" rgba="0.3 0.4 0.45 0.6"/>
        <geom name="wall_back" type="box" group="0" pos="-1.02 0 0.12" size="0.02 1.04 0.12" rgba="0.3 0.4 0.45 0.6"/>
        <geom name="wall_left" type="box" group="0" pos="0 1.02 0.12" size="1 0.02 0.12" rgba="0.3 0.4 0.45 0.6"/>
        <geom name="wall_right" type="box" group="0" pos="0 -1.02 0.12" size="1 0.02 0.12" rgba="0.3 0.4 0.45 0.6"/>
        <body name="crab" pos="0 0 {height}">
          {free}
          <geom name="body" type="box" size="{half} {half} {geometry['body_half_height']}" mass="{mass['body']}" rgba="0.83 0.35 0.19 1"/>
          {''.join(limbs)}
        </body>
      </worldbody>
      <actuator>{''.join(actuators)}</actuator>
    </mujoco>'''
