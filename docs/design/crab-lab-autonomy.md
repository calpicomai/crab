# Crab-owned movement and Crab Lab

Design decision, 2026-10-27; not an implemented simulator or hardware acceptance.
Crab owns its model, kinematics, procedural controller, behavior and personality.
ROS supplies standard infrastructure. Vendor material supplies hardware facts and
temporarily isolated access to working electronics, not the desired behavior
architecture. This iteration implements only the read-only observation bridge.

## What autonomy means here

Behavior autonomy chooses what to do from evidence: investigate an observed
object, wait, seek interaction, or stop. Navigation estimates location, plans a
route and tracks progress. Locomotion produces foot/body trajectories and servo
targets. Adaptive locomotion changes those trajectories using measured state.
A procedural gait computes trajectories continuously from speed, phase, stance
and geometry; it need not be a fixed vendor frame list or a learned policy.
A learned policy is another controller that needs training, bounded outputs and
validation. Neither randomness nor calling a script makes movement adaptive.

Current stationary perception/interaction can be autonomous without locomotion.
With verified monocular imagery and forward sonar, future limited obstacle-aware
behavior is possible in a supervised flat enclosed arena, after perception and
motion acceptance. Those sensors do not establish metric localization, complete
clearance, cliff detection, contact, balance or recovery. No current autonomous
walking/navigation claim follows from the stock software having walked once.
LLMs may suggest validated goals; they never write servo targets or override
stale-input, power, stop, expiry or workspace limits.

## Evidence and missing hardware facts

The current Crab Pi code reads battery and a single forward ultrasonic sensor;
camera capture uses the owner-identified OV5647. Optional USB microphone/speaker
are interaction devices, not locomotion state. There is no integrated IMU,
encoder, joint-state measurement, foot-contact sensor, force/torque measurement
or physical odometry in `robot/`, `brain/` or `shared/`. Reported completed vendor
cycles and commanded angles are not measured displacement or joint position.

| Item | Evidence available | Required model/validation work |
| --- | --- | --- |
| Mechanism | Four legs, three driven joints each. Current upstream PiCrawler code declares link constants A48/B78/C33 and body-side77, conventionally mm. | Treat as source hints, not a dimensioned drawing. Verify joint centers, axes, handedness, body anchors, horn zero and assembly revision against measurements/drawings. Do not infer full CAD or inertias. |
| Limits | Upstream kinematics clamp alpha[-90,90], beta[-10,90], gamma[-60,60] degrees. | These are software clamps, not certified collision-free mechanical limits. Establish narrower per-joint workspace, self-collision, cable and horn clearances. |
| Servo access | Vendor Servo maps commands to 50Hz PWM/500–2500us; HAT PWM uses I2C registers. Its API does not return shaft position. | Confirm installed servo model, voltage/power rail, pulse calibration, slew/deadband/backlash, loaded torque/speed and thermals. Do not equate PWM rate with achieved servo response. |
| Calibration | Upstream Robot reads a 12-value `picrawler_servo_offset_list` from `~/.config/.picrawler.config`; PiCrawler defines channel order and signs. | Preserve file byte-for-byte; read-only importer must validate length, units/order/signs and refuse missing/ambiguous calibration. Test conversion against fake drivers before any replacement. Never rewrite offsets implicitly. |
| Dynamics | No verified link masses, center of mass, inertia, foot compliance/friction or actuator response curve. | Publish uncertainty ranges; provisional models are labelled provisional. Add measurement gates before sim-to-real. No invented torque/current feedback. |

