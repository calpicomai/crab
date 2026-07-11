# AGENTS.md — guidance for AI agents working in this repo

Read `CLAUDE.md` first for architecture decisions and constraints that are already
made — follow them, do not substitute alternatives.

## Cursor Cloud specific instructions

Cloud VMs have **no Raspberry Pi or Jetson hardware**. All development and testing
here runs in **simulate mode**:

- Robot: `PICRAWLER_SIMULATE=1` (auto-detected when `picrawler`/`robot_hat` missing)
- Sim world (recommended): `PICRAWLER_SIM_WORLD=1` — ray-cast sonar, 2D map, `/sim` dashboard
- Brain loops: `--sim` on pet/agent; perception uses the **dummy** backend

### Setup

Dependencies install automatically via `.cursor/setup-cloud.sh` (also run manually):

```bash
bash .cursor/setup-cloud.sh
```

Do **not** run `brain/setup_perception.sh`, `brain/setup_agent.sh`, or
`brain/setup_voice.sh` in cloud — they require Jetson CUDA, Ollama, or Piper
binaries.

### Run from repo root

Both venvs expect the repo root as cwd so `import shared` resolves:

- `robot/.venv` — Pi command server + gait (simulate)
- `brain/.venv` — client, wander, pet, agent, perception server

### Quick smoke tests

```bash
# Full stack (blocking — Ctrl+C to stop)
bash sim.sh poles

# Or step by step:
PICRAWLER_SIMULATE=1 PICRAWLER_SIM_WORLD=1 robot/.venv/bin/python -m robot.server &
sleep 2
curl -fsS http://localhost:8000/health
brain/.venv/bin/python -m brain.test_movement --base-url http://localhost:8000
brain/.venv/bin/python -m brain.pet --sim --duration 10 --base-url http://localhost:8000
brain/.venv/bin/python -m brain.agent.loop --sim --max-ticks 3 --base-url http://localhost:8000
```

Open **http://localhost:8000/sim** when the sim-world server is running with
`--dashboard` on the pet/agent loop.

For camera→costmap fusion in sim, run perception with the simblob backend:

```bash
PERCEPTION_BACKENDS=simblob PERCEPTION_CAMERA_URL=http://localhost:8000/camera/stream \
  brain/.venv/bin/python -m brain.perception.server
```

### Secrets

This project is fully local — simulate mode needs **no API keys or cloud secrets**.
