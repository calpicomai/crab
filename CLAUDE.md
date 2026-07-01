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
  root so `import shared` resolves.

## Gait seam (important)

`robot/gait.py` exposes `GaitEngine` with fixed method signatures
(`stand`/`sit`/`walk`/`turn`/`get_status`). Stage 1 backs them with picrawler's
canned `do_action`. The **real custom, coordinate-based gait** will replace those
bodies (`do_action` → `crawler.do_step(coords, speed)`) **without changing the
signatures, the HTTP protocol, or the client.** Canned calls are marked
`# TODO: real custom gait`.

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

## Workflow: staged, STOP between stages

Do NOT build the whole system at once. Each stage must be independently
runnable, and you STOP after each so the user can test on real hardware.

- **Done:** Scaffold + Stage 1 (movement link: Pi server, Jetson client,
  one-shot test, systemd unit).
- **Not built yet (roadmap in README):** perception, voice I/O, Ollama agent
  loop, behavior-tree fallback, the learning stack (episodic memory → skill
  library → outcome self-tuning → offline fine-tune), spatial mapping/SLAM, and
  the real custom gait.

## Ask before assuming

- The Pi's IP/hostname on the LAN → it lives in `brain/config.py`
  (`picrawler.local` by default); never hardcode it elsewhere.
- Whether working gait functions exist before wiring real movement.

## Extension seam for learning

The future agent loop will emit an *experience record* per action (command +
`CommandResponse` + perception snapshot). Memory, skill, tuning, and mapping
subsystems consume that stream — design them to plug into that seam rather than
reshaping the protocol.
