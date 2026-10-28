# Crab Lab — opt-in physics laboratory

A maintained opt-in CPU MuJoCo laboratory with a provisional12-joint Crab, one bounded arena,
an independently written procedural controller and a browser view of actual
physics transforms. It is **not** a validated digital twin, learned policy,
navigation system or hardware-ready controller. There is no physical adapter,
Pi URL, ROS command publisher, camera/microphone capture or automatic recording.
Existing Pi/Jetson software and configuration are not imported or changed.

## Try the local prototype

Use a separate development checkout or the `prototype` directory in the review
archive. Python3.12 is the tested interpreter. No GPU or ROS installation is
needed for this CPU prototype. These are future user-run instructions; nothing
was installed on the owner's Windows PC or robot by this task.

Windows PowerShell, from the prototype root containing `lab`:

```powershell
if (Test-Path .venv) { throw 'Use a new dedicated folder; preserve the existing environment.' }
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r lab\requirements.txt
.\.venv\Scripts\python.exe -m lab serve
```

Linux/macOS, in a new dedicated folder:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r lab/requirements.txt
.venv/bin/python -m lab serve
```

Open `http://127.0.0.1:8765`. The world starts paused. Select Stand, Crawl or Hold,
then Run. Pause freezes simulation time; Step advances20ms. Reset applies the
selected seed/scenario and pauses. Drag/orbit, scroll/zoom or use arrow keys on
the canvas. Expand is optional. Hold stops generating new gait targets; simulated
momentum can persist. This is not the physical robot's STOP control.

The process binds loopback only, rejects untrusted Host/cross-origin controls,
bounds control bodies/step counts/rate and has one physics writer/latest snapshot.
A physics failure pauses and latches a visible error; Run/Step/mode changes stay
blocked until Reset. Disconnect marks the last view stale and disables controls.
Ctrl+C exits only this laboratory. No services, global settings or dependencies
are installed by the launcher. To roll back, stop it and remove its dedicated
development folder when no longer needed; preserve any explicit exports first.

## What is real in the simulation

The free body falls under gravity; contacts, joint limits, torque/slew saturation
and delayed position-servo targets affect motion. The browser does not move a
sprite in response to a command. Its geometry comes from MuJoCo geom positions
and rotation matrices; body height, displacement, tilt and contact forces are
privileged simulated measurements. They are clearly separate from the
stock-like policy observation, which has delayed/noisy sonar, time, commanded
phase and goal. There is no hidden joint encoder, IMU or foot-contact input to
the controller. Future real feedback requires an explicit capability change.

The crawl computes swing/stance targets and independent yaw/two-link IK; it does
not replay SunFounder MoveList frames. It remains open-loop position control,
with no measured balance feedback or fall-recovery controller. The evaluator
reports falls without silently supplying privileged state to the policy.

Scenarios: flat ground, low friction and periodic sonar dropout. No echo is
unknown and inhibits crawl; capture age/future stamps and expired goals are
checked. This small fixed world is not learned scene understanding or a learned
internal world model. There is no perception model, path planner or training.

## Parameters and hardware inventory

[The model manifest](../lab/specs.json) gives sources, units, values and uncertainty.
Current upstream PiCrawler source hints A48/B78/C33/body77 are interpreted as
millimetres. Joint axes/anchors, primitive dimensions beyond those hints,
mass/inertia/centres of mass, pivot limits, friction/compliance, torque, delay,
deadband and sensor behavior are estimated. Declared uncertainty ranges are
not yet sampled as domain randomization. Only sonar noise is seeded/randomized.
There is no validated motor electrical, battery sag, backlash or foot compliance
model. Exact servo identity, loaded response, geometry and masses remain missing.

| Current project dependency | Physical role | Replacement direction |
| --- | --- | --- |
| `robot/gait.py` → `picrawler.Picrawler` | Current vendor initialization, calibration application and scripted primitives | Keep isolated until Crab-owned local controller/driver passes physical gates. This Lab uses none of it; do not instantiate vendor code to inspect it because construction moves targets. |
| `robot/sensors.py` → Robot HAT Pin/Ultrasonic/battery | Sensor electrical access | Retain necessary access behind project interfaces; validate atomic capture/provenance before replacement. No sonar rewrite here. |
| `robot/speaker_hat.py` → Robot HAT board/device metadata | Verified opt-in amplifier access | Keep current bounded lifecycle; no audio/mic operation in Lab. |
| Picamera2/libcamera, ALSA | Hardware media infrastructure | Keep standard drivers; custom behavior does not require replacing working capture. |
| ROS/MuJoCo/numerical/browser components | Messaging, physics and display infrastructure | Use standard maintained components with accurate licenses; own Crab's model, controller, experiments and behavior. |

