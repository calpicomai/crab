# Milestone 1 validation — 2026-10-22

**Passed after the live-feed and WebSocket follow-ups:** 251 focused
unit/integration tests, existing off-body checks, nine deterministic scenarios
and the isolated HTTP/WebSocket simulator smoke. No physical
robot or inference model was used. Hardware acceptance remains pending.

Executed from repository root:

```bash
bash test_sim.sh
brain/.venv/bin/python -m compileall -q brain robot shared tests
bash -n brain/run.sh robot/run.sh sim.sh test_sim.sh
git diff --check
brain/.venv/bin/python -m brain.pet.scenarios --all --summary --output docs/evidence/scenarios.json
brain/.venv/bin/python -m brain.pet.benchmark --iterations 30 --output docs/evidence/coordinator-benchmark.json
brain/.venv/bin/python -m robot.audio
brain/.venv/bin/python -m brain.realtime_benchmark --output docs/evidence/realtime-benchmark.json
brain/.venv/bin/python tests/browser_live_feed.py --output-dir /tmp/crab-websocket-browser
```

The dashboard script and all HTML inline handlers were extracted and parsed with
`node --check`; a malformed play-button handler discovered by that check was
corrected. The follow-up also checks real headless Chromium against an explicit
local simulator: visible telemetry, one camera request at a time, stale timing,
reconnect, JavaScript exceptions and mobile-width overflow. See
[live-feed evidence and limits](live-feed-performance.md). This does not validate
every mobile interaction or physical-camera latency.

The focused suite covers real worker-thread interruption, stop/resume ordering,
old semantic resume cancellation, command correlation/replay/expiry/restart,
nonfinite payloads, initialization health gates and uncertain outcomes, elapsed
state and stillness, ignored/stale/dummy observations, battery caps, audio process
and HTTP errors, reordered/restarted audio, STT/playback overlap, model expiry and
cleanup, migration/backups and restart continuity. The live HTTP simulation also
feeds a dashboard greeting through the actual runtime, confirms acknowledgement,
confirms “good crab, stop” overrides praise, and resumes explicitly.

The companion smoke now requires a ready WebSocket session. Thirteen actual
loopback socket tests cover leases, ownership, independent HTTP stop, heartbeat
expiry, loss during an active command and reconnect without replay. Further
threaded regressions cover completed-but-unconsumed resume results after stop,
old input epochs in transit and Pi restart resetting its epoch. Readiness tests
confirm compatibility checks never open a control connection or request sound.
The repeated Chromium check displays WebSocket health from a labelled fixture,
with no JavaScript exceptions or mobile overflow and at most one camera request
in flight; see [browser evidence](evidence/websocket-browser.json).

[WebSocket event measurements](websocket-transport.md) compare the previous
10 Hz HTTP input worker with the production WebSocket client using 24 events per
path on one explicit simulated server: median **52.965 → 2.273 ms**, p95
**101.256 → 3.294 ms**. This is submission-to-input-callback time on the development
host, not physical reaction time. Status sampling remains 5 Hz. Combined fixture
RSS was 50.96 MiB with no models; Pi/Jetson CPU, shared RAM and inference latency
remain unmeasured.

Scenario fixtures use a virtual clock and inline fake execution for reproducible
traces. They are separate from the threaded concurrency and HTTP tests. The
preserved legacy world-model self-tests include label-interest/chase vocabulary;
that experimental model is not consumed by companion runtime and no animal or
physical target was pursued.

Measured on this **x86_64 development host**, over 270 scripted scenarios:

| Measurement | Result | Scope |
| --- | ---: | --- |
| Peak process RSS | 26.77 MiB | Python coordinator/scenarios only |
| Process CPU | 2.7333 s | Tight benchmark loop, not idle companion utilization |
| Wall time | 2.7336 s | All 270 scenarios |
| Whole-scenario median / p95 | 7.643 / 24.591 ms | Virtual behavior including trace construction |
| Model inference latency | Unmeasured | No models/GPU available |
| Jetson combined 8GB budget | Pending | Must include OS, model server, GPU/shared RAM and speech |

[Scenario evidence](evidence/scenarios.json), [benchmark JSON](evidence/coordinator-benchmark.json),
and [read-only audio diagnostics](evidence/audio-development-host.json) record
provenance and limitations. The host has no ALSA playback/capture utilities or
physical output; the audio report is not a diagnosis of the owner's Pi.

## What is not verified

- Physical HAT revision, installed Pi vendor APIs, battery accuracy, calibration,
  servo initialization, electrical limits, displacement, stalls or stop latency.
- Pi I2S/ALSA routing, service-account permissions, amplifier enablement or audible
  speaker output. Software failures are now observable; the actual silent-speaker
  cause must be established using the [audio acceptance guide](audio-acceptance.md).
- Jetson model loading, inference quality/latency, actual RAM residency, camera
  detector accuracy or microphone transcription. Runtime refuses missing local
  model assets rather than downloading or silently simulating them.
- Collision/stair/cliff safety, precise turning, reverse, pet chasing or unsupervised
  navigation. These are not provided by this milestone.

Sandbox note: ASGI/asyncio worker wakeups and loopback tests required network
permission in this execution environment. With it, tests completed without
external requests; restricted runs can hang on a blocked wake socket. This was
treated as an execution constraint, not a reason to weaken the tests.

Next gate: [explicitly approved staged hardware acceptance](HARDWARE_ACCEPTANCE.md).

## Clock follow-up — 2026-10-26

`bash test_sim.sh --quick` passed 272 tests, off-body checks, nine scenarios and
isolated simulated HTTP/virtual-body checks. The real Chromium check passed with
an injected +30 ms browser clock, stale-data/reconnect assertions, no JavaScript
errors, no mobile overflow and at most one camera request in flight. Compileall,
shell syntax and `git diff --check` passed. See [clock policy, exact validation
limits and owner-controlled disabled-hardware steps](companion-clock.md) and
[browser report](evidence/companion-clock-browser.json).

The complete `bash test_sim.sh` failed the live explicit-resume assertion after
its earlier portions passed. An unchanged `b505086` baseline reproduced the same
resume/control-epoch ordering failure. This follow-up does not claim a green full
smoke or physical acceptance, and does not repair sonar or audio.

## Opt-in camera encoder follow-up — 2026-10-26

The [explicit hardware MJPEG profile](camera-encoding.md) retains software JPEG
by default. Final quick validation passed 284 tests, off-body/nine scenarios and
HTTP/virtual-body. The preceding full 283-test revision also passed live
WebSocket greeting/priority stop/resume. Hardware/API compatibility and physical
throughput remain unverified; tests use fake encoders and explicit simulation.
