# Codex CLI commissioning prompt

Paste the text below into Codex CLI from the actual Crab repository. Supply the
reviewed release SHA from its release report. This prompt starts with inspection;
it does not grant blanket permission to install, deploy or operate hardware.

```text
Commission this Crab checkout toward a stationary, hardware-disabled session.
RELEASE_SHA = <full 40-character reviewed main commit from release report>

Read AGENTS.md, CLAUDE.md, README.md, docs/commissioning.md,
docs/commissioning-diagnostics.md and docs/runtime-lifecycle.md before acting.
Identify whether this is the Pi, Jetson or a development PC; do not infer from
kernel architecture alone. Start with read-only local inspection. Report the
current Git SHA/dirty state, OS, userspace ABI/bitness, Python and existing
environment/dependency facts. Do not dump environment secrets, configuration,
private memory, camera/mic data or Git remote credentials.

Known prior inventory, to verify: Pi Bookworm ARMHF userspace/Python3.11 with
aarch64 kernel, Robot HATv4 and OV5647; Jetson Ubuntu22.04 arm64/Python3.10,
JetPack6.2.1/L4T36.4.7. Jetson SD root previously had write I/O errors, aborted
journal and a protective read-only remount. Reboot restored writes but did not
resolve storage acceptance. Stop before update/install/deploy until the owner
accepts a separate storage recovery/verification plan. Do not blindly remount
read-write or run filesystem repair on a mounted root. Do not upgrade/reflash.

Preserve all existing tracked edits, untracked/ignored files, crab.env,
calibration, ~/.picrawler_pet or configured memory/identity, model assets and
role environments. Locate specific paths without publishing their contents.
Propose an owner-approved private backup and record the prior paired Git SHAs
and dependencies before any write. No git reset --hard, git clean, force
checkout, automatic stash or configuration rewrites. Git updates must abort on
edits/divergence/collisions, including ignored collisions. Use the pinned update
block in docs/commissioning.md only after explicit update approval; verify the
release is on fetched main and both devices use exactly the same release.
Do not silently choose a newer main. Report any blocker before continuing.

Inspect existing processes/services and their actual owners without starting a
second writer. No broad pkill. Ask before stopping/restarting any process or
service, installing dependencies/models/drivers/ROS, changing OS/network/security/
power/mixer settings, recording media or contacting a device endpoint. Do not
create credentials or persistent access. Existing user authorization for a
specific step carries forward; do not ask repeatedly for that same step.

First run passive local diagnostics with an existing Python interpreter:
  python3 -m brain.pet.readiness --offline
On the Jetson also use --ros-humble for the static ABI comparison. If using
brain/run.sh check -- --offline, remember that launcher sources trusted local
crab.env. Offline reporting must not open sockets, a control WebSocket, capture,
inference, playback or a ROS node. Availability metadata is not runtime or
physical acceptance. Named/redacted configuration errors must be resolved by
reviewing the intended setting, never weakening freshness, STOP or clearance.

After storage, preservation, paired update and dependency gates pass, request
approval for one stationary server/companion session. Use the exact stage4
commands in docs/commissioning.md: Pi actuators0/home none/simulation0, camera,
audio, ultrasonic and battery disabled; Jetson --no-voice --no-stt --no-camera
--no-llm --no-emote. Verify the Pi endpoint with the owner. Do not enable motion,
body expressions, homing, audio, microphone or physical sensor capture as a
convenience. Sensor-disabled unknown values are expected. No synthetic values
may appear as healthy real readings.

Read-only connected readiness uses health/status and audio self-test play:false;
it never owns a companion control session. Starting the companion is a separate
approved step because its disconnect latches STOP. Verify the real/sim label,
motion-disabled state, boot/session/source age, clock uncertainty, Pi/Jetson
vitals, input states and exact dashboard outcome correlation. With owner
approval, type Hello, STOP and a fresh Resume. A successful result is a software
outcome, not proof of sound or movement. Resume does not move the robot. Exercise
only specifically approved disconnect/restart cases; stale/missing information
must show unknown, old input/results must not replay, and recovery must require
fresh health plus explicit Resume. Never automatically retry an uncertain action.

Normal shutdown is Ctrl+C in each identified foreground terminal, companion
then Pi. SIGTERM is supported for a confirmed process. Record incomplete cleanup
or unresolved native work honestly. A companion progress fault permanently
fences authority for that process and requires restart, not in-process rearm.
A software STOP cannot guarantee interruption inside a vendor call or substitute
for a human-accessible physical power cutoff.

ROS: the implemented observer is GET-only observations/diagnostics, no command
subscriptions/services/forwarding or control ownership. Pi remains the sole
hardware writer. Do not install ROS on ARMHF Pi. Jetson Humble provisioning is
optional, separately approved, storage-gated and ABI-specific; its matching ABI
is not runtime acceptance. Follow docs/ros-observer.md and measure arm64 resource
overhead before adoption. Keep localhost discovery and bounded queues. Stop only
the observer for rollback; do not alter robot settings.

Crab Lab: use a separate developer checkout/environment and docs/crab-lab.md.
It is CPU MuJoCo, opt-in, loopback-only and has no physical adapter. Do not route
training/replay into hardware. Its estimated masses/geometry/servo dynamics,
disabled self-collision, open-loop procedural gait and absent learned policy
must remain conspicuous. Linux/Windows CI is not the owner's PC/GPU or mechanical
validation. Do not start training, real-data recording, uploads or paid compute.

No autonomous walking, balancing, navigation, person recognition or safe cliff
avoidance is accepted by this milestone. Later physical sensors/audio/mic/models
and especially actuators require separately bounded owner-approved stages from
docs/HARDWARE_ACCEPTANCE.md. Retain local STOP/lease/session/epoch/freshness and
one-writer protections. LLMs/planners never directly command servos.

If a software regression appears, reproduce with fakes/explicit local simulation
and repair a scoped branch with meaningful tests; do not use the real robot as
an unattended test harness. Rollback only after stopping both roles and obtaining
approval, using the prior exact paired commit, preserved config/environment and
the non-force rollback block in docs/commissioning.md. Source rollback does not
undo dependency/data migrations. Do not merge/push new repairs without approval.

Finish with: each host's exact tested SHA, commands actually run and exit states,
startup/shutdown/reconnect/outcome evidence, configuration changes (if approved),
pass/fail/unrun per acceptance gate, rollback target, and the smallest next
owner-controlled step. Distinguish software, simulation, actual device runtime
and human-observed hardware acceptance. Stop at a real dependency or approval
gate with its exact action, target and reason; never claim deployment complete
from cloud tests or a green dashboard alone.
```
