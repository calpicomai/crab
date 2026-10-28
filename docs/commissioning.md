# Paired software commissioning

This guide prepares a stationary Crab deployment; it does not authorize device
operations by an unattended agent. Approve each stage before execution. Keep
Pi and Jetson on the same reviewed release. A working simulator, successful
process or HTTP response does not establish physical movement, audibility,
sensor accuracy or safe navigation.

## 1. Inventory, preservation and storage

Read `AGENTS.md` and `CLAUDE.md`. Identify each host and repository before writing.
Stop the existing foreground companion and Pi server with Ctrl+C, after owner
approval. Check for an existing service/second writer; inspect its actual unit
before proposing its specific stop command. Do not use broad `pkill`, start a
duplicate server, or change service enablement. Preserve local `crab.env`,
calibration, identity/memory, model assets and untracked files. Make an
owner-approved private backup of those specific locations before changes;
never publish their contents. Record both current Git SHAs and dependency
versions for rollback.

Run from each repository, read-only:

```bash
git rev-parse --show-toplevel HEAD
git status --short
findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS -T .
df -h .
cat /etc/os-release
uname -m
getconf LONG_BIT
python3 --version
dpkg --print-architecture
if [ -f /etc/nv_tegra_release ]; then cat /etc/nv_tegra_release; fi
dpkg-query -W nvidia-jetpack nvidia-l4t-core 2>/dev/null || true
```

Known inventory: Pi Bookworm ARMHF userspace/Python3.11 despite an aarch64 kernel;
Jetson Ubuntu22.04 arm64/Python3.10, JetPack6.2.1/L4T36.4.7. Verify rather than
assuming those facts still hold. Jetson's SD had write I/O errors, an aborted
journal and protective read-only remount. A reboot restoring writes did not
resolve that gate. **Stop before update, installation or deployment until the
owner accepts storage health through a separate recovery plan.** Do not remount
read-write blindly or run filesystem repair on a mounted root. Disk free space
alone is insufficient evidence.

## 2. Pinned update, after approval

Set `RELEASE_SHA` to the full 40-character reviewed main commit supplied with
the release report, on both devices. This block installs nothing and starts
nothing. It aborts tracked edits, divergence and ignored/untracked collisions;
it neither deletes nor stashes local files. Run it from the intended checkout.

```bash
export RELEASE_SHA='<full reviewed main commit from release report>'
(
  set -eu
  [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full commit SHA' >&2; exit 1; }
  cd "$(git rev-parse --show-toplevel)"
  git diff --quiet && git diff --cached --quiet || { echo 'Tracked edits: stop and preserve them' >&2; exit 1; }
  [ "$(git branch --show-current)" = main ] || { echo 'Expected main; inspect this checkout' >&2; exit 1; }
  printf 'Previous commit: %s\n' "$(git rev-parse HEAD)"
  git fetch origin main
  git merge-base --is-ancestor "$RELEASE_SHA" FETCH_HEAD || { echo 'Release is not on fetched main' >&2; exit 1; }
  git merge-base --is-ancestor HEAD "$RELEASE_SHA" || { echo 'Checkout diverges or is newer: inspect first' >&2; exit 1; }
  git merge --ff-only --no-overwrite-ignore "$RELEASE_SHA"
  [ "$(git rev-parse HEAD)" = "$RELEASE_SHA" ]
  git status --short
)
```

Any failure is a stop condition, not permission to reset, force checkout,
delete files or retry against a different ref. Confirm matching SHAs on both
hosts before starting either role. Keep private backups separate from Git.

## 3. Dependencies and passive readiness

With an existing interpreter, the new report needs no runtime imports/network:

```bash
python3 -m brain.pet.readiness --offline
# Jetson only: static Jammy/arm64/Python3.10 comparison, not ROS runtime acceptance
python3 -m brain.pet.readiness --offline --ros-humble
# Existing brain environment plus saved trusted crab.env preferences:
bash brain/run.sh check -- --offline --ros-humble
```

The direct Python form inspects only its process environment; the launcher
sources local `crab.env`. Module discovery is not an import/ABI/model test.
Exit0 means requested report operations completed, not that every feature is
available. See [diagnostic states and limits](commissioning-diagnostics.md).
Microphone STT now defaults off, including an empty `PET_STT`. An existing saved
`PET_STT=1` remains an explicit opt-in; the stationary command below additionally
uses `--no-stt` so saved preferences cannot start microphone input.

