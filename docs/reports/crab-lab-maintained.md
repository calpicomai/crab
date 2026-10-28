# Maintained opt-in Lab acceptance — 2026-10-28

The original local work is preserved and rebased onto merged ROS main7deeb0c.
Normal Pi/brain launchers still do not import/start Lab or install its optional
dependencies. Lab has no physical adapter, capture path or ROS command route.

Local release checks:25real MuJoCo/API/episode/evaluation tests PASS, six fixed
scenario/seed evaluations PASS, dependency/syntax/whitespace checks PASS. The
owned-server Chromium suite PASS on desktop/narrow layouts with no JS errors,
including actual physics movement, pause/step/reset, dropout inhibition,
disconnect/reconnect and a separately synthetic error-view fixture. The test
owns and terminates its localhost subprocess rather than requiring a running
configured service. Independent review reran25tests and found no new blockers.

New maintenance gates: isolated Linux/Windows physics/API/evaluation CI and an
independent Linux browser CI job; explicit direct dependency pins; source and
runtime hashes; bounded research-evaluation exit status/fingerprint; exact
controller-input/target/reason evidence; atomic failed-reset behavior. Pending
CI results must be read on the final PR head before claiming platform acceptance.
Windows CI is software acceptance on its hosted runner, not the owner's PC/GPU.

The model remains provisional. Self-collision is explicitly disabled, not
validated; masses/inertias/axes/limits/friction/servo response remain estimates.
The controller remains procedural open-loop with sonar inhibition, without
learned policy, balance feedback, navigation or recovery. Evaluation checks
sampled decision records, not every physics step or an exact replay trace.
No training/recording or physical commissioning acceptance is claimed.

The next commissioning increments cover correlated input outcomes, robust
resource/signal cleanup and independent progress STOP, read-only platform/profile
diagnostics, and a gated CLI commissioning handoff. They do not authorize device
installation, storage repair, actuation, real recordings or paid compute.
