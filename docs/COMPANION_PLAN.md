# Crab companion foundation

Status: milestone 1 plus live-feed/WebSocket follow-ups implemented and software
validated; see [dated validation](VALIDATION.md). No Pi/Jetson hardware validation.
Baseline: branch `work`, inspected 2026-10-22. This plan supersedes contradictory
“done” and roadmap claims in the old README/CLAUDE guidance.

## Evidence before changes

The repository contains runnable code for camera transport, detectors, local
LLM calls, Piper/Whisper transport, SQLite memory, vendor gait dispatch and a
kinematic simulator. Presence of code and simulated success are not evidence of
working hardware. This development machine has no Pi, Jetson, Robot HAT, ALSA
playback device, vendor motion libraries or installed inference models.

All reported defects were reconfirmed in the baseline implementation:

| Finding | Baseline evidence | Milestone response |
| --- | --- | --- |
| Turn magnitude ignored on hardware | `GaitEngine.turn` calls one vendor turn; simulation uses requested degrees | Reject precise-angle requests; expose bounded directional cycles, label simulated angle estimates |
| Blocking gestures lose outcomes | `expressions.express` loops, sleeps, discards replies and exceptions | Declarative single-primitive expression plans, coordinator owns execution and result ledger |
| Stale thoughts/targets | Body reads old heading every tick; target field unused; last target reused without fresh bearing | Expiring validated intentions; attention target requires fresh observed label; no pursuit |
| Pursuit bypasses camera and costmap | Target-ahead path walks based on sonar reflex | No autonomous translation in companion milestone; later exploration must use a common clearance gate |
| Gesture overrides battery cap | `max(60/70/80,speed)` in gestures | Pi owns conservative limits for all motion including poses |
| `wiggle` versus `wag` | Mood/brain emit wiggle; expression registry has wag | One canonical capability vocabulary; migrate alias at input only |
| Speech negation/stop priority | Ordered keyword first-match selects praise before stop | Stop precedence and explicit negation handling |
| Real failures become simulated health | Sensor/camera/audio fallback paths fabricate values | Explicit simulation only; per-input availability, provenance, freshness and failure |
| Audio false success | `aplay` return code ignored; simulation returns True; HTTP result ignored | Bounded diagnostics, subprocess status, structured playback results and self-test |

## Architecture and first coherent milestone

Pi owns vendor motion, calibration, power limits, interruption at vendor/leg
boundaries, sensor capture and speaker playback. Jetson owns interpretation,
local speech and one behavior coordinator. One local WebSocket carries status,
input events, bounded primitives and short-lived command leases, never servo
trajectories. HTTP remains the independent stop/readiness/media path; browser
telemetry uses SSE. A server boot identity,
command IDs and outcomes distinguish restart, rejected commands and uncertain
network completion. Stop is independent of queued social activity.
Per-boot stop epochs preserve each input's original authority: newer status
cannot turn an old resume into a post-stop instruction. Queues are bounded and
reconnect requires explicit recovery. See the [transport contract](websocket-transport.md).

Each captured input carries provenance, timestamps, availability and failure.
The coordinator explicitly records whether it used an input and why it ignored
one. Unknown evidence cannot authorize movement. A shared movement result
records completed vendor boundaries without claiming measured odometry.

The default companion remains nearby. Grounded engagement produces a timed
notice → orient attention → acknowledge → listen → respond → settle episode.
Short local sounds and sparse staged pose changes provide expression; no
random wandering, compulsory narration, locomotor gestures or pet chasing.
Temperament affects hesitation, episode tempo and persistence; physical distance
preferences remain descriptive until supervised movement is validated. Quiet
companionship and interaction are available; supervised exploration is a later
proposal, not silently enabled by a mode flag.

LLM work is optional and event-driven. A single outstanding local request may
produce validated dialogue/high-level intentions; late results expire and are
invalidated by interruption. It never owns safety. SQLite stores observed facts,
inferences and generated narrative separately; existing identity/memory files
are backed up for explicit migrations/reset and remain inspectable/exportable.

## Staged delivery and acceptance

1. **Stationary companion foundation (this PR).** Health/readiness, honest
   bounded motion/audio, stop/expiry/reconnect handling, timed interaction,
   provenance-aware memory, dashboard explanations, deterministic scenarios.
   Validate with unit + HTTP integration + simulator, then stop at hardware gate.
   Reviewable follow-ups add latest-only feeds and the local WebSocket channel;
   their host measurements are recorded separately from physical acceptance.
2. **Hardware acceptance PR.** Inventory exact HAT revision and installed vendor
   versions; verify read-only sensors and ALSA/service permissions; approved
   direct sound and endpoint tests; only then approved supported poses/one-cycle
   gait with accessible power cutoff. Record power draw, latency, slip and stop
   boundaries. No OS/driver changes without separate approval.
3. **Local inference budget PR.** Measure Pi/Jetson CPU/RSS, shared RAM and p50/p95
   STT/TTS/LLM/detection latency on the actual 8GB Jetson; choose a residency
   schedule with explicit unloading and headroom. Add enrolled preferences and
   useful episodic recall without inferring identity from person labels.
4. **Supervised exploration proposal/PR.** Controlled inanimate targets only,
   conservative common sonar/camera/costmap gate, expiry after any unmeasured
   rotation, bounded investigation and return-to-stillness. Stock sensing does
   not guarantee stair/cliff detection or collision avoidance. No reverse or
   precise turn promise without new evidence.
5. **Richer play proposal/PR.** Small approved pose/sound vocabulary, user-led
   games and measurable repetition/engagement improvements. No guilt-based
   dependency, claims of consciousness or safety changes from generated traits.

Each stage is reviewable and independently runnable. No automatic merge. Runtime
inference and personal data remain local. Existing training/world files are
preserved but are not trusted as identity, metric position or live clearance.
