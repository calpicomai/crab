# Dashboard recovery and systems review

Base: main `88df7b3f8792524e7e025c610e6854064d3118fd`.
Branch: `fix/dashboard-startup-diagnostics`. Source tree was clean before this
work. Scope: code, regression tests and design/backlog. Publication is a draft PR;
deployment, device operation and settings changes remain outside this work.
Hardware limits/defaults stay.

## Confirmed evidence

- Owner sees HTTP200 for `/sim/state`, `/sim/events`, `/camera/frame` and repeated
  Jetson telemetry POST200, yet the page remains at initial UNKNOWN labels.
- Local actual uvicorn + Chromium test (native EventSource, real HTTP/SSE) with
  actuators/audio/physical sensors disabled and explicit synthetic camera accepts
  null/unavailable physical-mode metrics and renders real/disabled provenance.
- The same test with injected +300ms browser skew receives five real SSE events
  and decodes960x720, yet `clockAnchor`/`latestState` both remain null, source stays
  UNKNOWN and preview FPS blank. No browser exceptions. This reproduces the UI
  mechanism, not the owner's actual clock offset or latency.
- `probeClock` uses the full diagnostic snapshot, rejects bad timing and hides the
  reason. `connectState` drops every status before assignment without calibration.
  `paint` updates independent preview rates only through accepted status rendering.
- New host metric collection is synchronous inside that snapshot path. Hardware
  collection latency remains unmeasured; metrics/serialization nulls are ruled out
  as the cause in the local reproduction.

## Execution order

1. Add red regressions to the existing HTTP/timing/browser coverage: a minimal
   clock reply must bypass expensive metrics; untrusted clock still explains
   transport/camera activity without fresh-health or Resume claims.
2. Implement a small independent observation clock endpoint and explicit browser
   probe/stream diagnostics. Preserve all canonical bounds, replay/boot fences,
   monotonic expiry and fail-closed Resume. Do not treat receipt as capture age.
3. Move optional vitals collection into one bounded background sampler, outside
   HTTP/SSE/publisher critical paths, with unknown warmup/stale samples and bounded
   shutdown. Prove slow/missing metrics cannot stall clock/status delivery.
4. Run actual server/browser physical-mode fixtures: null/missing fields, skew,
   delay, reconnect/reboot, malformed stream sample and HTTP failures; STOP remains
   accessible, Resume remains deliberate and fresh. Review pixels and report scope.
5. Audit remaining UX/startup/config/transport and write a prioritized local
   backlog with evidence, acceptance criteria, dependencies and hardware gates.
   Evaluate retain/ROS2bridge/port from current official platform/QoS/lifecycle docs.
6. Finish a reviewable patch and exact verification report for draft review.

## Product constraints

Single honest camera preview, no new capture consumer; no fake sensor health or
battery percent; retain no-voice/no-STT and disabled motion/homing defaults. Explain
intent versus queued/dispatched/result/physically unverified outcomes. Unknown or
stale capture timing remains unknown, and no auto-resume or clock-setting changes.

## Completion

Local implementation and review are complete. The 54 focused dashboard tests,
89 affected control/transport tests and three browser suites pass. The final full
smoke passes 347 tests, off-body/scenarios/HTTP and live priority STOP/explicit
Resume. The reproduced runtime ordering defect is repaired with 16 new regressions
and independent safety/concurrency review; no protection was weakened. See the
[verification report](../reports/dashboard-local-repair.md) for exact evidence,
limits and reproducible commands. The owner's later direction selects ROS 2 as
the long-term backbone; the [migration design](../design/robotics-architecture-ros2.md)
now specifies typed interfaces, phases, platform gates, reuse and rollback.
ROS runtime integration, device operation, merge and deployment remain outside
this draft's acceptance claims.
