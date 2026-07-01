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

**JetPack 6.2 torch prerequisites** (the script handles the second automatically):
- System CUDA must be installed — `sudo apt-get install -y nvidia-jetpack` —
  else `import torch` fails with `libcudart.so.12: cannot open shared object file`.
- torch ≥ 2.8 links **cuDSS**, which JetPack 6.2 doesn't ship
  (`libcudss.so.0: cannot open shared object file`). `setup_perception.sh` detects
  this and installs `nvidia-cudss-cu12 --no-deps` (no-deps so it doesn't shadow
  the system CUDA 12.6). If ever needed by hand:
  `brain/.venv/bin/pip install --no-deps nvidia-cudss-cu12`.

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
2. **Voice I/O** — wake word + VAD → whisper.cpp / faster-whisper STT; Piper TTS.
3. **Ollama tool-calling agent loop** — Qwen-family ~3B instruct model; robot
   abilities exposed as tools that call `RobotClient`.
4. **Behavior-tree fallback** — deterministic behavior when the LLM is unavailable.
5. **Learning stack** (all local, staged):
   - **Episodic memory** — SQLite / vector store of interactions, people,
     places, commands, and user corrections the LLM retrieves from (foundation).
   - **Skill library** — learned, named action sequences the LLM reuses.
   - **Outcome self-tuning** — adjust gait/action params from success/failure
     feedback (needs sensor signal).
   - **Offline fine-tune** — periodically fine-tune the small LLM on collected
     experience logs (data collection is cheap; on-device training is
     constrained by 8GB).
6. **Spatial mapping / "know the house"** — visual SLAM or occupancy mapping
   from the camera (optionally a depth sensor / LiDAR — revisit hardware at this
   stage). The map becomes the "places" layer episodic memory attaches to. Runs
   as a loadable/unloadable mode given the RAM budget.
7. **Real custom gait** — replace picrawler's canned `do_action` calls in
   `robot/gait.py` with a coordinate-based gait via `crawler.do_step(...)`,
   reading picrawler's stored calibration offsets. The HTTP protocol, the
   client, and the `GaitEngine` method signatures do **not** change.

**Extension seam:** the future agent loop will emit an *experience record* per
action (command + `CommandResponse` + perception snapshot). Memory, skill,
tuning, and mapping subsystems consume that stream — so they plug in without
reshaping the protocol.
