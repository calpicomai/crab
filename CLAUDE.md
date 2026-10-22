# Crab implementation guidance

Read `AGENTS.md`, `README.md` and `docs/COMPANION_PLAN.md`. This file supersedes
the earlier contradictory “completed feature” list. Hardware validation is
pending unless a dated acceptance record explicitly demonstrates otherwise.

- Python monorepo: `robot/` on Raspberry Pi 4B, `brain/` on Jetson Orin Nano
  Super 8GB, `shared/` transport-free canonical schemas and paths.
- Pi owns all vendor servo timing, stored calibration and power/kinematic limits.
  Never send servo trajectories from an LLM or across the network.
- Runtime models and personal data stay local. Missing assets/capture/ALSA are
  unavailable; never auto-simulate or download. Simulation requires explicit
  `PICRAWLER_SIMULATE=1`/`PERCEPTION_SIMULATE=1`; brain `--sim` only selects a mock
  mind and does not change the robot's hardware provenance.
- The default pet is `brain.pet.loop` + `Coordinator`: grounded elapsed-time
  interaction episodes, no periodic narration or wandering. Coordinator owns
  intentions, priorities, expiry, cancellation, results and movement ledger.
  Keep status/model/network work off its thread. Speech stop wins over social
  behavior; a newer stop invalidates older resumes and queued interactions.
- Motion commands have one-use Pi-minted leases. Stop bypasses queueing and latches
  at vendor/leg boundaries; explicit resume never starts movement. Unknown command
  completion is not retried. Account initialization and partially executed motion
  honestly. No measured heading/joint position is available.
- `turn(degrees)` is deprecated/rejects nonzero values. Use bounded directional
  vendor cycles only where explicitly appropriate. The simulator labels its
  idealized displacement assumptions. No reverse movement is claimed.
- Body expressions are declarative bounded poses, never hidden movement macros.
  Central Pi speed caps apply to every primitive. Unknown power or required
  clearance inhibits motion. Calibration remains vendor-owned; no offset tables.
- InputHealth traces capture timestamps/provenance through transport,
  interpretation and behavior use. Receipt never refreshes capture age. Failed,
  missing, stale, dummy or untrusted evidence cannot mean free space.
- Local LLM output must validate against `shared.companion.MindOutput`; it sets
  dialogue/attention only. Targets require fresh supported detections; no pursuit.
  Persist observed facts, inferred context and narrative separately. Back up
  versioned files before migrations/resets; keep legacy data recoverable.
- Audio returns honest per-request results. Check subprocess and HTTP failures.
  Simulated/disabled output never claims sound. Session ordering and short-lived
  dispatch leases reject cancelled/restarted audio. Suppress microphone chunks,
  buffered STT and playback tails to avoid responding to the speaker.
- No implied person identity/familiarity without enrollment. No consciousness or
  guilt-based attachment claims. Temperament never changes safety constraints.

Development hardware gates: actuators disabled and startup homing off by default.
No live commands, calibration, OS/driver installation, power changes or reboot
without explicit approval. Stop at the staged hardware acceptance gate. Stock
sensors do not guarantee cliff/collision avoidance; physical cutoff is required.
Legacy wander/agent code is simulation-only pending result-accounting migration.

Preserve easy launchers (`setup.sh`, `robot/run.sh`, `brain/run.sh`, `sim.sh`).
`brain/run.sh check` must remain read-only. Do not automatically load multiple
heavy models or change Jetson power; measure actual peak shared RAM and latency
before adopting a residency policy. Host-only benchmarks are not Jetson results.

Work on a separate branch, keep changes reviewable, run appropriate tests and do
not merge automatically. See `docs/VALIDATION.md` for reproducible checks and
`docs/HARDWARE_ACCEPTANCE.md` for the separate approved physical checks.
