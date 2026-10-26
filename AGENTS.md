# Agent guidance

Read `CLAUDE.md`, the current implementation and `docs/COMPANION_PLAN.md` before
changing behavior. Prior documentation mixed implemented, simulated and hardware
verified claims; use dated evidence and tests, not those old claims.

This cloud workspace has no Pi/Jetson hardware. All actuator tests must use
explicit simulation or fakes. Missing vendor libraries must never enable
simulation implicitly or synthesize healthy data on a real deployment.

From repository root, existing environments are `robot/.venv` and `brain/.venv`:

```bash
bash test_sim.sh --unit
bash test_sim.sh
bash sim.sh room
brain/.venv/bin/python -m brain.pet.scenarios --all --summary
```

The full smoke test binds an isolated loopback port and uses temporary memories.
In sandboxed execution, asyncio/ASGI and localhost tests may need network
permission even when no external connection is made. Do not weaken tests for
that environment limitation. No API keys/cloud services are needed.

Do not run Jetson setup/model/driver/power scripts in cloud. Do not send commands
to a configured LAN robot. Hardware acceptance, audio playback, calibration,
installation, power changes and reboot require explicit authorization. Preserve
vendor calibration and existing configuration/memory through backups.

Main seams: canonical protocol/health/companion/speech in `shared/`, bounded
motion in `robot/gait.py`, one intention owner in `brain/pet/coordinator.py`,
nonblocking integration in `brain/pet/loop.py`. Legacy navigation is simulation
only. New companion work must not reintroduce hidden motion or perpetual roaming.
