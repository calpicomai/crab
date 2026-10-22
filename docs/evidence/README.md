# Software evidence (2026-10-22)

These artifacts exercise the companion coordinator with explicit simulated inputs
and a virtual clock on an x86_64 development VM. They are not hardware acceptance.
No actuator, physical speaker, microphone, detector model, or language model was
used to produce them.

Reproduce from the repository root:

```bash
brain/.venv/bin/python -m brain.pet.scenarios --all --summary --output docs/evidence/scenarios.json
brain/.venv/bin/python -m brain.pet.benchmark --iterations 30 --output docs/evidence/coordinator-benchmark.json
bash test_sim.sh
```

Omit `--summary` to inspect every observation, intention, reason, and result at
each virtual time step. Scenario fixture operations execute inline for stable
traces. Separate coordinator and control-race tests exercise actual worker-thread
interruption; scenario traces alone do not establish concurrency correctness.

The benchmark's latency is for an entire scripted scenario (roughly 13 seconds
of virtual behavior), including JSON-ready snapshots. Its CPU percentage describes
a tight benchmark loop, not the sleeping runtime's CPU utilization. RSS measures
only this Python process. Model inference latency is explicitly null. The Jetson's
8GB shared-memory budget remains unverified until its installed model combination
is measured, including the model server, STT, TTS, perception, OS and GPU allocations.

The full smoke runner uses a temporary identity/memory directory and an explicit
simulation server bound to an ephemeral loopback port. Its movement test checks
returned completion/failed outcomes and rejects precise-turn claims. It does not
verify distance traveled, mechanical stability, audible sound or collision safety.
