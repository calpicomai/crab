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

### Robot (Raspberry Pi)

```bash
cd ~/crab                      # repo root, so `import shared` resolves
python3 -m venv robot/.venv
robot/.venv/bin/pip install -r robot/requirements.txt
# robot_hat + picrawler come from the SunFounder installer on the Pi.

# Run directly (real servos):
robot/.venv/bin/python -m robot.server
# ...or bench-test without moving servos:
PICRAWLER_SIMULATE=1 robot/.venv/bin/python -m robot.server
```

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
python3 -m venv brain/.venv
brain/.venv/bin/pip install -r brain/requirements.txt
```

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

> **Safety:** on the first real run, elevate the robot / keep the legs clear.

### Off-hardware dev

Both nodes run on a laptop with no SunFounder hardware: the `GaitEngine`
auto-detects the missing `picrawler` import and drops into **simulate** mode
(logs the action, returns success). Run the server with `PICRAWLER_SIMULATE=1`
and point the test at `--base-url http://localhost:8000`.

## Roadmap (documented, NOT built yet)

1. **Perception** — NanoOWL / YOLO detectors feeding the brain; loadable/unloadable.
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
