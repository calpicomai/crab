# Read-only ROS observation bridge

Local implementation, 2026-10-27. Baseline `1705929`. This optional process reads
the existing Pi API and publishes typed observations; the Pi server, companion,
capture, actuator permissions and default launchers are unchanged. It creates no
application subscriptions or services, opens no companion control WebSocket and
never sends commands. Stopping it needs no robot STOP because it has no command
authority. Existing motion ownership and STOP protections remain authoritative.

## Compatibility and deployment gates

The owner's Jetson is Ubuntu22.04.5 arm64/Python3.10.12, JetPack6.2.1+b38,
L4T36.4.7. Humble's Jammy arm64 binaries match that OS/Python ABI. Its support
ends May2027, so this is a compatibility stage with a short remaining support
window, not a long-term distro commitment. The Pi is Bookworm **armhf userspace**
despite an aarch64 kernel; no ROS installation or port on it is needed.
[REP-2000](https://reps.openrobotics.org/rep-2000/),
[Humble platforms](https://raw.githubusercontent.com/ros2/ros2_documentation/humble/source/Releases/Release-Humble-Hawksbill.rst),
[Python binary compatibility](https://raw.githubusercontent.com/ros2/ros2_documentation/humble/source/How-To-Guides/Using-Python-Packages.rst).

The Jetson SD root had write I/O errors, an aborted journal and protective
read-only remount; reboot restoring writes did not establish storage health.
Resolve that separately before any substantial installation. No device ROS
installation, storage repair, OS upgrade or deployment was performed or is
authorized by these instructions. Provisioning ROS and matching dependencies is
a future separately approved step. Preserve the existing environment/config.

## Contract and behavior

`crab_observation_msgs` defines four bounded ROSIDL messages: ObservationContext,
InputObservation, HostVitals and ObservationSnapshot. They include source,
robot boot, reported transport session, publisher/sequence, original acquisition
and producer receipt stamps with explicit clock domains, bridge-local receipt,
bounded clock offset and conservative age. Missing identities/timestamps/values
have validity flags; numeric zero placeholders are never valid readings.
Foreign monotonic epochs are metadata only. Availability, use and physical
verification are separate fields. Real provenance does not prove verification.

The bridge measures its own Pi clock anchor using `/sim/clock`, then GETs
`/sim/state`. Default1Hz, maximum5Hz, one HTTP worker/connection, one latest result
slot, response cap256KiB, inputs cap32, bounded reasons/IDs and request timeout
0.5s. Each read also has an elapsed deadline; a blocked read can overshoot it by
one transport timeout. Worker close joins at most0.5s and fences late output.
A blocked resolver may outlive that join in its daemon thread; it cannot publish
late data or acquire hardware authority. Failures and replacements are counted.

The 2Hz ROS timer consumes each result once and ages it with local monotonic
time before publication. Unknown calibration retains raw diagnostic values but
marks timing unverified/freshness false. Repeated Pi receipt does not refresh
source capture. Changed boot/session/publisher/sequence and replay checks remain
explicit. Failed polls emit diagnostics, never a retained successful snapshot.
Last observations may be retained internally only to describe staleness.

Topics `/crab/observations` and `/crab/diagnostics` use best effort, KeepLast1,
volatile durability and2s lifespan. These are observations, never safety inputs
or control authorization. Consumers must check validity, boot/sequence, original
age and time since **their own** receipt; never compare bridge monotonic values
with another host's monotonic epoch. DDS lifespan covers publication-to-receipt,
not acquisition freshness. Default discovery is process-local localhost only,
domain173 unless explicitly set. Domain IDs are not access control. No camera,
microphone bytes, utterances, identity narrative or full raw state are exported.
[Official QoS semantics](https://raw.githubusercontent.com/ros2/ros2_documentation/humble/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst).

## Future owner-controlled startup and rollback

Prerequisites: storage accepted; separately provisioned Humble and colcon;
matching Python3.10 environment with rclpy visible plus existing brain httpx and
pydantic dependencies; this branch available locally. No installer is provided.
Build the message overlay in an isolated developer build directory:

```bash
cd "$HOME/crab"
source /opt/ros/humble/setup.bash
colcon --log-base ros2/log build --base-paths ros2/src \
  --packages-select crab_observation_msgs \
  --build-base ros2/build --install-base ros2/install
source ros2/install/local_setup.bash
# Choose an existing matching environment; no environment is modified here.
export CRAB_OBSERVER_PYTHON="$PWD/brain/.venv/bin/python"
bash ros2/run-observer.sh --base-url http://10.1.20.170:8000
```

The launcher checks the ABI/imports and fails clearly when prerequisites are
missing. It does not source `crab.env`, install packages, arm anything or start
the companion. Keep the existing Pi/brain launch profile hardware-disabled.
To roll back, Ctrl+C only the observer process; remove its optional launch from
your terminal workflow. Do not stop/restart the Pi or modify saved configuration
for this rollback. Overlay build products are ignored; no service is installed.

## Off-device reproduction

The test image is **amd64-only** and is not a Jetson installation artifact. It
uses a pinned official Humble/Jammy image and host-resolved CPython310 wheels;
container build/run have no network. From a development host with Docker and an
existing dependency resolver (not the warning-affected Jetson):

```bash
python -m pip download --only-binary=:all: --platform manylinux2014_x86_64 \
  --python-version 310 --implementation cp --abi cp310 \
  --dest ros2/.test-wheels -r ros2/test-requirements.txt
docker build --network none -f ros2/Dockerfile.test \
  -t crab-observer-humble-test:local ros2
docker run --rm --network none \
  --mount "type=bind,source=$PWD,target=/src,readonly" \
  crab-observer-humble-test:local
```

`ros2/test.sh` generates/compiles real ROSIDL, runs real rclpy publishers and a
separate DDS subscriber against fake loopback HTTP, and cleans its temporary
overlay. No devices, host networking, Docker socket or hardware config are
mounted. Pure tests run through the ordinary project test discovery; actual ROS
integration is separate in `tests/ros_observer_runtime.py`. See the dated
[verification report](reports/ros-observer-local.md) for exact versions/results.