Source inspection is of current upstream files, not proof that their revision
matches the installed library. Verify that match before replacing the physical
adapter. [PiCrawler geometry/limits](https://raw.githubusercontent.com/sunfounder/picrawler/main/picrawler/picrawler.py),
[Robot calibration and commanded-position bookkeeping](https://raw.githubusercontent.com/sunfounder/robot-hat/2.5.x/robot_hat/robot.py),
[servo PWM mapping](https://raw.githubusercontent.com/sunfounder/robot-hat/2.5.x/robot_hat/servo.py),
[HAT PWM transport](https://raw.githubusercontent.com/sunfounder/robot-hat/2.5.x/robot_hat/pwm.py).

For useful attitude feedback, integrate and validate an IMU with known mounting,
bias, timestamps, rate and saturation behavior. For joint tracking, overload or
slip/contact adaptation, measured joint positions and/or contact/load sensing are
needed; current PWM readback cannot supply them. Exact upgrades need electrical,
mechanical and resource review. Camera-based motion estimation could eventually
aid navigation, but needs calibration, observability and failure tests; no
monocular metric-scale or visual-SLAM accuracy is assumed.

## Dependency ownership and replacement

| Current dependency | Role | Direction / migration risk |
| --- | --- | --- |
| `picrawler.Picrawler`, MoveList, do_action/do_step | Geometry, scripted frames, interpolation, MCU initialization, calibration application | Replace convenience/gait behavior with Crab model/controller. Retain behind the existing disabled hardware seam until the replacement passes initialization, signs, offsets, timing and stop tests. Constructor currently moves servos; never instantiate it to inspect capabilities. |
| `robot_hat.Robot` | Offset loading, direction/origin application and software target bookkeeping | Replace deliberately with a read-only calibration adapter and explicit target semantics; avoid double offsets/sign inversions. Never publish its software positions as measured joint states. |
| `robot_hat.Servo/PWM/I2C`, battery/sonar/GPIO | Electrical access to the verified HAT | Initially isolate as low-level drivers. A project driver is feasible only after protocol, reset, timing and failure behavior are validated. Driver replacement is separate from gait design. |
| Picamera2/libcamera and ALSA tools | Camera/audio access | Keep standard hardware infrastructure behind source-owned capture/playback interfaces; replacing working media drivers adds no custom personality. |
| ROS, MuJoCo, numerical/browser libraries | Messaging, physics and visualization | Reuse maintained infrastructure with explicit versions/licenses. Project ownership means our architecture/controllers/assets, not inventing every library. |

Both inspected SunFounder repositories publish GPLv3 license files. Do not copy
vendor motion implementations into a newly labelled proprietary controller.
Track provenance/notices and review distribution/linking obligations before
shipping replacements. The repository currently has no chosen top-level license;
the new message package says UNLICENSED rather than inventing permission.
[PiCrawler license](https://raw.githubusercontent.com/sunfounder/picrawler/main/LICENSE),
[Robot HAT license](https://raw.githubusercontent.com/sunfounder/robot-hat/2.5.x/LICENSE).

## Simulator choice and Windows host

Choose **CPU MuJoCo first**, with Crab-owned MJCF/model parameters and a small
browser arena. It supports articulated dynamics, contacts and configurable
actuators; those capabilities do not make an inaccurate model faithful.
Its Python bindings are infrastructure, not a complete game/editor. We must build
the scene/view bridge and experiment controls.
[MuJoCo overview](https://mujoco.readthedocs.io/en/stable/overview.html),
[model format](https://mujoco.readthedocs.io/en/stable/modeling.html),
[Python interface](https://mujoco.readthedocs.io/en/stable/python.html).

| Option | Fit and cost | Decision |
| --- | --- | --- |
| MuJoCo | Small articulated-controller experiments, headless repeatable rollouts, explicit actuator/contact models; custom ROS/browser adapter needed. | First controller laboratory. No GPU requirement for the initial CPU engine. Benchmark actual CPU, model and rendering costs before promising real-time or parallel throughput. |
| Gazebo | Strong ROS/world/sensor integration and an available web visualization path; more integration/version surface for the first leg controller. Humble's official pairing is Fortress, not Harmonic. | Revisit for larger ROS navigation/world integration. Do not install a conflicting Gazebo combination just to get a viewer. |
| Isaac Sim | Current published requirements exceed the known 3070Ti setup; supported aarch64 system is DGX Spark, not this Jetson. | Exclude from the first milestone. No paid compute or assumed Jetson support. |

[Gazebo/ROS compatibility](https://gazebosim.org/docs/harmonic/ros_installation/),
[Gazebo browser visualization](https://gazebosim.org/docs/harmonic/web_visualization/),
[current Isaac Sim requirements](https://docs.isaacsim.omniverse.nvidia.com/latest/installation/requirements.html).

The owner's Windows PC has 64GB RAM and an RTX3070Ti; CPU, Windows version,
architecture and actual VRAM are unverified. Prefer native Windows x86-64 for
the first Python CPU simulation and localhost browser viewer: MuJoCo publishes
Windows binaries. Validate the selected release's Python/wheel support first;
do not reuse Humble's Python3.10 constraint blindly for the latest simulator.
If a later ROS integration needs Linux, evaluate WSL2 with a deliberately chosen
Ubuntu distro, separate environment and localhost-only boundary. WSL is an
installation/virtualization decision, not a prerequisite for the first physics
test. No WSL, CUDA, driver or simulator installation is performed here.
[Official MuJoCo platforms](https://raw.githubusercontent.com/google-deepmind/mujoco/main/README.md),
[Microsoft WSL prerequisites](https://learn.microsoft.com/en-us/windows/wsl/install).

GPU training is optional later. First measure one rollout's simulated-seconds/s,
p50/p95 step time, RAM, viewer latency and CPU utilization on that PC. Then test
a small fixed batch and a selected learning framework against actual VRAM.
Do not promise large parallel training from a GPU name or 64GB system RAM.

## Shared interfaces and isolation

Use a transport-free Crab controller with explicit `reset(seed, model)` and
`step(observation, bounded_goal, dt)` interfaces. Observations contain only
available measured channels, validity, provenance, sequence/capture time and
uncertainty. Actuator targets are separate from measured state. The simulated
adapter applies bounded targets to the physics engine; a **future reviewed Pi
adapter** applies them locally to hardware, preserving calibration and local
watchdogs. Jetson behavior goals never stream per-servo trajectories over ROS.

The training runner has no Pi client, ROS command gateway, hardware plugins,
serial/I2C/GPIO mounts or configured robot URL. Initially run with external
network denied; the browser binds loopback. Its own reset/pause/goal controls
cannot route to `/sim/control` or physical command endpoints. A different ROS
domain/name alone is insufficient isolation. Replay is a file-to-simulator path;
there is no replay-to-hardware mode.

Separate `PolicyObservation` from privileged `EvaluationTruth`. The engine may
know exact pose, velocity, contact forces and terrain; only the evaluator and
explicitly marked training critic may consume that truth. Deployment-shaped
policy input must mask channels absent on the real robot. Test that adding
privileged fields does not change the deployed policy's inputs. Future IMU or
joint/contact channels require explicit versioned capability profiles.

The browser draws the physics engine's reported transforms with bounded latest
updates, not an animation that assumes commands succeeded. Show simulation,
model uncertainty, seed and timescale persistently. Pause, reset, replay and slow
motion operate only on virtual state. A learned internal predictive world model
is a later research component; it is distinct from this physics test world and
from rendering a virtual Crab.

## First simulator milestone and gates

1. **Model and fake driver:** independent frames/units, forward/inverse kinematics,
   per-joint workspace and self-collision tests; provisional primitive geometry,
   measured-or-range mass parameters. Reject unreachable/singular targets rather
   than silently projecting them. Start with a suspended/fixed-base single leg.
2. **Crab Lab v0:** floating 12-joint body on a flat bounded arena, position-servo
   model with finite torque/slew, delay, damping, backlash/deadband and saturation.
   No perfect instantaneous position or torque-control assumption. Add contact
   friction/compliance, sensor noise/dropout/delay and uncertainty sweeps. CPU
   physics, one headless seed and a browser scene with pause/reset/replay. No RL
   and no hardware adapter in this milestone.
3. **Procedural controller:** generate smooth swing/stance trajectories and
   bounded body goals using our kinematics. Test support margins, joint limits,
   foot clearance, slip and attitude only where observable. Test missing/stale
   sensors, blocked leg, reduced torque, bad calibration, timing jitter, watchdog
   expiry, falls, unexpected terrain and command cancellation. Do not optimize
   distance while ignoring falls or servo limits.
4. **Repeatable evaluation:** fixed engine/model/controller versions, seed and
   input log; record joint tracking error, saturation/time at limits, simulated
   contacts/slip, falls, progress error, clearance, deadline misses and energy
   proxy (labelled simulated). Fixed-seed regression tolerances, not a promise of
   bitwise identity across OS/CPU/solver versions. At least held-out friction,
   mass, latency and noise cases before tuning a policy further.
5. **Optional learning/perception:** only after a deterministic baseline, compare
   a small learned residual/policy on held-out seeds; preserve output limits and
   policy-observation restrictions. Add visual/sonar perception and behavior
   coordination independently; simulator truth never bypasses the deployable
   estimator. Appearance/tempo can be distinctive without unsafe random goals.
6. **Separate sim-to-hardware acceptance:** accepted storage/power, verified
   geometry/calibration/servo characterization and required feedback. Compare
   one joint/leg under support, then body poses, then a bounded cycle in a clear
   supervised arena with physical cutoff. Reconcile model error before longer
   tests. Human presence remains necessary at these gates; simulated success is
   not permission for unattended hardware training or navigation.

Rollback is simple while simulator-only: stop Crab Lab; existing devices and
current companion remain untouched. A later physical-controller replacement
must restore the old sole owner with STOP latched and require fresh deliberate
authorization. Never run old and new hardware writers concurrently.

## Models and future training data

These are different artifacts: the **physical robot model** describes geometry,
mass, joints, actuators and sensors; a **control policy** maps observations/goals
to bounded actions; an optional **learned world model** predicts future state;
language/perception models interpret speech/images or propose high-level intent.
The current companion uses programmed behavior with optional separately enabled
models. This bridge does not train a model or record a dataset. It publishes a
bounded live diagnostic snapshot only; nothing here starts camera/mic capture.

Do not seek an unrelated animal/humanoid motion dataset for the first locomotion
controller. Generate simulated episodes only after the physical/observation
contracts and evaluation metrics are credible. Proposed episode manifest:

| Group | Required fields |
| --- | --- |
| Identity/provenance | Schema/run/episode IDs, simulated source, UTC creation time, model/engine/controller versions and hashes, asset provenance/licenses, capability profile, seed, platform and solver settings. |
| Conditions | Sampled mass/friction/servo/noise/latency parameters with units, bounds and distributions; terrain/goal configuration. Mark provisional hardware assumptions explicitly. |
| Per step | Simulation time and dt, policy-visible observations with validity/capture age, goal and expiry, requested action, bounded/applied target, saturation/intervention flags and result/failure. |
| Evaluation only | Privileged ground-truth pose/velocity/contact/terrain, metrics, termination reason and uncertainty. Separate storage/key namespace and loader access from actor observations. |
| Split/replay | Fixed train/validation/test seed and condition lists, episode/trajectory grouping, replay log, retention policy and content hashes. Never split adjacent frames from one episode across train and test. |

Keep held-out condition ranges and failure scenarios, not just unseen random
seeds from identical easy terrain. Store bounded summaries plus explicitly
requested episode traces; initial design budget is100episodes/1GiB per run with
visible stop-on-cap and explicit export/delete. That is a proposed limit, not an
implemented recorder. Training/evaluation logs cannot instantiate a physical
adapter or replay commands into a live ROS graph.

Any later real camera, microphone or personal interaction dataset needs an
explicit opt-in, named purpose, local destination, bounded retention and deletion
controls. Do not add it to telemetry by default. Real calibration/system-ID data
requires separately approved supervised hardware tests and measured provenance;
simulation labels cannot stand in for those measurements.

## Vision and reasoning acceptance

Good vision means useful, timed evidence with known failure behavior, not just a
clear preview or a fluent description. Keep the owner's OV5647 clarity/latency
work separate from perception accuracy. First establish calibrated image
geometry, capture provenance and measured pipeline latency. Evaluate detections
on held-out scenes, illumination, motion blur, occlusion and unknown objects;
report misses and false positives as well as confidence. Temporal tracks need
stable IDs within an episode, velocity uncertainty, expiry and re-identification
limits; track identity is not person identity or persistent recognition.

Obstacle/free-space interpretation needs a defined ground plane, footprint and
uncertainty margin. Monocular images do not by themselves provide reliable
metric depth or unseen-floor safety. Sonar can corroborate a narrow range, not
fill every unobserved pixel as free space. Perception age, missing echo, ambiguous
depth, lost track or localization loss must produce unknown/stop/ask behavior,
not an invented map. Test those failures in the same simulator scenarios used
for planning, then validate with separately authorized real scenes.

Scene memory stores timestamped observations, inferred hypotheses and generated
narrative separately, with evidence links and expiry. The planner owns goals,
preconditions, a bounded horizon, expected observable outcomes and recovery;
the coordinator arbitrates one intention and cancellation. Verify action results
against independent observations where available. A completed command is not
proof that a target was reached; repeated failure should cancel/replan/stop,
never loop indefinitely or retry uncertain hardware execution.

Acceptance scenarios include a moved obstacle, a disappearing target, a false
positive, stale/contradictory sonar and vision, ambiguous depth, slipping without
progress, an impossible goal, delayed model output, reboot and human STOP. Score
task completion alongside collision/fall rate, wrong confident decisions,
recovery time, evidence age and resource use. The local bounded controller and
Pi safety owner remain independent below planner/model failures.

The owner accepts pretrained perception components with Crab-owned behavior and
control. No specific model is selected, downloaded, trained or licensed by this
plan. Compare candidate capability, provenance/license, Jetson RAM/latency and
held-out accuracy before selection. Custom behavior/control does not require
training a vision foundation model from scratch.

The desired improvement loop is: observe/predict → choose a bounded action →
measure outcome/intervention → curate offline → train/version → held-out
regression → review/approve → reversible release. Include prediction and model
versions, sensor quality, corrections and human interventions in the episode
schema. No uncontrolled online policy updates, automatic upload or recording.
The Tesla analogy expresses the owner's product goal; it establishes no shared
capability or borrowed architecture.

A future decision view overlays actual tracked detections on the captured frame,
with age/uncertainty and separate estimated free-space geometry. Show current
goal, evaluated planner candidates and chosen path/footholds **only when those
components produce real events**, constraints/reason codes, intended action and
observed outcome. Link each result to its evidence/frame ID. Missing planner,
depth or motion measurements remain explicitly unavailable; never draw a made-up
path or report movement from a command alone. Use concise grounded explanations,
not private model chain-of-thought. This is a future dashboard/data contract;
the read-only bridge does not yet publish perception tracks or planner events.

## Optional self-hosted LiveKit layer

LiveKit is a candidate for browser video/audio and selected observation delivery,
not the learning loop itself. Its robotics media interfaces accept raw or
pre-encoded frames and support timestamp metadata. Reuse the existing capture
owner; do not create another camera consumer/encoder merely to add transport.
Self-hosting permits local infrastructure ownership but adds operating work.
[Robotics overview](https://docs.livekit.io/robotics/),
[video interfaces](https://docs.livekit.io/robotics/media/video/),
[self-hosting](https://docs.livekit.io/transport/self-hosting/).

**Do not launch ROS Portal with absent/default configuration:** current docs
describe forwarding every topic in both directions. A Crab proposal must require
an explicit, validated allowlist of outbound observation topics only, initially
exact `/crab/observations` and `/crab/diagnostics`, rate limited. No inbound topic,
service proxy, control route, parameter/graph administration, wildcard namespace
or latching. Pin/review the Portal version and test that undeclared and incoming
routes cannot reach the local graph. Local command authority, leases, monotonic
watchdogs and STOP remain outside this media path.
[Portal routing/defaults](https://docs.livekit.io/robotics/integrations/ros-portal/configuration/).

Self-hosted Egress is a separately deployed recording service; a video recording
does not supply synchronized observations, predictions, actions, outcomes and
interventions. An opt-in recorder would need the episode manifest above, shared
frame/event IDs, capture-clock uncertainty, gaps and retention controls.
[Egress deployment](https://docs.livekit.io/transport/self-hosting/egress/).

Before adoption, compare existing JPEG against a supported codec path at fixed
resolution/scene/client count: capture/encode/transfer/decode and p95/p99 frame
age, bitrate, CPU/RSS/GPU/shared memory, loss, reconnect and slow-subscriber
behavior. Codec/hardware support on the actual Pi/Jetson is a test gate, not a
claim. First benchmark with synthetic/local media and no hardware/control route;
owner-controlled capture comparison later. Keep JPEG as rollback. No LiveKit
dependency, server, credentials, firewall changes, recording, upload or external
media transmission is introduced by this design or the ROS bridge milestone.
