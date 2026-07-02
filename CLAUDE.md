# CLAUDE.md — conventions for the crab (PiCrawler local-brain) monorepo

Read this before working in this repo. It captures decisions that are **already
made** — follow them, do not re-litigate or substitute alternatives.

## What we're building

An autonomous, voice-interactive quadruped with a fully local, no-cloud AI
brain. Two physical nodes on the same LAN:

- **Robot** (Raspberry Pi 4B): real-time servo/gait + a small command server.
- **Brain** (Jetson Orin Nano Super, 8GB): perception, speech, decision LLM.

## Architecture & tech decisions (use these; do NOT substitute)

- **Monorepo, two deploy targets**: `robot/` (Pi) and `brain/` (Jetson), plus
  `shared/` for the command protocol.
- **Protocol defined ONCE** in `shared/protocol.py` as Pydantic models. Both the
  Pi server and the Jetson client import it so they cannot drift. Do not
  duplicate command/response shapes anywhere else.
- **Transport**: HTTP via FastAPI to start. Keep `shared/protocol.py`
  transport-free so a WebSocket transport can be added later **without changing
  the protocol**. Paths live in `shared.ACTION_PATHS`; do not hardcode path
  strings on either side.
- **LLM** (later stage): Ollama serving a Qwen-family ~3B instruct model with
  tool calling. Robot abilities are exposed as tools that call `RobotClient`.
- **STT** (later): whisper.cpp or faster-whisper. **TTS**: Piper.
  **Perception**: NanoOWL / YOLO.
- **Servo control**: SunFounder `robot_hat` / `picrawler`.

## Hard constraints

- **Real-time servo/gait timing stays entirely on the Pi.** NEVER send per-servo
  timing over the network. Network commands are high-level intent only
  (walk N steps, turn D degrees, stand, sit, get_status).
- **No cloud, no external API calls anywhere.** Everything runs on-device.
- **8GB shared RAM on the Jetson.** You cannot hold a large VLM + LLM + Whisper +
  Piper resident at once. Prefer one ~3B tool-calling LLM + small Whisper +
  Piper + lightweight detectors, and make heavy models loadable/unloadable
  rather than all-always-on.
