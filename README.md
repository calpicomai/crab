# Crab

Crab is a local mini companion for the SunFounder PiCrawler. The current
milestone listens for engagement, acknowledges it, opens a listening window,
responds and settles nearby. Stillness is the default. There is no autonomous
pursuit, wandering or person recognition in companion mode.

**Software milestone, hardware acceptance pending.** The repository implements
camera and audio transport, a bounded Pi motion layer, optional local inference,
and recoverable memory. Simulator success does not establish physical movement,
audible sound, battery accuracy or safe navigation.

The Raspberry Pi owns capture, speaker output and real-time vendor motion. The
8GB Jetson owns optional STT/TTS, detectors, local LLM interpretation and the one
behavior coordinator. Python and the vendor calibration mechanism are retained;
no per-servo trajectories cross the network. Personal data and runtime inference
stay local. Model files must be provisioned separately; runtime does not download
missing models or automatically start heavy services.

## Start here

Existing launchers remain the entry points:

```bash
bash setup.sh                 # Python environments; does not launch the robot
bash robot/run.sh             # Pi readiness server; actuators disabled by default
bash brain/run.sh check       # read-only readiness and audio diagnostics
bash brain/run.sh pet         # stationary interaction
bash brain/run.sh quiet       # quiet companionship
```

`crab.env` preferences remain usable. `bash brain/run.sh reconfigure` backs up
existing preferences before updating the robot address/name. The launcher does
not install drivers, change power modes, start model services or enable motion.
Use `--` to pass runtime options, for example
`bash brain/run.sh pet -- --no-stt --no-camera`.

Open `http://<pi>:8000/sim` for local typed engagement, stop/resume controls,
input provenance/age/use reasons, intentions and audio results. The `/sim` name
is retained for compatibility; without a simulated world it displays live
telemetry. A person label is only a detector observation, never enrollment or
recognition. The dashboard and HTTP server are intended for a trusted local
network; they do not provide internet-facing authentication.

The dashboard streams telemetry and pulls the newest camera frame at up to
10 updates per second. Slow detection does not hold up status or typed input;
old pending frames/updates are replaced. Feed age, request timing and stale
warnings are visible. See [live-feed measurements](docs/live-feed-performance.md)
for reproducible latency checks and the remaining Pi/LAN/Jetson measurements.

Pi–Jetson status, input events, command leases and results use one bounded local
WebSocket connection. The browser keeps SSE telemetry and separate camera/audio
HTTP feeds. Link health and round-trip time appear on the dashboard. A lost
companion connection latches stop; reconnect never replays a command or resumes
movement. Update Pi and brain together, then run read-only readiness before
starting the companion. See [transport contract and measurements](docs/websocket-transport.md).
Capture timing accepts a measured, bounded clock offset instead of assuming
identical Pi/Jetson/browser wall clocks. See [clock policy, validation limits and
disabled-hardware update steps](docs/companion-clock.md).

Voice/effects are opt-in through `PET_VOICE=1` or `--voice`. Effects work without
Piper; speech additionally requires a local Piper binary and `.onnx` plus
`.onnx.json` model. Missing Whisper/Piper/ALSA stays visibly unavailable. No audio
is emitted in explicit simulation. See [audio diagnosis](docs/audio-acceptance.md)
for the independent Pi I2S/ALSA, service account, direct playback, endpoint,
network and Jetson TTS checks. A successful `aplay` exit still does not prove
that a human heard sound.

## Reproducible development

No hardware is needed, and simulation is always explicit:

```bash
bash sim.sh room              # localhost:8000/sim; type hello, stop, resume, let's play
bash test_sim.sh --unit       # off-body and focused regression tests
bash test_sim.sh              # isolated HTTP/simulator smoke, temporary memory
brain/.venv/bin/python -m brain.pet.scenarios --all --summary
brain/.venv/bin/python -m brain.pet.benchmark --iterations 10
```

Scenarios cover greeting, interruption, ignored commands, sensor dropout, low
battery, blocked route, model timeout, repeated interaction and reconnect. They
record what was sensed, intended, blocked and completed. Scenario timings and
random seeds are reproducible. [Evidence](docs/VALIDATION.md) distinguishes host
measurements from unmeasured Jetson inference.

The kinematic simulator labels displacement/turn assumptions. `turn(degrees)`
rejects nonzero precise-angle requests on both hardware and simulator. The
bounded `turn_steps(left|right, cycles)` primitive reports completed vendor
cycles; physical heading remains unmeasured. Legacy wander/agent experiments
are restricted to explicit simulation until migrated to the new motion result
semantics. They are not supported companion modes.

## Motion and safety boundary

Development defaults to `PICRAWLER_ACTUATORS_ENABLED=0` and
`PICRAWLER_HOME_ON_START=none`. Do not enable actuators, calibrate servos, play
hardware test sounds, install drivers, change power modes or reboot without an
explicitly approved hardware test. `--body-expressions` requests staged poses;
it does not override the Pi's independent enablement and health gates.

The Pi centralizes speed/battery limits and checks power at each motion boundary.
Forward cycles require a valid ultrasonic read and cannot lower the Pi's reflex
margin. Unknown input blocks relevant motion. Short-lived one-use leases reject
expired/replayed commands and commands from a previous server boot. Unconfirmed
command completion is an unknown outcome, never retried automatically. Stop is latched;
resume must be explicit and does not move the body.

The stock hardware has no assumed IMU, depth, foot contact, joint feedback or
sound direction. Stops take effect between vendor calls/pose stages, not
instantaneously. The sensors do not guarantee collision avoidance or stair/cliff
detection. Keep an accessible physical power cutoff; a software stop is not a
replacement. No autonomous pet chasing. Hardware movement tests use a controlled
area and inanimate targets only.

## Identity and memory

Identity and useful interaction history persist locally under `PET_HOME`
(default `~/.picrawler_pet`). Versioned migrations back up old JSON/SQLite files
before changes. Legacy observations are retained as unverified, generated
narrative is separate from observed facts, and generated traits cannot alter
motion limits. A repeated greeting can change the acknowledgement without
claiming to recognize a particular person.

```bash
brain/.venv/bin/python -m brain.pet.memory_cli inspect
brain/.venv/bin/python -m brain.pet.memory_cli export /tmp/crab-memory.json
# Explicit reset is backed up; identity remains unless --include-identity is used.
brain/.venv/bin/python -m brain.pet.memory_cli reset --confirm
```

See [memory recovery](docs/MEMORY.md), [staged architecture plan](docs/COMPANION_PLAN.md),
[official hardware evidence](docs/HARDWARE_EVIDENCE.md), and
[hardware acceptance checklist](docs/HARDWARE_ACCEPTANCE.md).

Later proposals cover measured local model residency within 8GB, supervised
exploration through a common clearance gate and richer user-led play. They are
not implied by the presence of older world-model/training experiments. Existing
world databases remain untouched and recoverable.

The local [operations dashboard](docs/mission-control.md) presents camera,
companion intent/results, bounded live vitals and explicit safety/health states.
