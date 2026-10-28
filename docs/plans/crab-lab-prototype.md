# Crab Lab v0 local prototype

Baseline: read-only ROS bridge tree `8d435dde`, isolated branch
`feat/crab-lab-prototype`. Local implementation only; no publication, hardware
client/driver/ROS command route, capture, recording, device/PC installation or
training. Existing Pi/brain/ROS paths remain untouched.

Deliver a working CPU MuJoCo vertical slice: one provisional12-joint robot,
bounded flat arena, Crab-owned procedural stand/crawl controller, localhost
browser view of actual physics transforms, deterministic episodes and explicit
model uncertainty. A procedural controller is not a learned policy. Robot
geometry hints come from cited upstream specification evidence; masses, inertias,
limits and servo/contact dynamics remain labelled estimates pending measurement.

## Units and ownership

- L1 fresh physics worker owns `lab/__init__.py`, `lab/physics.py`,
  `lab/controller.py`, `lab/model.py`, `lab/specs.json`,
  `lab/tests/__init__.py`, `lab/tests/test_physics.py`. No git writes.
  Proof-first real MuJoCo tests, not mocked dynamics.
- L2 host owns `lab/server.py`, `lab/__main__.py`, `lab/episodes.py`,
  `lab/static/**`, `lab/requirements.txt`, browser/server/episode tests,
  startup/docs/reports. Shared integration is sequential after L1 contract.
- Independent reviewers inspect physics/fidelity/isolation and browser behavior.

## Contract and acceptance

`LabWorld(seed=1, scenario='flat', mode='stand')` supports `step(steps=1)`,
`reset(seed=1, scenario='flat', mode='stand')`, `set_mode('stand'|'crawl'|'stop')`
and `snapshot()`. Fixed dt and explicit seed. Root will adapt presentation to the
returned snapshot, never fabricate movement. Model/render geoms include names,
types, size, position, rotation matrix and RGBA from actual MuJoCo state. Publish
bounded `policy_observation` separately from privileged `evaluation_truth`.
Current hardware lacks integrated joint feedback/IMU/contacts; those simulator
truth channels must not silently enter controller policy input. Body motion and
joint measurements remain explicitly simulated. Controller targets are separate.

Server uses one owner thread/lock, bounded latest snapshots, loopback binding,
rate-limited bounded controls/step counts and no arbitrary paths/URLs or imports
of hardware/robot clients. Pausing/rendering cannot authorize a real command.
Viewer includes simulation/uncertainty, goal/controller state, units, contact and
limit indicators, intended versus actual simulated movement, seed/time, reset,
pause and single-step. Browser arena can orbit/zoom; no unsupported perception,
planner path, learned policy or digital-twin claim.

Episodes record versions/seed/conditions, observable input, bounded target,
simulated outcome, interventions and separate evaluator truth; deterministic
train/eval scenario split. Defaults write no recording; CLI export is explicit
and bounded to a user-selected local file. No live sensor collection/upload.
Test gravity/drop/contact, controller motion, limits, reproducibility,
noise/dropout, action bounds, fall/stale/deadline failure and truth separation.
Report throughput only for this coding host; no Windows/GPU benchmark claim.

No unnecessary framework/model/physics-engine migration or large training run.
Windows x86-64 setup guide is future user-run instruction, not executed here.