- **Python throughout.** Separate `requirements.txt` and venv per node (the Pi's
  deps are minimal; the Jetson's assume CUDA/JetPack). Run both from the repo
  root so `import shared` resolves. **Launchers:** `setup.sh` only builds venvs;
  `robot/run.sh` (Pi) and `brain/run.sh` (Jetson, interactive menu + saved
  `crab.env` prefs) actually run things, and `sim.sh` is the no-hardware button.
  Keep those the easy one-command entry points.

## Gait seam (important)

`robot/gait.py` exposes `GaitEngine` with fixed method signatures
(`stand`/`sit`/`walk`/`turn`/`test_leg`/`get_status`). `stand`/`sit` are
**staged**: one leg at a time via `do_single_leg` to picrawler's stand/sit
coordinates, at `config.STAND_SPEED` with `config.LEG_SETTLE_S` between legs — a
power-safety measure (all-12-at-once browns out the shared Robot HAT rail).

`walk` dispatches on `config.GAIT_MODE`:
- **`canned`** (default, proven): picrawler's built-in `do_action('forward')`.
- **`custom`**: `_custom_walk` plays picrawler's **real forward keyframes**
  (`FORWARD_FRAMES`, the exact do_step sequence that translates the body) via
  `crawler.do_step`, with a tunable **stride scale** (`GAIT_STRIDE_SCALE`) that
  amplifies each leg's x/y offset from the stand neutral (z/lift untouched);
  scale 1.0 reproduces the stock step, >1.0 lengthens it. Beware: a v1 that
  modulated a global +x just danced in place — forward motion here is a per-leg
  **y-sweep**, so the custom gait is built *from* the proven frames, not invented.
  Tune on hardware via `robot/gait_tune.py` (Pi-local, elevated) + `PICRAWLER_GAIT_*`,
  then flip the default. `turn` is still canned. `walk(steps, speed, mode=...)`
  takes an optional mode override; the HTTP protocol/`WalkCommand` are unchanged.

## Movement safety

Never drive all 12 servos at once — the current spike browns out the Pi on the
shared 2×18650 rail. Keep motion staged/slow. `test_leg(leg, speed)` +
`robot/diagnose.py` (Pi-local, no network) move one leg at a time to isolate a
mis-calibrated/mis-wired/stalling leg. See the README "Movement safety /
brownout" section.

On startup the server homes to `config.HOME_ON_START` (`stand`/`sit`/`none`,
default `stand`) via the same staged motion, so it doesn't sit in picrawler's
splayed power-on pose. Homing runs from the app **lifespan** hook (not `main()`)
so it also fires under the systemd unit (`uvicorn robot.server:app`).

## Calibration convention

Calibration is done with the SunFounder calibration tool; offsets are stored in
picrawler/robot_hat's own config on the Pi and applied automatically by
`do_action`. **Do not add a calibration table to this repo.** When the custom
gait needs raw/coordinate control, read picrawler's stored offsets — do not
duplicate them.

## Simulate mode

If `picrawler` / `robot_hat` are not importable (dev laptop, CI), `GaitEngine`
auto-enables `simulate`: it logs the intended action and returns success, so the
whole robot↔brain link runs off-hardware. Force it anywhere with
`PICRAWLER_SIMULATE=1`. `RobotStatus.simulate` reports which mode is active.

**Whole-robot sim world (`robot/simworld.py`).** That default simulate is shallow
(flat 80 cm sonar, unrelated camera). Opt in with `PICRAWLER_SIM_WORLD=1` to back
the gait/sonar/camera with a 2D `SimWorld` (pose + obstacles): walk/turn move a
virtual robot, the sonar **ray-casts** real clearances, the camera renders a
first-person view, so the whole brain runs against a real environment. A live
**dashboard** is served at `/sim` (top-down map + camera + costmap + the pet's
mood/gesture/character/speech), fed by brain telemetry pushed via `POST /sim/brain`
(`brain/dashboard.py`, enabled with `--dashboard`); it's interactive (click to
drop obstacles, pause/reset/scenario). The `simblob` perception backend detects
the rendered obstacle boxes for the camera→costmap path. It's **kinematic /
behavior-level** (idealized odometry, no servo physics — a physics/URDF sim is a
far-later option). The `/sim*` endpoints are **dev-only**, NOT part of the
robot↔brain command protocol in `shared/`.

## Perception (camera on the Pi, detection on the Jetson)

The **camera is on the robot (Pi)** — the Pi 4B can't run the detectors, so it
captures and streams; the Jetson pulls the stream and detects.

- **Pi** (`robot/camera.py` + `robot/server.py`): `PiCamera` captures via
  picamera2 and the server exposes `CAMERA_STREAM_PATH` (MJPEG,
  multipart/x-mixed-replace) + `CAMERA_FRAME_PATH` (single JPEG). No picamera2
  (dev/CI) → synthetic JPEG frames, so the video link runs off-hardware.
- **Jetson** (`brain/perception/`): `PerceptionEngine` owns an `MjpegCamera` that
  reads the Pi's stream (default `brain/config.BASE_URL + CAMERA_STREAM_PATH`,
  override `PERCEPTION_CAMERA_URL`), decoding JPEG→numpy with Pillow — **no
  OpenCV/GStreamer needed on the Jetson**. A registry of loadable detector
  **backends** behind a `DetectorBackend` ABC — `yolo` (fixed COCO), `nanoowl`
  (open-vocab, prompt-steered), `dummy` (off-hardware/CI) — lazily import heavy
  libs in `load()` and free them in `unload()` for the RAM budget. `detect()`
  fuses every loaded backend into a `PerceptionSnapshot`. Served over HTTP
  (`perception/server.py`, port 8100).

`simulate` tracks the **camera/frame** state (synthetic or forced via
`PERCEPTION_SIMULATE`/`PICRAWLER_SIMULATE`); the **detector** state is the
separate `backends` list (dummy vs yolo/nanoowl) — a real camera with only the
dummy detector reports `simulate:false, backends:["dummy"]`. `PerceptionSnapshot`
lives in `brain/perception/types.py` (brain-internal, **not** `shared/`; only the
camera path constants are shared) and is the perception half of the
experience-record seam.