If dependencies need installation, pause for the owner's explicit installation
approval after the storage gate. Existing role scripts are `bash robot/setup.sh`
on Pi and `bash brain/setup.sh` on Jetson. They write their role's `.venv`,
upgrade pip and install requirements; preserve the previous environment first.
Brain setup imports optional library modules for visibility, which can initialize
native runtimes. Do not run it merely to obtain a passive report. Neither script
installs ROS, models, OS drivers or accepts hardware. Do not run old movement
demos, perception installers or power scripts as part of this milestone.

## 4. Approved stationary startup

These commands intentionally leave physical sensors disabled for the first
transport/UI check. Unknown battery/sonar is expected, never fabricated healthy.
The owner must separately approve server startup and hardware-facing imports.
Use one foreground terminal per process so ownership and Ctrl+C are clear.

Pi, from its repository:

```bash
PICRAWLER_SIMULATE=0 PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_HOME_ON_START=none \
PICRAWLER_CAMERA_ENABLED=0 PICRAWLER_AUDIO_ENABLED=0 \
PICRAWLER_ULTRASONIC_ENABLED=0 PICRAWLER_BATTERY_ENABLED=0 bash robot/run.sh
```

Jetson, after the Pi is up: set `PI_BASE_URL` to the verified Pi LAN endpoint;
do not substitute an untrusted/public URL. The check reads health/status and
requests audio diagnostics with `play:false`; it does not open a control owner.

```bash
export PI_BASE_URL='http://<verified-pi-address>:8000'
bash brain/run.sh check -- --base-url "$PI_BASE_URL"
```

Then, after owner approval for the companion control connection:

```bash
bash brain/run.sh pet -- --base-url "$PI_BASE_URL" --dashboard \
  --no-voice --no-stt --no-camera --no-llm --no-emote
```

Open `http://<verified-pi-address>:8000/sim`. Verify real source, actuators disabled,
fresh Pi/Jetson transport/vitals, and disabled inputs. Type Hello and inspect its
correlated outcome; it must not claim sound or movement. Test STOP, then a fresh
explicit Resume. Resume permits future eligible commands; it does not move.
Approved disconnect/restart checks must show unknown/stale states, latched stop,
no replay and new boot/session evidence. Do not retry uncertain actions.

Stop the companion with Ctrl+C, then the Pi server. SIGTERM is supported when
delivered to the identified process. A progress fault permanently fences that
companion and requires restart; do not try to clear it through Resume. Record
cleanup failures as unknown and use the physical cutoff for actual danger.
See [runtime ownership limits](runtime-lifecycle.md).

## 5. Optional features and rollback

Enable each sensor, audio output, microphone, perception/model or actuator only
through its own approved acceptance stage in [hardware acceptance](HARDWARE_ACCEPTANCE.md).
Camera capture is separate from recording. Microphone input and real-data
collection remain opt-in; no automatic uploads or training occur. Keep the
hardware MJPEG encoder opt-in and compare measured age/CPU/quality first.

The [ROS observer](ros-observer.md) is optional and GET-only, not a control path.
Do not install ROS on the ARMHF Pi. Humble is a Jetson ABI compatibility stage,
with support ending May2027; provisioning and actual arm64 runtime/resource
acceptance require approval after storage acceptance. The guide's build/start
commands apply only after those prerequisites exist. Stop only the observer to
roll it back. [Crab Lab](crab-lab.md) runs in a separate developer environment;
it has no route to the physical robot. Its Linux/Windows software tests do not
validate the owner's PC/GPU or provisional mechanics.
Compatibility and support facts follow [official REP-2000](https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-2000.rst)
and the [Humble binary installation platforms](https://raw.githubusercontent.com/ros2/ros2_documentation/humble/source/Installation/Ubuntu-Install-Debs.rst).

For software rollback, stop both roles first and select the exact prior paired
commit recorded in stage1. With owner approval, clean tracked trees, accepted
storage and preserved configuration/environments, use on each host:

```bash
export ROLLBACK_SHA='<full previously working paired commit>'
(
  set -eu
  [[ "$ROLLBACK_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 1
  cd "$(git rev-parse --show-toplevel)"
  git diff --quiet && git diff --cached --quiet || { echo 'Preserve tracked edits first' >&2; exit 1; }
  git cat-file -e "$ROLLBACK_SHA^{commit}"
  git checkout --detach --no-overwrite-ignore "$ROLLBACK_SHA"
  [ "$(git rev-parse HEAD)" = "$ROLLBACK_SHA" ]
)
```

No force/reset/clean occurs. This intentionally leaves detached HEAD; inspect
before returning to main. A source rollback does not undo dependencies or data
migrations. Use the matching preserved environment and explicit recovery plan;
never overwrite memory/calibration with a guessed older copy. Run passive and
stationary checks again before an approved restart. Record exact evidence and
remaining gates rather than declaring hardware accepted from software checks.
