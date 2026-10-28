# Software commissioning milestone — 2026-10-28

The stationary companion, read-only ROS observer and isolated Crab Lab now have
a coherent commissioning path. This is software evidence, not physical
deployment or acceptance of autonomous walking, balance, navigation, sensing,
audibility or a learned policy. Start with the [paired commissioning guide](../commissioning.md)
and [Codex CLI prompt](../CODEX_COMMISSIONING_PROMPT.md). The release handoff must
supply an exact reviewed main SHA; do not substitute a moving branch tip.

## Delivered behavior

- [PR47](https://github.com/calpicomai/crab/pull/47): typed GET-only ROS
  observations with source/boot/session/sequence/timing and explicit unknowns.
  No control subscriptions, command forwarding, capture or recording.
- [PR48](https://github.com/calpicomai/crab/pull/48): opt-in CPU MuJoCo Lab,
  project-owned procedural controller, real physics/browser view, bounded seeded
  evaluations and exclusive-create synthetic episode exports. Estimated mechanics
  and privileged evaluator truth remain separate from policy observations.
- [PR49](https://github.com/calpicomai/crab/pull/49): exact dashboard input/outcome
  correlation, including reconnect, delayed results, old clients and unknown
  completion. Process success never claims audible output or measured movement.
- [PR50](https://github.com/calpicomai/crab/pull/50): owned partial startup and
  signal cleanup, bounded asynchronous persistence, independent monotonic progress
  fencing/STOP, pre-drain Pi shutdown and STOP admission before optional worker
  capacity. Pending cleanup cannot prevent a new motion STOP latch.
- This increment: calibrated MJPEG timing all the way into interpretation and
  Coordinator health; matching stream boot metadata, replay/order/generation
  fencing, bounded raw clock/observer response processing; passive redacted
  platform/profile/build/dependency reports; lazy unused navigation configuration;
  malformed-health partial results; microphone default-off; and tested preserved
  update/rollback commands. Saved explicit STT opt-in remains intact.

## Local verification

The final combined checkout starts from merged runtime baseline
`4b6e05fe11d952b3d21ba8020e019479b708fa90`; the following checks exercise the
commissioning changes together. CI and its exact revision must be checked on
the final PR/main, separately from this dated local evidence.

| Check | Result and scope |
| --- | --- |
| `bash test_sim.sh` | **495 tests passed**, then off-body checks, nine scenarios, HTTP/synthetic-camera and live WebSocket greeting/priority STOP/Resume smoke passed. Unit phase33.806s. All motion uses explicit simulation/fakes. |
| `tests/browser_dashboard_startup.py` | Passed actual loopback HTTP/SSE and synthetic camera, missing/null metrics, clock rejection without blank diagnostics, reboot/reconnect and responsive STOP. No JavaScript errors. |
| `tests/browser_dashboard_outcomes.py` | Passed actual server/companion Hello/STOP/Resume, disconnected unknown outcome and matching completion after reconnect. No JavaScript errors. |
| `tests/browser_mission_control.py` | Passed synthetic real/sim/disabled/disconnected/failure states, truthful results, stale Resume rejection and desktop/narrow controls. No JavaScript errors. |
| `tests/test_commissioning_commands.py` | **10 tests passed**, executing the documented commands only against disposable local Git repositories. Exact pin, dirty/divergent trees, ignored/untracked collisions, rollback and inherited Git redirection are covered. |
| Passive readiness and shell checks | Standard-library-only `python3 -m brain.pet.readiness --offline --ros-humble` passed with STT/control disabled. Three launch/setup scripts and seven documented Bash blocks parse; local handoff links resolve. |
| Lab regression | **25 physics/API/evaluation tests passed** on the integrated runtime tree; six seeded evaluation cases passed, fingerprint `7e998496036b4414bdd78b49901c2c97197346ee1563893b8f9638f7f2b8aac0`. Lab source is unchanged by this increment. Hosted Linux and Windows software CI passed in PR48; actual owner PC/GPU performance remains unrun. |

Focused proofs include actual httpx/loopback MJPEG under +30ms synthetic skew,
both offset directions through Engine→Coordinator, stale inference, missing or
mismatched boots, same-boot replay after reconnect, excessive uncertainty,
clock steps, decoder invalidation, slow/empty HTTP chunks and cancellation.
Independent reviews found and verified fixes for dropped interpretation clocks,
raw-body coalescing, STOP/audio capacity sharing, pending STOP/disconnect ordering,
thread-start slot recovery and test-fixture Git redirection.

The slow-drip, interpretation, STOP saturation and ignored-file tests observed
the pre-fix failures. A targeted test invocation initially lacked the project's
flat test import path; final discovery and corrected targeted runs passed. Raw
stream test fakes were updated at their transport boundary, retaining their
oversize/deadline assertions. There are no unresolved failures in the checks
listed above. Runtime native stalls can still outlive bounded software waits.

The earlier observer report records an actual off-device Humble/ROSIDL/FastDDS
integration test. Pure conversion/HTTP tests here are not a new arm64 middleware
acceptance claim. Socket timeouts are inactivity bounds: DNS/header parsing and
an in-progress read do not acquire a hard whole-request deadline. Late results
remain fenced or untrusted; shutdown does not claim native work completed.

## Acceptance and remaining gates

Software gates A–G from [the acceptance plan](../plans/software-commissioning-readiness.md)
have implementations and regression evidence: observer, maintained Lab, dashboard
outcomes, lifecycle, passive readiness, synthetic evidence/data boundaries and
the handoff. Check terminal exact-head PR and post-merge main CI before treating
the published revision as the release. No device was updated or operated here.

The first actual deployment dependency is **Jetson storage acceptance**. Previous
write I/O errors, an aborted journal and protective read-only remount are not
resolved by reboot success. Installation/deployment must wait for a separately
approved recovery/verification plan; do not remount blindly or repair mounted
root. Pi ARMHF and Jetson arm64 ABI facts must be verified locally. ROS is not
installed on the Pi; optional Jetson provisioning/resource acceptance is separate.

Physical battery/sonar accuracy, camera exposure/encode/LAN latency, audio
amplifier cleanup/audibility, microphone opt-in, model quality/shared-memory
budgets, service signals and native STOP boundaries remain unrun in this task.
No model training, real recording, upload, autonomous motion or hardware adapter
was added to Lab. Its self-collision is disabled and geometry/servo dynamics
remain provisional. The [next perception/learning increment](../design/perception-learning-milestone.md)
requires actual timed evidence and held-out evaluations before training or any
separately approved physical stage.