**Never fuse fabricated perception into navigation.** `LocalCostmap.integrate_camera`
skips a snapshot with `simulate:true` and drops any detection whose `source ==
"dummy"`. The `DummyBackend` emits a *constant* centered "person" every frame; on a
rig with no real detector, fusing it permanently blocks the forward arc so the
robot only ever turns — the "spins in circles" bug. A real camera + real detector
(`yolo`/`nanoowl`) and the sim-world `simblob` backend (`simulate:false`) still fuse
normally; a dummy/simulate rig just navigates on sonar + reflex.

## Autonomy (wander/avoid via local costmap) & ultrasonic

The **ultrasonic sensor is on the Pi** (`robot/sensors.py:DistanceSensor`, via
`robot_hat` Ultrasonic, default pins trig=`D2`/echo=`D3`, env-overridable). Its
reading rides on `RobotStatus.distance_cm` (populated in the server's `/status`
handler), so the brain gets clearance without a new endpoint. No robot_hat →
simulate (synthetic clearance).

`brain/wander.py` is the autonomous fallback (reactive / behavior-tree from the
roadmap). It no longer OR's the two senses — it fuses them into a
**`brain/costmap.py:LocalCostmap`** and steers by it. The costmap is a
robot-centered **polar occupancy histogram** (VFH): bins over ±`COSTMAP_FOV_DEG`,
ultrasonic writes accurate range at the forward cone, camera writes a bearing +
coarse range per detection; evidence combines by **max, never overwrite** (so a
camera-seen pole the sonar beam misses isn't cleared by a "sonar sees nothing"
reading). Size-aware inflation (`FOOTPRINT_RADIUS_CM`), short-memory decay +
open-loop dead-reckoning (rotate the histogram by the *commanded* turn — no IMU),
rotate-to-scan (fixed sonar), and gap steering pick the clearest wide-enough
heading. On startup wander pushes `COSTMAP_OBSTACLE_PROMPTS` to perception
`/prompts` when NanoOWL is loaded so the camera flags non-COCO obstacles.

**Two control layers.** The costmap is the *deliberative* layer (picks a
heading); underneath is a *fast reflex on the Pi*. Because `GaitEngine.walk` is
blocking, the robot is blind for a whole stride — so the walk now checks forward
clearance **between gait cycles** (`GaitEngine.clearance_fn`, injected from the
ultrasonic in `robot/server.py`) and aborts early when it drops below
`config.REFLEX_STOP_CM` (or `WalkCommand.min_clearance_cm`). The response carries
`RobotStatus.reflex_stopped`; wander records that as a close obstacle and steers
away. This is why motion is continuous, not stop-and-go. Real-time timing/reflex
stays ON THE PI (constraint intact); the network still carries only intent + an
optional safety margin. Known blind spot: objects below both sensors (low box) —
mitigate with camera down-tilt + conservative reflex; a real fix is hardware
(lower/second rangefinder — ask first). Wander also **loads** NanoOWL on startup
(not just prompts) so the camera can actually see poles, warning if it can't.

**Scope discipline:** this is a *local, ephemeral* free-vs-blocked-direction model
(Roomba-class), **not** a persistent metric/3D house map — that needs
depth/LiDAR + odometry/IMU this robot lacks (a later roadmap stage; ask before
assuming hardware). Don't reshape it into a world map.

It's still the `read sensors → model → decide → act` seam the LLM agent loop
plugs into (agent decides; wander is the fallback). Each step emits an
**experience record** (senses + costmap + decision + response; JSONL via
`--log`), and the costmap is a natural consumer/producer of that seam.

## LLM brain (`brain/agent/`) — deliberative layer

The agent loop is the *deliberative* top layer over the reactive avoidance. A
**multimodal LLM** (default **Qwen2.5-VL-3B** served by llama.cpp) sees the
robot's camera frame each tick and calls the robot's abilities as **OpenAI-style
tools** (`walk`/`turn`/`stand`/`sit`/`get_status`; `test_leg` is excluded). It
**free-roams + narrates** by default (no voice yet), with an optional `--goal`.

