# crab — PiCrawler local-brain robot

An autonomous, voice-interactive quadruped with a **fully local, no-cloud** AI
brain. Two physical nodes on the same LAN:

- **Robot** (Raspberry Pi 4B, 64-bit Raspberry Pi OS) — onboard, real-time.
  Drives 12 servos via the SunFounder Robot HAT and the `robot_hat` /
  `picrawler` libraries. Runs the gait engine and a small FastAPI command
  server. Nothing latency-critical leaves this board.
- **Brain** (NVIDIA Jetson Orin Nano Super, 8GB, JetPack 6) — off-board. Runs
  perception, speech, and the decision-making LLM. Sends high-level commands to
  the robot over the network and receives status back.

> **Status:** Scaffold + **Stage 1 (movement link)** complete. Later stages are
> in the [roadmap](#roadmap) and are **not built yet**.

## Repository layout

```
crab/
├── shared/          # command protocol — defined ONCE, imported by both nodes
│   └── protocol.py
├── robot/           # deploy target: Raspberry Pi (real-time gait + server)
│   ├── server.py    # FastAPI command server
│   ├── gait.py      # GaitEngine — picrawler-backed, stable seam for custom gait
│   ├── config.py
│   ├── requirements.txt
│   └── systemd/picrawler-server.service
└── brain/           # deploy target: Jetson (perception / speech / LLM)
    ├── client.py    # RobotClient — mirrors the shared protocol
    ├── config.py
    ├── test_movement.py
    └── requirements.txt
```

The command protocol lives **only** in `shared/protocol.py` (Pydantic models).
Both the Pi server and the Jetson client import it, so the two nodes cannot
drift. It is transport-free: Stage 1 speaks HTTP, but a WebSocket transport can
be added later by reusing the same models **without changing the protocol**.

## Data flow (full system, target)

```
Mic → wake word + VAD → Whisper STT → local LLM (Ollama, tool-calling)
    → decides actions → robot commands over HTTP → Pi executes gait on servos
    → status returned.  LLM reply → Piper TTS → speaker.
Camera → NanoOWL/YOLO detection → feeds the brain.        (entirely local)
```

Stage 1 implements only the **robot commands over HTTP → gait → status**
segment (the movement link).

## Hard constraints

- Real-time servo/gait timing stays entirely on the Pi. **Never** send
  per-servo timing over the network — commands are high-level intent only.
- No cloud services, no external API calls anywhere. Everything runs on-device.
- The Jetson has only 8GB shared RAM: you cannot hold a large VLM + LLM +
  Whisper + Piper resident at once. Prefer one ~3B tool-calling LLM + small
  Whisper + Piper + lightweight detectors; make heavy models loadable/unloadable.
- Python throughout, with a **separate venv and requirements per node**.

See `CLAUDE.md` for the full conventions.

## Setup & run

Each node uses its own virtual environment (see `CLAUDE.md`). A setup script per
node creates the venv and installs the dependencies in one step — use it rather
than a bare `pip install` (see [Troubleshooting](#troubleshooting)).

### Robot (Raspberry Pi)

```bash
cd ~/crab                      # repo root, so `import shared` resolves
bash robot/setup.sh            # creates robot/.venv and installs deps

# Run directly (real servos):
robot/.venv/bin/python -m robot.server
# ...or bench-test without moving servos:
PICRAWLER_SIMULATE=1 robot/.venv/bin/python -m robot.server
```

On startup the server gently stages into a **standing** pose (instead of
picrawler's splayed power-on pose). Change it with `PICRAWLER_HOME_ON_START`
(`stand` / `sit` / `none`); `stand` assumes every leg is calibrated (an
uncalibrated leg could stall — see [Movement safety](#movement-safety--brownout)):

```bash
PICRAWLER_HOME_ON_START=sit robot/.venv/bin/python -m robot.server   # or none
```

`robot/setup.sh` creates the venv with **`--system-site-packages`** so it can
import SunFounder's `picrawler` / `robot_hat` (their installer puts them in the
system Python — an isolated venv can't see them, and the server would silently
run in simulate mode). The script prints whether the libs are visible. If not,
run SunFounder's installer, then re-run `robot/setup.sh`.

<details><summary>Manual venv setup (equivalent to the script)</summary>

```bash
cd ~/crab
python3 -m venv --system-site-packages robot/.venv   # --system-site-packages: see picrawler/robot_hat
robot/.venv/bin/pip install -r robot/requirements.txt
robot/.venv/bin/python -c "import picrawler, robot_hat; print('hardware libs OK')"
```
</details>

Autostart with systemd (see the header of the unit file for full steps):

```bash
sudo cp robot/systemd/picrawler-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now picrawler-server
curl localhost:8000/health
```

**Calibration** is already handled by the SunFounder calibration tool; those
offsets are stored in picrawler/robot_hat's own config on the Pi and applied
automatically by `do_action`. There is no calibration table in this repo.

### Brain (Jetson)

```bash
cd ~/crab
bash brain/setup.sh            # creates brain/.venv and installs deps
```

<details><summary>Manual venv setup (equivalent to the script)</summary>

```bash
cd ~/crab
python3 -m venv --system-site-packages brain/.venv   # see Jetson's system cv2/torch
brain/.venv/bin/pip install -r brain/requirements.txt
```
</details>

`brain/setup.sh` creates the venv with **`--system-site-packages`** so it can
import the Jetson's system OpenCV (with GStreamer, for the CSI camera) and CUDA
`torch`. Those and `ultralytics` are **not** in a base flash — install them (see
[Perception (Jetson)](#perception-jetson) and `brain/requirements-perception.txt`).
Until they're present, the perception server runs but falls back to synthetic
frames + the dummy detector (`simulate:true`).

Configure how to reach the Pi in `brain/config.py` (defaults to the Pi mDNS
hostname `picrawler.local:8000`). Override without editing code:

```bash
export ROBOT_HOST=192.168.1.42     # if mDNS isn't available, use the Pi's IP
```

Run the Stage 1 movement test (makes the robot take a step across the network):

```bash
cd ~/crab
brain/.venv/bin/python -m brain.test_movement
# get_status → stand → walk(1) → sit, printing each response.
```

> **Safety:** on the first real run, elevate the robot / keep the legs clear, and
> see [Movement safety / brownout](#movement-safety--brownout) below.

### Perception

The robot's "eyes" span both nodes: the **camera is on the Pi** (CSI, captured
with picamera2) and streamed as **MJPEG** over the LAN; the **Jetson** pulls that
stream and runs the detectors (the Pi 4B can't). Two detector backends behind one
`PerceptionEngine`, each **loadable/unloadable** (the 8GB Jetson can't hold both
*and* the future LLM+Whisper+Piper — free one on demand):

- **YOLO** (Ultralytics) — fast, fixed COCO classes. Default backend.
- **NanoOWL** — open-vocabulary, text-prompted ("a person", "a red ball"); the
  future agent loop steers it at runtime via `/prompts`.

**Pi side** — the robot server serves the camera automatically:

```bash
# picamera2 is normally preinstalled on Raspberry Pi OS Bookworm; else:
sudo apt install -y python3-picamera2      # visible to the --system-site-packages venv
robot/.venv/bin/python -m robot.server     # serves /camera/stream (MJPEG) + /camera/frame
curl http://<pi>:8000/camera/frame -o test.jpg    # quick check
```

**Jetson side** — install the detectors (after `brain/setup.sh`). The helper
installs them the **right** way; note the Jetson does **not** need
OpenCV-with-GStreamer (the camera pipeline is on the Pi — it just decodes MJPEG):

```bash
cd ~/crab
bash brain/setup_perception.sh
# torch is the one JetPack-version-specific piece — install NVIDIA's Jetson wheel,
# or let the script do it by passing the jetson-ai-lab index:
TORCH_INDEX_URL=https://pypi.jetson-ai-lab.dev/jp6/cu126 bash brain/setup_perception.sh
```

`torch`/`opencv` deliberately aren't pip requirements — on Jetson the PyPI wheels
are the wrong builds (torch: no CUDA). NanoOWL stays manual (`torch2trt` + a built
TensorRT engine); see `brain/requirements-perception.txt`. The base subset alone
runs the server + simulate path anywhere.

**JetPack 6.2 torch prerequisites** (the script handles cuDSS automatically):
- System CUDA must be installed — `sudo apt-get install -y nvidia-jetpack` —
  else `import torch` fails with `libcudart.so.12: cannot open shared object file`.
- torch ≥ 2.8 links **cuDSS**, which JetPack 6.2 doesn't ship
  (`libcudss.so.0: cannot open shared object file`). `setup_perception.sh`
  installs `nvidia-cudss-cu12 --no-deps` (no-deps so it doesn't shadow the system
  CUDA 12.6) **and** registers its lib dir via `/etc/ld.so.conf.d` + `ldconfig`
  (the wheel alone leaves the `.so` off the loader path). By hand:
  `pip install --no-deps nvidia-cudss-cu12` then add its dir to `ldconfig`.
- **numpy must be < 2** (pinned in `requirements-perception.txt`): the JetPack
  torch/opencv/matplotlib are numpy-1.x builds, so numpy 2 breaks them with
  `_ARRAY_API not found`. If you have numpy 2 in `~/.local`, the venv pin shadows it.

Run the perception server (port 8100) and query it. It reads the camera from the
robot at `brain/config.py`'s `BASE_URL` + `/camera/stream` (override with
`PERCEPTION_CAMERA_URL`):

```bash
brain/.venv/bin/python -m brain.perception.server
curl localhost:8100/health                                     # simulate:false when the Pi stream is live
curl localhost:8100/snapshot                                   # detections for one frame
curl -XPOST localhost:8100/prompts -H 'content-type: application/json' \
     -d '{"prompts":["a person","a ball"]}'                    # steer NanoOWL
curl -XPOST localhost:8100/load   -d '{"backend":"nanoowl"}' -H 'content-type: application/json'
curl -XPOST localhost:8100/unload -d '{"backend":"nanoowl"}' -H 'content-type: application/json'
```

Smoke test (prints detections, writes annotated JPEGs to `perception_out/`):

```bash
brain/.venv/bin/python -m brain.test_perception --frames 5
brain/.venv/bin/python -m brain.test_perception --backend nanoowl --prompts "a person,a ball"
```

Set `PERCEPTION_SIMULATE=1` (Jetson) / `PICRAWLER_SIMULATE=1` (Pi) to run on
synthetic frames + a dummy detector with no camera or models — the whole
Pi→MJPEG→Jetson→detect link runs off-hardware. A `PerceptionSnapshot` (see
`brain/perception/types.py`) is the perception half of the future experience
record.

### Autonomy: wander + avoid (`brain/wander.py` + `brain/costmap.py`)

The robot's autonomous fallback — it moves on its own and steers around
obstacles, no LLM. Instead of reacting to one sensor at a time, it fuses both
senses into a **local occupancy costmap** (`brain/costmap.py:LocalCostmap`) and
steers toward the clearest gap.

**The costmap** is a robot-centered **polar histogram** (Vector Field Histogram
style): the forward arc is split into `COSTMAP_BINS` angular bins over
±`COSTMAP_FOV_DEG` (0° = straight ahead), each holding an obstacle *confidence*
and nearest *range*. Two senses write in where each is strong:

- **ultrasonic** (`get_status().distance_cm`, sensed on the Pi, pins `D2`/`D3`) —
  an accurate range across the forward sonar cone (`SONAR_BEAM_DEG`).
- **camera** (the perception server's `/snapshot`) — a *bearing* per detection
  (from its pixel x-center and `CAMERA_HFOV_DEG`) plus a coarse range from box
  size. Catches wide/off-axis things the narrow sonar beam misses.

Evidence combines by **max, never overwrite** — so a thin pole the sonar beam
misses but the camera sees stays blocked instead of being cleared by a "sonar
sees nothing ahead" reading (the case that let the robot walk into a pole). Four
behaviors make it a *model*, not a snapshot:

- **Rotate-to-scan** — the sonar is fixed forward, so to fill the off-center bins
  the robot periodically turns its body in increments and reads at each
  (`SCAN_EVERY`, `SCAN_RANGE_DEG`, `SCAN_STEP_DEG`).
- **Size-aware inflation** — each obstacle is widened by the angular size the
  robot's own half-width (`FOOTPRINT_RADIUS_CM`) subtends at its range, so it
  never aims at a gap the body won't fit through.
- **Short-memory decay + dead-reckoning** — every cycle confidences fade
  (`COSTMAP_DECAY`); after each turn the histogram is rotated by the *commanded*
  degrees (open-loop, no IMU — which is why memory is short).
- **Gap steering** — pick the widest-enough passable gap nearest straight-ahead:
  forward gap clear → **walk**; gap off to a side → **turn toward it**; boxed in →
  **rotate-to-scan** then re-pick.

**Two control layers (why it stops running into things).** The costmap above is
the *deliberative* layer — it picks a heading. Underneath it is a *fast reflex on
the Pi*: the walk gait is blocking, so without a reflex the robot is **blind for
the whole stride** and noses into things it "should" see. Now every `walk`
carries a `min_clearance_cm`; the Pi reads the ultrasonic **between gait cycles**
and aborts the stride the instant clearance drops below it
(`PICRAWLER_REFLEX_STOP_CM`, or the brain's `WANDER_REFLEX_CM` per walk). The
response comes back with `reflex_stopped=True`, which the brain records as a
close obstacle and steers away from. Because the reflex — not a cautious pause —
provides the safety, motion stays **continuous** (`WANDER_STEP_DELAY_S≈0`) rather
than stop-and-go. This mirrors a real robot/car stack: a slow planner on top, a
fast real-time reflex below.

> **Sensor blind spot (honest):** a single fixed forward sonar + a mono camera
> can't see objects **below both** (a low box, a floor lip). Software only
> partly mitigates — tilt the camera down a little and keep `REFLEX_STOP_CM`
> conservative. Fully fixing it needs a lower/downward rangefinder or a second
> sonar (hardware — ask before assuming it).

> **Scope (honest):** this is a *local, ephemeral* model of free-vs-blocked
> *directions* around the robot (Roomba-class reactive avoidance) — **not** a
> saved metric/3D house map. That would need depth/LiDAR + odometry/IMU this
> robot doesn't have; see the mapping roadmap item.

It's the reactive/behavior-tree fallback from the roadmap and the same
`read sensors → model → decide → act` seam the LLM agent loop plugs into next
(the agent sets high-level intent; this layer keeps it safe in real time).

```bash
# on the Jetson (perception server running for camera avoidance; robot elevated first):
ROBOT_HOST=10.1.50.13 brain/.venv/bin/python -m brain.wander
brain/.venv/bin/python -m brain.wander --no-camera           # ultrasonic only
brain/.venv/bin/python -m brain.wander --no-scan --max-steps 30 --log run.jsonl
brain/.venv/bin/python -m brain.costmap                      # off-robot self-test
```

For a pole and other non-COCO obstacles, the camera **must run NanoOWL** — YOLO's
fixed COCO classes never flag a pole, so on startup the wander loop **loads**
NanoOWL (`POST /load`) and pushes `COSTMAP_OBSTACLE_PROMPTS` (e.g. "a pole", "a
chair leg", "a wall", "furniture") to `/prompts`. If perception is still
YOLO-only afterward it prints a **loud warning** that thin poles won't be seen
(only sonar + the Pi reflex protect against them) — build the NanoOWL TensorRT
engine to close that gap.

Tunables (env or flags): geometry — `COSTMAP_BINS`, `COSTMAP_FOV_DEG`,
`CAMERA_HFOV_DEG`, `SONAR_BEAM_DEG`, `FOOTPRINT_RADIUS_CM` (default 16 — bump if
it clips things while turning); behavior — `COSTMAP_DECAY`, `COSTMAP_BLOCKED_CONF`
(default 0.45 — lower to steer away sooner), `COSTMAP_MAX_RANGE_CM`, `MIN_GAP_DEG`
(0 = derived from footprint), `SCAN_EVERY`/`SCAN_RANGE_DEG`/`SCAN_STEP_DEG`;
reflex — `WANDER_REFLEX_CM` (brain, per-walk stop margin) and
`PICRAWLER_REFLEX_STOP_CM`/`PICRAWLER_REFLEX_ENABLED` (Pi emergency default);
motion — `WANDER_TURN_DEG` (max turn per step), `WANDER_SPEED` (default 100),
`WANDER_STEPS`, `WANDER_STEP_DELAY_S` (default 0 = continuous); camera —
`PERCEPTION_BASE_URL`, `WANDER_USE_CAMERA`, `COSTMAP_OBSTACLE_PROMPTS`. Ultrasonic
pins/pings are env-overridable on the Pi (`PICRAWLER_ULTRASONIC_TRIG`/`_ECHO`/
`_PINGS`, disable with `PICRAWLER_ULTRASONIC_ENABLED=0`). Each step emits an
**experience record** (senses + costmap + decision + response, incl.
`reflex_stopped`) — with `--log`, as JSONL. Ctrl+C stops and sits. Runs
end-to-end in simulate.

### LLM brain: multimodal agent loop (`brain/agent/`)

The deliberative layer on top of the reactive avoidance: a **multimodal LLM that
sees the camera** and drives the robot's abilities as tools. It **free-roams and
narrates** by default (there's no voice input yet) — each tick it looks at one
camera frame, says what it sees and why, and picks ONE high-level action; pass a
goal to steer it.

```bash
# on the Jetson, with a local model server running (see brain/setup_agent.sh):
brain/.venv/bin/python -m brain.agent.loop                     # free-roam + narrate
brain/.venv/bin/python -m brain.agent.loop --goal "find a person"
brain/.venv/bin/python -m brain.agent.loop --sim --max-ticks 5 # canned policy, no model
```

- **Backend-agnostic, local, default llama.cpp.** The agent speaks the
  OpenAI-compatible chat API (`openai` SDK) pointed at a **local** server —
  `llama-server` from llama.cpp by default (`LLM_BASE_URL=http://localhost:8080/v1`).
  Swap to Ollama or any compatible server by changing `LLM_BASE_URL`; no cloud.
- **Multimodal.** It sends the current camera frame (from the robot's
  `/camera/frame`) as an image to a small VLM (default **Qwen2.5-VL-3B**; SmolVLM2
  is a lighter fallback). Set `LLM_MULTIMODAL=0` with a text model to run on the
  perception text summary instead.
- **Two control layers.** The LLM sets *intent* (slow, ~seconds/decision on an
  Orin Nano); the Pi reflex + costmap keep it safe in **real time**. Movement
  tools go through the reflex-protected client, so a bad decision still can't ram
  something. If the LLM is unreachable or errors, the tick **falls back** to one
  reactive costmap step.
- **RAM (8GB).** The VLM does the seeing, so on startup the agent unloads the
  YOLO/NanoOWL detectors (`AGENT_FREE_PERCEPTION_RAM=1`; `--keep-perception` to
  skip). In agent mode the reactive safety leans on sonar + reflex.

Tunables: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_MULTIMODAL`, `AGENT_TICK_S`,
`AGENT_TEMPERATURE`, `AGENT_MAX_TOKENS`, `AGENT_REFLEX_CM`,
`AGENT_FREE_PERCEPTION_RAM`, `AGENT_SIMULATE`, `AGENT_SYSTEM_PROMPT`. Each tick
emits an **experience record** (frame ref + status + goal + narration + action +
response) — with `--log`, as JSONL. Runs end-to-end in simulate (`--sim`).

### Pet mode: a robot pet that grows its own personality (`brain/pet/`)

The "living creature" mode. You **name it once and it grows its own personality
from experience** — it roams, has **moods**, **remembers** what it sees, reacts
with little **gestures**, and over runs becomes a distinct individual.

```bash
brain/.venv/bin/python -m brain.pet --name Nibbles     # meet your pet
brain/.venv/bin/python -m brain.pet --sim --duration 60  # off-GPU, canned inner voice
brain/.venv/bin/python -m brain.pet --no-llm            # pure reactive + mood + memory
```

Two layers run at their own pace so it's **always moving yet smart**:

- **Body** (fast, always on) — a continuous reactive control loop (`brain/costmap.py`
  + the Pi reflex) with **steering hysteresis**, so it moves smoothly and **no
  longer stops and pans side to side**. It's the only thing that commands motion,
  so the pet physically can't ram anything.
- **Mind** (slow, background, optional) — every few seconds it looks through the
  camera and reacts *in character* (its evolving personality + current mood +
  recalled memories), nudging the body's heading and firing a gesture. With a
  local VLM (llama-server) up, that's its real inner voice; without one it uses a
  canned voice, so **it still feels alive today** on mood + memory alone.

What makes it a pet:

- **Personality that develops + persists** (`brain/pet/identity.py`) — a name +
  a random temperament seed, then a **character summary re-condensed from its
  memories** every so often and saved to `PET_HOME` (default `~/.picrawler_pet`).
  Same pet across runs, becoming more itself. (Prompt/summary growth that
  persists — not model fine-tuning; that's a far-later stage.)
- **Moods** (`brain/pet/mood.py`) — curious / excited / playful / cautious /
  startled / bored / sleepy, shifting from what happens (sees a person → excited;
  a close call → startled; nothing for a while → bored → rests). Mood colors both
  its words and its pace.
- **Expressive gestures** (`brain/pet/expressions.py`) — a happy wiggle, a curious
  head-tilt, resting when sleepy — all small, reflex-protected moves.
- **Episodic memory** (`brain/pet/memory.py`) — a local SQLite log of what it saw
  and felt; drives recognition and feeds the personality growth. First piece of
  the roadmap's learning stack.

Honest limits: personality growth is persisted text/tallies, not learned weights;
"where things are" stays loose (no metric map); narration is **text** — giving it
an actual **voice** (Piper TTS) is the next stage and is what will really make it
land. Config: `PET_NAME`, `PET_HOME`, `PET_REFLECT_S`, `PET_EVOLVE_EVERY`,
`PET_HYSTERESIS_TICKS`, plus the shared LLM/costmap/reflex knobs. `wander` remains
the plain reactive fallback and `brain/agent` the goal-driven agent; `pet` is
built on both.

### Custom gait (experimental, tune on hardware)

`walk` picks its gait from `PICRAWLER_GAIT_MODE` on the Pi:

- **`canned`** (default) — picrawler's built-in `do_action('forward')`. Proven; used
  by everything today.
- **`custom`** — plays picrawler's **real forward keyframes** via `crawler.do_step`
  with a tunable **stride scale**. At scale 1.0 it's the stock step (it truly walks);
  raise the scale for a longer stride. (A first attempt that modulated a global +x
  axis just "danced" in place — forward motion on this robot is a per-leg y-sweep,
  so the custom gait is built *from* the proven frames rather than invented.)

Tune it (Pi-local, robot **elevated**, start slow):

```bash
robot/.venv/bin/python -m robot.gait_tune --cycles 3 --speed 40           # scale 1.0 = stock step
PICRAWLER_GAIT_STRIDE_SCALE=1.4 robot/.venv/bin/python -m robot.gait_tune --cycles 3 --speed 60
```

Then set it on the floor and confirm it moves forward. Knob:
`PICRAWLER_GAIT_STRIDE_SCALE` (stride length; push up until it's the longest step
that stays stable). Once dialed, run the server with `PICRAWLER_GAIT_MODE=custom`
(+ your `PICRAWLER_GAIT_STRIDE_SCALE`) and `walk`/wander use it — no protocol/client
change. `turn` stays canned for now.

### Off-hardware dev

Both nodes run on a laptop with no SunFounder hardware: the `GaitEngine`
auto-detects the missing `picrawler` import and drops into **simulate** mode
(logs the action, returns success). Run the server with `PICRAWLER_SIMULATE=1`
and point the test at `--base-url http://localhost:8000`.

### Troubleshooting

- **`error: externally-managed-environment`** (PEP 668, common on Raspberry Pi
  OS Bookworm): you ran `pip install` against the system Python instead of a
  venv. Run the node's `setup.sh` (or the manual venv steps above) and install
  into `robot/.venv` / `brain/.venv`. Do **not** pass `--break-system-packages` —
  it pollutes the system Python and defeats the per-node isolation the project
  relies on.
- **`ModuleNotFoundError: No module named 'picrawler'`** inside `robot/.venv`
  (so `/health` reports `simulate: true` on the real robot): the venv can't see
  the system-installed SunFounder libs. Recreate it **with**
  `--system-site-packages` (re-run `robot/setup.sh`, which now does this), and
  confirm they're installed in the system Python.
- **Perception `/snapshot` returns `simulate:true` / `backends:["dummy"]`** on the
  Jetson: the camera and/or detector aren't available, so the engine fell back.
  Check the server startup log for `CSI camera unavailable` / `could not load
  backend`. Fixes: (a) recreate `brain/.venv` **with** `--system-site-packages`
  (re-run `brain/setup.sh`); (b) install `cv2` with GStreamer and the Jetson
  `torch` wheel + `ultralytics` — see [Perception (Jetson)](#perception-jetson).
  `brain/setup.sh` prints which of `cv2` / `torch` / `ultralytics` are visible.

### Movement safety / brownout

**Symptom:** the Pi resets when the robot moves — your SSH session drops
("Connection reset by peer" / "Broken pipe"), the server process dies, and the
next request times out. Often the robot folds/extends legs oddly first.

**Cause:** driving many servos at once is a big current spike. On the standard
2×18650 Robot HAT, the Pi and the servos share one rail, so the spike sags the
rail and browns out the Pi. It's worse if the cells are low, or if a leg is
mis-calibrated / mis-wired / has a badly seated servo horn and **stalls** (a
stalled servo draws max current continuously).

**What the software now does:** `stand`/`sit` are **staged** — one leg at a time
via `do_single_leg`, at a low speed (`PICRAWLER_STAND_SPEED`, default 40) with a
settle delay (`PICRAWLER_LEG_SETTLE_S`, default 0.2s), so only ~3 servos draw
current at once. The default gait speed is 50. Tune gentler if needed:

```bash
PICRAWLER_STAND_SPEED=30 PICRAWLER_LEG_SETTLE_S=0.35 robot/.venv/bin/python -m robot.server
```

**Isolating a bad leg** — run the Pi-local diagnostic (no network), with the
robot **elevated and legs clear**:

```bash
cd ~/crab
robot/.venv/bin/python -m robot.diagnose --all --speed 30   # steps legs 0→3, pausing
robot/.venv/bin/python -m robot.diagnose --leg 2            # just one leg
```

Watch each leg move to its **standing** position. If a leg drives to a
wrong/extreme angle or buzzes/binds (a stall), **cut power** and fix that leg:
re-run the SunFounder calibration tool, re-seat the servo horn, or check it's
wired to the channel picrawler expects (`PIN_LIST`). Also **fully charge** the
2×18650 cells — low cells are a common brownout cause. Once every leg reaches
standing individually without stalling, `stand` via the server should be stable.

## Roadmap

1. ✅ **Perception** — YOLO + NanoOWL detectors feeding the brain, loadable/
   unloadable, served over HTTP (`brain/perception/`). *Built — see
   [Perception (Jetson)](#perception-jetson).*
2. ✅ **Reactive autonomy** — wander + obstacle avoidance steered by a fused
   ultrasonic + camera **local occupancy costmap** with gap steering
   (`brain/wander.py` + `brain/costmap.py`); the model-free
   `read→model→decide→act` baseline. *Built — see
   [Autonomy: wander + avoid](#autonomy-wander--avoid-brainwanderpy--braincostmappy).*
3. ✅ **Multimodal LLM agent loop** — a small VLM (default Qwen2.5-VL-3B via
   llama.cpp, OpenAI-compatible + swappable) that sees the camera and drives the
   robot's abilities as tools; free-roams + narrates, falls back to the reactive
   costmap when the LLM is unavailable. *Built — see
   [LLM brain: multimodal agent loop](#llm-brain-multimodal-agent-loop-brainagent).*
4. **Voice I/O** — wake word + VAD → whisper.cpp / faster-whisper STT; Piper TTS
   (turns the agent's narration into speech and adds spoken commands).
5. **Learning stack** (all local, staged):
   - **Episodic memory** — 🟡 *started* (`brain/pet/memory.py`: on-device SQLite
     log the pet remembers from and grows its personality on). Next: richer
     recall (vector store) of people, places, and corrections.
   - **Skill library** — learned, named action sequences the LLM reuses.
   - **Outcome self-tuning** — adjust gait/action params from success/failure
     feedback (needs sensor signal).
   - **Offline fine-tune** — periodically fine-tune the small LLM on collected
     experience logs (data collection is cheap; on-device training is
     constrained by 8GB).
6. **Spatial mapping / "know the house"** — a *persistent, metric* map (visual
   SLAM or a global occupancy grid), the "places" layer episodic memory attaches
   to. Distinct from the ephemeral local costmap in item 2: a saved world map
   needs depth/LiDAR + odometry/IMU, so **revisit hardware at this stage**. Runs
   as a loadable/unloadable mode given the RAM budget.
7. **Real custom gait** — replace picrawler's canned `do_action` calls in
   `robot/gait.py` with a coordinate-based gait via `crawler.do_step(...)`,
   reading picrawler's stored calibration offsets. The HTTP protocol, the
   client, and the `GaitEngine` method signatures do **not** change.

**Extension seam:** the future agent loop will emit an *experience record* per
action (command + `CommandResponse` + perception snapshot). Memory, skill,
tuning, and mapping subsystems consume that stream — so they plug in without
reshaping the protocol.
