# AGENTS.md — guidance for AI agents working in this repo

Read `CLAUDE.md` first for architecture decisions and constraints that are already
made — follow them, do not substitute alternatives.

## Cursor Cloud specific instructions

Cloud VMs have **no Raspberry Pi or Jetson hardware**. All development and testing
here runs in **simulate mode**:

- **Virtual body (`SimWorld`)** — enable with `PICRAWLER_SIM_WORLD=1`. A 2D room
  with pose, obstacles, ray-cast sonar, and a rendered camera. Walk/turn move the
  robot in the world so wander/pet/agent can navigate for real (kinematic, not
  servo physics). Live dashboard at **http://localhost:8000/sim**.
- Robot: `PICRAWLER_SIMULATE=1` (auto-detected when `picrawler`/`robot_hat` missing)
- Brain loops: `--sim` on pet/agent; perception uses the **dummy** backend (or
  **`simblob`** when pointed at the sim-world camera stream)

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

### Automated smoke test (CI / cloud)

Run the non-interactive test suite after setup — exits 0 on success:

```bash
bash test_sim.sh          # full check (~30s)
bash test_sim.sh --quick  # skip pet/agent loops
```

This is also run in GitHub Actions (`.github/workflows/sim-test.yml`) on every
PR. It covers costmap self-test, dummy perception, SimWorld virtual-body motion,
movement link, and canned pet/agent loops.

### Interactive emulator (virtual body)

`sim.sh` starts the **SimWorld** virtual body + pet in one command:

```bash
bash sim.sh poles            # scenarios: poles / room / corridor / slalom
# → http://localhost:8000/sim  (click map to drop obstacles)
```

Scenarios place poles, walls, and boxes; the pet's body loop navigates with the
same costmap + reflex stack as on hardware.

Or step by step:

```bash
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
