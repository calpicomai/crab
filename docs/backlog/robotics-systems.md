# Crab engineering backlog — 2026-10-26

Review based on main `88df7b3` and the startup-recovery patch.
Priorities reflect observed failures and code evidence, not a hardware diagnosis.
All hardware tasks are future owner-controlled verification gates. ROS 2 is the
intended long-term backbone; the reversible
migration is specified in [the decision record](../design/robotics-architecture-ros2.md).

## Status refresh — 2026-10-28

The tables below retain their original evidence and priorities. Use the
[commissioning report](../reports/software-commissioning.md) for current software
verification and the [commissioning guide](../commissioning.md) for staged use.

- Dashboard recovery and Resume ordering shipped in PR46. Typed read-only ROS
  observations shipped in PR47, maintained isolated Crab Lab in PR48, exact
  dashboard input outcomes in PR49, and persistence/progress/startup/shutdown
  ownership in PR50. CTRL-02/03 and LIFE-01/02 now have fault/process regressions;
  physical driver interruption remains unverified.
- This commissioning increment addresses TIME-01 end to end through camera,
  interpretation and Coordinator, adds passive platform/profile diagnostics for
  START-02, makes microphone input default-off, and supplies tested paired
  update/rollback plus a self-contained CLI handoff. Protocol/session/freshness
  guards and disabled motion defaults remain intact.
- ROS-01 now has an implemented observer and off-device middleware evidence.
  Jetson arm64 resource/runtime acceptance remains open. ROS control integration
  and a ROS dashboard gateway are future work, not implied by the observer.
- LAB-01 has actual CPU physics, browser controls, seeded evaluation and bounded
  opt-in synthetic episodes. MODEL-01 fidelity, self-collision, measured servo
  dynamics and sim-to-real validation remain open; there is no learned policy.
- STORAGE-01 is the first device gate: the Jetson's previous I/O errors and
  aborted journal require separately accepted storage recovery/verification
  before installation or deployment. A reboot or writable mount does not close it.
- SENSOR-01, publisher-lineage TIME-02, measured perception quality/resource
  budgets, physical audio/mic/sensor acceptance and actuator commissioning remain
  open. The [next perception/data milestone](../design/perception-learning-milestone.md)
  specifies off-device evidence and held-out evaluation before learning or live
  media collection. No unattended training, recording or autonomous motion is added.

## This local iteration

| ID / priority | Evidence and outcome | Acceptance / verification |
| --- | --- | --- |
| UI-01 P0 | Silent clock rejection leaves `latestState=null`, initial UNKNOWN labels and blank preview FPS despite real SSE/JPEG200. | Native browser/server test with ±skew/delay: diagnostics and measured preview activity remain visible; capture freshness and Resume stay false. Lightweight clock probe bypasses snapshot work. Implemented locally. |
| OBS-01 P1 | `SystemVitals.snapshot` previously collected under the request/publisher path. | One bounded worker per process; blocked metrics do not block snapshot/clock/state. Include collection duration in age; missing values remain null; late results cannot restore closed health. Implemented locally. |
| UI-02 P1 | A terminal EventSource error leaves a closed object that native reconnect cannot recover. | One bounded reconnect timer only for CLOSED; normal native reconnect retained; hidden/pagehide cancels it. Actual browser network disconnect/reconnect. Implemented locally. |
| START-01 P2 | Readiness's single try block hid status/audio/local checks after health failure. | Independent per-stage results and durations; no WS control owner, playback, mic capture or motion. Preserve required compatibility failure. Implemented locally. |
| CTRL-01 P1 | Successful explicit new-epoch Resume arrived before matching status; the coordinator retained the old sampled epoch and relatching mistook the acknowledgement for a new STOP. | Implemented locally with 16 deterministic ordering/error regressions and independent safety/concurrency review. A validated same-boot/unlatched acknowledgement records only the original event epoch, without refreshing status/sensor age. Newer STOP, stale input, reboot, bad acknowledgement and expiry still block. Confirmed cancelled cleanup does not issue another STOP. No test-harness sequencing or timeout change; final smoke results are in the report. |