- **Backend-agnostic, local, no cloud.** It speaks the OpenAI-compatible chat API
  (`openai` SDK) at `LLM_BASE_URL` — a local `llama-server` by default; swap to
  Ollama/etc. by config, no code change. `LLM_MULTIMODAL=0` + a text model uses a
  scene-text summary instead of the image.
- **Two layers / safety.** The LLM sets slow intent; the **Pi reflex + costmap**
  own real-time safety. Movement tools go through the reflex-protected
  `RobotClient`, and if the LLM is unreachable/errors the tick falls back to one
  reactive `LocalCostmap` step. Never rely on the LLM for collision avoidance.
- **RAM (8GB):** the VLM does the seeing, so the agent unloads YOLO/NanoOWL on
  startup (`AGENT_FREE_PERCEPTION_RAM`). `AGENT_SIMULATE`/`--sim` runs a canned
  policy so the whole loop is testable off-GPU.
- Same **experience-record** seam (`--log` JSONL) as wander. `brain/agent/`:
  `config.py`, `tools.py`, `llm.py` (real + mock brain), `loop.py`.

## Pet mode (`brain/pet/`) — the "living creature"

The pet is the capstone autonomous mode: **you name it once and it grows its own
personality from experience.** Two threads: a fast **body** (continuous reactive
costmap+reflex control loop with steering **hysteresis** — the fix for the
stop-and-pan; it is the ONLY thing that commands motion) and a slow **mind** (an
in-character (V)LM every few seconds that nudges heading + gesture, else a canned
voice). The body also has an **anti-spin escape** (`PET_ANTISPIN_TICKS`): after
too many consecutive turn-only cycles (boxed in, or a mis-sensing forward sonar)
it forces one reflex-protected probe step — the Pi reflex aborts the stride if
it's truly blocked, so a genuinely stuck pet can't circle forever. Design rules
to preserve:

- **Personality is persisted + emergent, not hardcoded.** `identity.py` stores a
  name + random temperament seed + a `character` self-summary that the brain
  **re-condenses from memory** every `PET_EVOLVE_EVERY` reflections and saves to
  `PET_HOME`. Same pet across runs. This is prompt/summary+tally growth, NOT
  weight fine-tuning (a far-later stage) — don't overstate it.
- **Memory is the learning-stack foothold** (`memory.py`, SQLite, roadmap 5a).
  Local, no cloud. Feeds recognition + the character growth.
- **Mood** (`mood.py`) is event-driven and owned by the body thread (single
  writer); the mind only nudges via `mood_hint`. A fresh observation counts as
  novelty (`saw_new`) so a lively dog doesn't get bored between slow reflections.
- **Dog-like emoting** (`expressions.py`): it must *emote*, not just walk — a
  **signature** move on every mood change + smaller **idle** fidgets sprinkled in
  (`PET_EMOTE_CHANCE`). Gestures (wag/spin/playbow/perk/sniff/cower/pounce/…) are
  built from the existing abilities so all are reflex-protected; locomotor ones
  are used as reactions, in-place ones as idle fidgets so they don't fight nav.
- **Voice, both ways** — mic + speaker on the **Pi**, STT/TTS compute on the
  **Jetson** (same split as the camera). Out: `voice.py` Piper TTS synthesized on
  the Jetson, `PET_AUDIO_SINK=pi` POSTs the WAV to the robot's `/audio/play`. In:
  `robot/audio.py` streams the Pi mic (`/audio/stream`), `brain/hearing.py` runs
  faster-whisper, `brain/pet/commands.py` maps phrases → actions (works with no
  VLM); free-form speech goes to the mind via `shared.heard`. Paths are in
  `shared/protocol.py` (`AUDIO_STREAM_PATH`/`AUDIO_PLAY_PATH`), like the camera.
  All optional/degradable (no ALSA/piper/whisper → text-only).
- Reuses `costmap.py`, the reflex-protected `client`, and `brain/agent` LLM
  backend/config. Works today with `--sim`/`--no-llm`/no-voice (no GPU/model/audio).

## World model (`brain/pet/worldmodel.py`) — intentional behavior + chasing