There is no integrated measured joint feedback, IMU, foot contact or physical
odometry in the current Crab stack. A commanded servo angle is not shaft
feedback. Sonar and monocular OV5647 imagery do not establish balance, complete
free space or cliff safety. See the [autonomy design](design/crab-lab-autonomy.md)
for source-backed geometry/calibration/driver evidence and missing feedback.

## Reproducible episodes, without automatic collection

Run one synthetic episode without writing a file:

```bash
.venv/bin/python -m lab episode --seed 1 --scenario flat --seconds 8 --mode crawl
```

On Windows use `.\.venv\Scripts\python.exe` for the same arguments. An explicit
`--output new-episode.json` creates a new local file exclusively; an existing file
is never overwritten. Duration is bounded0.05–30s and export size8MiB. No camera,
microphone, real user interaction, network upload or hardware replay is supported.

The versioned manifest includes spec hash, seed, scenario, engine/controller,
Python/NumPy versions, controller/physics/model source hashes, estimated conditions
and units. Sampled transitions20Hz keep policy observations,
requested target summaries, simulated outcome, constraints/interventions and
privileged evaluation truth separate. This is not an every-command replay log or
training-ready dataset. Flat is a development fixture; low friction/dropout are
evaluation-condition fixtures. There is no training job or enforced dataset
splitter yet. Do not treat that label as proof of a held-out training protocol.

Each reported controller decision retains the exact observation it consumed,
simulation time, mode, generated target and constraint reason. The viewer exposes
that record separately from the next policy observation and privileged outcome.
It is procedural evidence, not an inferred explanation or private chain-of-thought.

Run the bounded research regression matrix without collecting real data:

```bash
.venv/bin/python -m lab evaluate --seeds 1,2 --seconds 5
```

This evaluates the three fixed scenarios for1–5distinct seeds, reports explicit
assertions and exits nonzero on a regression. An optional new `--output` file uses
the same8MiB exclusive-create limit as episodes. Fingerprints exclude wall-clock
throughput/host labels; they are scoped to matching code/model/runtime, not
bitwise equivalence across platforms. Short runs omit duration-dependent progress
assertions explicitly; use at least4s for flat-ground forward-progress checks.
These are provisional research criteria, not hardware safety acceptance. Robot
self-collision is currently disabled in the model and is not certified by them.

First establish model/servo fidelity and deterministic controller baselines.
Then extend scenario sweeps and offline curation/evaluation before any learning.
Pretrained perception components are acceptable to the owner, but none is
selected/downloaded here. Future decision overlays must show actual timed
detections/tracks, uncertainty, planner candidates/selected goals and observed
outcomes; they cannot fabricate a path or expose private chain-of-thought.
Real media collection needs explicit opt-in, retention and deletion controls.

## Verify

```bash
.venv/bin/python -m unittest discover -s lab/tests -v
.venv/bin/python -m lab episode --seconds 8 --seed 1 --mode crawl
```

Browser acceptance additionally uses a development-only Playwright installation
and Chromium, while the ordinary viewer needs only a browser. With Lab running:

```bash
python lab/tests/browser_lab.py --serve --output /tmp/crab-lab-browser
```

No browser automation dependency is installed by Lab's runtime requirements.
The dedicated Crab Lab CI workflow installs only Lab dependencies and executes
real headless physics/API/episode tests separately on Linux and Windows. This
does not run on the owner's PC or certify hardware/model fidelity.
Its separate Linux browser job installs `lab/requirements-dev.txt` and owns its
temporary localhost fixture process; there is no user-device installation or
configured robot URL in acceptance tests.
The [verification report](reports/crab-lab-local.md) distinguishes actual CPU
physics/browser results from unrun Windows, GPU and physical acceptance.