## Next bounded control and data work

| ID / priority | Evidence | Acceptance criteria | Dependencies / verification gate |
| --- | --- | --- | --- |
| CTRL-02 P1 | Coordinator loop performs SQLite writes/summary and log flushes synchronously (`loop.py:279,285,297,318–319`; `memory.py:29,77`). | Block/fail DB/log writer while ticks, fresh STOP input and health stay responsive. One bounded persistence owner; ordering, backups and unpersisted/error reporting. | Independent of sensor work. Measure Jetson storage latency later. |
| CTRL-03 P1 | Pi pings prove the separate transport reader is alive (`robot/realtime.py:302`, `brain/realtime.py:240`), not coordinator progress. | Expose monotonic progress age first. Freeze coordinator with reader alive; separately specified command-authority deadline must latch STOP without replay. Recovery requires fresh explicit Resume. | Needs measured deadline and a canonical capability/version contract; do not silently repurpose heartbeat. Vendor-call interrupt latency is hardware-only. |
| TIME-01 P1 | Perception still directly subtracts Jetson wall from Pi capture (`brain/perception/camera.py:161,206`), rejecting a Pi30ms ahead. | ±30ms MJPEG fixtures decode through a bounded read-only anchor; stale/future/replayed/malformed samples fail. Boot/reconnect clears anchor and sequence. Original timestamp/provenance retained. | Reuse shared clock and lightweight endpoint; separate perception process must not become a second WS control owner. Exposure-to-detection remains hardware-only. |
| OBS-02 P1 | Observations mix source wall stamps, receipt times and scalar age; version1 advertisement does not identify clock/build capabilities. | Versioned observation envelope: producer, boot/session/sequence, acquisition/domain, receipt, uncertainty, availability/use/verification. Contract tests reject old/missing capabilities before control. | Extend canonical schema deliberately; compatible paired rollout. Do not fabricate monotonic comparisons across hosts. |
| LIFE-01 P2 | Resource construction precedes cleanup try in `brain/pet/loop.py:190–233`; only KeyboardInterrupt handled at322. | Inject each startup failure and SIGTERM; close already-created clients/workers in bounded order. Honest uncertain outcomes; no retry of dispatched commands. | Lifecycle owner/ExitStack and cooperative termination. No service installation needed for local tests; physical shutdown latency later. |
| LIFE-02 P2 | Native browser reboot test showed Uvicorn's graceful SIGTERM waits on an open SSE stream beyond4s; lifespan cleanup starts after connections drain. | Specify a bounded observation-stream drain/close path; test SIGTERM with active SSE and WS plus in-flight fake audio. STOP and amplifier cleanup must precede process exit; uncertain work is not retried. | Separate lifecycle patch with measured deadlines. Current test uses an abrupt reboot of its owned fake server; no production timeout or service setting changed here. |
| SENSOR-01 P2 | HTTP/WS/reflex paths can call shared drivers; value and health are separate reads (`server.py:_status_with_distance`, `sensors.py:48`). | Fake concurrent reads show one driver call at a time and atomic value/health metadata. Required fresh reflex cannot use a receipt-refreshed cache; no echo stays unavailable. | Separate ownership design. This is not a sonar rewrite or an established cause of the owner's sonar behavior. Hardware reliability needs later measurement. |
| START-02 P2 | Launcher sources saved prefs, but imported legacy config eagerly parses unused navigation knobs (`brain/run.sh:14`, `brain/config.py:31`). | Read-only effective-profile report names feature values/sources without secrets. Invalid unused knobs do not break typed-only startup; invalid active safety values fail clearly. | Split active config parsing from legacy modules; preserve config files. No silent fallback to weaker limits. |
| TIME-02 P2 | Telemetry publisher ordering uses Jetson wall-start epochs (`robot/telemetry.py:46`), so a backward clock step may require reset. | Explicit run lineage/session test rejects delayed old publishers and handles boot/backward steps without automatic reset or replay acceptance. | Observation-envelope versioning; separate from motion authority. No clock changes. |
| UI-03 P2 | Renderer, controls and camera/clock state remain in one script; one rendering exception previously ended RAF. | Separate transport/view contracts incrementally; unexpected render errors are visible, STOP remains available, Resume fails closed, next valid sample can recover. Keyboard/focus and320/768/1440 screenshots. | Keep lightweight stack; avoid a framework rewrite. Some recovery defenses are local in this iteration; module extraction remains open. |
| UI-04 P2 | Raw cached intention/result words can imply current or physical execution; history is finite and depends on sample receipt. | Show accepted/queued/dispatched/result/unknown separately; stale records labelled last reported. Vendor completion never claims displacement; played never claims audibility. | Stable event/outcome envelope; bounded history and explicit gaps. No fabricated odometry. |
| OBS-03 P2 | Clock, rendering, SSE, coordinator and model errors have different owners and recovery needs. | Small bounded diagnostic snapshot/trace with component state, last success/error, duration, age and build identity. No raw audio/video/user text by default. One-click local export must redact secrets. | Schema and privacy review; do not add a heavy observability service. |