The substrate that makes the pet feel *intentional* rather than twitchy, grown from
what it sees + does (SQLite, mirrors `memory.py`'s single-connection pattern; the
**body thread** is the sole writer — it owns the snapshot each tick — and publishes a
text `world_summary` into `_Shared` for the mind to read, so the DB is single-thread).
Three learned parts: **objects** (label/seen/bearing/range/valence — recognition +
novelty), **places** (a *semantic fingerprint* = the set of visible labels, matched by
Jaccard — "I know this spot" WITHOUT metric SLAM, which the sensors can't support), and
**action→outcome** tallies (`record`/`predict`, Laplace-smoothed frequency stats — mild
foresight, NOT a neural predictor).

- **Targets / "chase cats".** `salient_target(snapshot)` picks the most interesting
  visible thing, interest = per-label weight (`PET_CHASE_LABELS` e.g. cat/dog stay
  exciting even when familiar; `PET_INTEREST_LABELS` fade with familiarity → boredom).
  This is a NEW label-aware channel: detections used to fuse into the costmap **only as
  obstacles** — now they can also become a goal to steer toward. The body sets a *goal
  heading* from the target's bearing and, when it's roughly ahead, walks trusting the Pi
  **reflex** (not the costmap) for safety, so the cat it's chasing isn't treated as an
  obstacle to stop for. Reflex + sonar still prevent collisions.
- **Purposeful, de-twitched motion** (the "it just wiggles" fix): multi-stride walks
  scaled by `mood.explore_bias` (`PET_WALK_STEPS_*`), an **EMA-smoothed** desired heading
  (`PET_HEADING_SMOOTH`) + a forward **deadband** (`PET_FORWARD_DEADBAND_DEG`) so tiny
  biases don't cause micro-turns, and a **fidget diet** — idle gestures only when
  genuinely bored/sleepy; `perk` at a newly-noticed thing, `pounce` when closing in.
- **LLM-ready.** `worldmodel.summary()` + the current target feed the VLM system prompt
  (`identity.persona_prompt(mood, memory_summary, world_summary)`), and `PetThought` has a
  `target` field so a VLM can *choose* what to chase; `MockPetBrain` gets excited about a
  target too, so **canned mode chases cats** with no model. Honest scope unchanged: this
  is structured + learned, not a trained world model or literal consciousness.

## Workflow: staged, STOP between stages

Do NOT build the whole system at once. Each stage must be independently
runnable, and you STOP after each so the user can test on real hardware.

- **Done:** Scaffold + Stage 1 (movement link); movement-safety (staged stand/
  sit, per-leg diagnostic, home-on-startup); **Perception** (`brain/perception/`:
  CSI camera + YOLO/NanoOWL fused, loadable/unloadable, HTTP server + test);
  **ultrasonic sensor + reactive wander/avoid** (`robot/sensors.py`,
  `brain/wander.py`); **local occupancy costmap + gap steering**
  (`brain/costmap.py`, fusing ultrasonic + camera); **continuous avoidance**
  (fast Pi-side reflex between gait cycles + costmap; `GaitEngine.clearance_fn`);
  **multimodal LLM agent loop** (`brain/agent/`: a VLM sees the camera + calls
  robot abilities as tools, free-roam + narrate, reactive fallback);
  **pet mode** (`brain/pet/`: continuous reactive body + in-character mind, moods,
  gestures, persistent+evolving personality, episodic memory — the learning
  stack's first foothold).
- **Not built yet (roadmap in README):** voice I/O (next), the rest of the
  learning stack (richer memory → skill library → outcome self-tuning → offline
  fine-tune), spatial mapping/SLAM, and the real custom gait.

## Ask before assuming

- The Pi's IP/hostname on the LAN → it lives in `brain/config.py`
  (`picrawler.local` by default); never hardcode it elsewhere.
- Whether working gait functions exist before wiring real movement.

## Extension seam for learning

The future agent loop will emit an *experience record* per action (command +
`CommandResponse` + perception snapshot). Memory, skill, tuning, and mapping
subsystems consume that stream — design them to plug into that seam rather than
reshaping the protocol.