## Camera measurements and phased ROS migration

| ID / priority | Evidence | Acceptance criteria | Gate |
| --- | --- | --- | --- |
| CAM-01 P1 | Owner960x720q90 looked clearer but ~5.8fps/~388ms age/~214ms RTT; software JpegEncoder is current default. | Hold scene/resolution/FPS/client count constant; compare q90/q80 and explicit hardware MJPEG. Measure bytes, capture/encoded/decoded cadence, header/body/decode/queue timing and CPU. No cloud result advertised as Pi speedup. | Hardware/API capability inventory and owner-run comparison later. No default encoder/resolution switch. |
| CAM-02 P2 | OV5647 identified, lens/focus/exposure/contamination unverified. | Distinguish optical blur from compression/display scaling using same-scene native crops and measured settings. Only supported controls offered; no autofocus invention or OCR scope. | Owner-controlled inspection/capture later; no physical action in this task. |
| CAM-03 P2 | Encoded-output timestamp excludes exposure and encoding; q/rate labels are requested, not achieved. | Preserve sensor timestamp provenance through encoding with a documented clock domain and measurable stage ages; test reorder and dropped frames. Show unmeasured stages honestly. | Verify installed Picamera2 metadata/output APIs; no guessed alignment. Hardware exposure latency gate. |
| ROS-01 P1 | Owner selected ROS 2 for long-term flexibility; platform compatibility is still unknown. | Inventory platform, pin distro/RMW, implement typed observation/health/intent contracts and a disabled-by-default read-only adapter. Synthetic contract/fault tests; no commands or extra capture. Stop/crash leaves existing stack intact. Quantify CPU/RSS/shared RAM/network and p95/p99 age/drops. | OBS-02 contracts and exact OS/JetPack/Python inventory. Decision-record phases1–2; no install authorized here. |
| ROS-02 P2 | Dashboard, perception and lifecycle should share typed evidence rather than parallel ad-hoc contracts. | Existing browser acceptance runs against a ROS observation gateway; isolated replay cannot reach hardware; future perception outputs preserve original frame identity/expiration. One coordinator and bounded persistence owner. Reversible launch profiles and measured8GB budget. | ROS-01, UI-03, CTRL-02/03 and LIFE-01/02. Phases3–5; future runtime work, not implemented here. |
| ROS-03 P3 | Moving command transport reopens every authority/lifecycle boundary. | Separate reviewed Pi gateway preserves stop/lease/epoch/expiry/uncertain-result equivalence. One writer and authoritative transport; no replay, implicit re-arm or startup motion. Fault injection before separately approved hardware tests. | Successful observation/perception migration and explicit command-safety design. Phase6; no wholesale port in this iteration. |

## New owner-verified platform and next milestones — 2026-10-27

Jetson: Jammy22.04.5 arm64/Python3.10.12/JetPack6.2.1/L4T36.4.7. Pi: Bookworm
armhf userspace/Python3.11.2. Humble is the matching observation-stage target,
with support ending May2027. Jetson SD write/journal errors remain unresolved;
reboot restoring RW is not storage acceptance. No device installation here.

| ID / priority | Evidence and bounded next action | Acceptance / dependency |
| --- | --- | --- |
| ROS-01 update | First typed read-only observation bridge is implemented locally; see [runtime guide](../ros-observer.md). No control/media path or Pi changes. | Pure conversion/HTTP plus actual generated Humble/DDS tests; arm64/resource/operational acceptance remains open. Storage and separately approved provisioning precede device installation. |
| UI-05 P2 | Owner observed `awaiting behavior outcome` remaining after real Hello/STOP/Resume results on1705929 with actuators disabled. | Deterministic event/result correlation test with delayed/out-of-order/reconnect and terminal outcome, preserving stop epochs. Clear the label only from the matching outcome; no control semantics change. Separate patch. |
| MODEL-01 P1 | Owner wants Crab-owned motion; current vendor targets have no measured joint/contact/IMU feedback. | Independent frame/kinematic model, provenance/uncertainty manifest, read-only calibration importer and fake-driver limits/sign tests. Verify geometry and servo identity; no hardware writer replacement yet. |
| LAB-01 P1 | Owner wants remote physics-based training/testing; Windows PC has3070Ti/64GB RAM, CPU/VRAM unverified. | [Crab Lab v0](../design/crab-lab-autonomy.md): CPU MuJoCo single-leg then12-joint arena, bounded servo/contact models, localhost browser viewer, seeded regression and isolation proof. No physical adapter, RL, paid compute or installation in this iteration. MODEL-01 first. |
| CTRL-04 P2 | Scripted vendor frames do not satisfy adaptive/custom movement. | Project procedural swing/stance controller against deployment-shaped observations; held-out friction/mass/noise/latency tests, joint/workspace/stop bounds. LAB-01; feedback capability review before physical balance claims. |
| SENSE-02 P2 | No measured joint states, body orientation or foot contact integrated. | Specify IMU/joint/contact options against mechanical/electrical/timing/resource needs; distinguish commands and estimates from measurements. No inferred sensors or hardware purchase/install. Required channels depend on controller goals. |
| AUTO-01 P2 | Meaningful behavior/navigation differs from locomotion adaptation. | Perception/estimator capability matrix, expiring goals and one coordinator; deterministic simulated tasks with blocked path, no echo, localization loss and STOP. No random wandering, hidden pursuit or LLM servo control. LAB-01/CTRL-04 and measured localization/clearance gates. |
| MEDIA-01 P2 | Owner asks whether self-hosted LiveKit can support the learning/decision view. | Synthetic benchmark versus JPEG, explicit outbound observation allowlist and reject incoming/wildcard/service routes. Preserve frame provenance, capture owner and local control. Egress separate/opt-in; recording alone is not a learning episode. No installation/dependency or media transmission now. |

The new [autonomy/simulation decision](../design/crab-lab-autonomy.md) records
vendor dependency replacement, licenses, MuJoCo/Gazebo/Isaac comparison, Windows
versus WSL, privileged-truth isolation, measurements and hardware-only gates.

## Definition of done

A local item closes only with a named regression that failed before and passes
after, relevant broader checks, source review and explicit limits. Simulated or
fake-driver success never closes a hardware gate. Safety/protocol changes get
separate reviewable commits. Unknown/stale data cannot authorize movement; Pi
remains the sole actuator writer and defaults remain disabled. Nothing in this
backlog authorizes deployment, clock/network/mixer changes, recording or movement.
