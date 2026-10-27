# First ROS 2 observation bridge

Baseline: merged `17059297fb62a7df8dc42afc0421d91e1c8477ac`.
Branch: `feat/ros-observation-bridge`. Pre-work tree clean. Local implementation
and tests only; no publication, device operations, installation on devices,
capture changes or control paths. Existing Pi service remains untouched.

## Platform and direction

Verified owner inventory: Jetson Ubuntu22.04.5 arm64, Python3.10.12,
JetPack6.2.1+b38/L4T36.4.7; Pi Bookworm armhf userspace with an aarch64 kernel,
Python3.11.2. Target Humble on Jetson, matching Jammy/Python3.10 binaries; support
ends May2027. Preserve typed contracts for later distro migration rather than
upgrading either device. The Jetson's SD write/journal errors remain unresolved:
reboot restoring RW is not storage acceptance and no heavy installation is in scope.

The coding host is Debian13/amd64/Python3.12 without rclpy, with Docker available.
Use an isolated official Humble/Jammy development container for actual ROSIDL/DDS
tests if available. Pure conversion/fake-HTTP tests do not establish ROS runtime
acceptance. All network fixtures are loopback; no hardware device/socket mounts.

## Coherent units and ownership

1. Typed observation contract, conversion and bounded read-only HTTP worker.
   Owner: one fresh implementation worker; files `brain/ros_observer/__init__.py`,
   `brain/ros_observer/core.py`, `ros2/src/crab_observation_msgs/**`,
   `tests/test_ros_observer.py`. Worker does not commit. Proof-first tests include
   malformed/missing fields, clock bounds, replay/boot changes, stale source,
   failures, caps, cleanup and GET-only behavior. Host owns authoritative tests.
2. ROS publisher adapter and real middleware test, dependent on unit1's contract.
   Owner: host; files `brain/ros_observer/node.py`, `tests/ros_observer_runtime.py`,
   `ros2/Dockerfile.test`, `ros2/test.sh`, documentation and ignore rules.
   No parallel authoring across the shared interface. Docker preparation is
   independent of core authoring and remains host-owned.

## Contract and bounds

- Standalone observer requires explicit base URL; no saved-config sourcing or
  automatic launch. Only GET `/sim/clock` and `/sim/state`; no WebSocket, command,
  audio/video endpoint, recording, service/subscription or parameter-to-arm path.
- Reuse canonical `shared.clock` bounds and `shared.local` destination validation.
  Keep its Python dependencies in the existing brain environment; ROS binaries
  require matching Python3.10. No new dependency in the Pi process.
- Default1Hz polling, maximum5Hz; one worker, one latest slot, bounded response
  bytes/strings/arrays/timeouts and shutdown. ROS publishers use KeepLast1,
  volatile observation QoS with finite lifespan. Expose failure/replacement counts.
- Preserve source capture and receipt times/domains, source IDs, robot boot,
  reported transport session, publisher sequence and reported uncertainty. Missing
  fields have validity flags; never invent a session/sequence or physical health.
- Local HTTP receipt is separate metadata. Conservative input age adds upstream
  source age and delivery delay; repeated receipts cannot refresh it. Do not compare
  producer monotonic epochs across hosts. Rejected clock calibration still allows
  explicitly untrusted diagnostics, never fresh input claims.
- Typed snapshots contain sensor/host values and health plus motion/stop reports
  as observations only. Availability, current use and verification remain separate.
  Missing sonar is unknown, not zero/free space; no battery percentage estimate.
- No raw utterances, saved identity narrative, microphone bytes, camera bytes or
  generic full-state JSON publication. Text reasons are bounded diagnostics.

## Verification and delivery

Pure unit/HTTP regressions first; generated ROSIDL build and actual rclpy
publisher/subscriber delivery on supported Humble if container available; assert
no control subscriptions/services, stale/error diagnostics and bounded shutdown.
Run project347test/full smoke after integration. Record exact dependency/image
versions and distinguish amd64 container evidence from arm64 device acceptance.
Provide startup/rollback instructions as future gated steps only. Bridge crash or
removal must leave existing companion/Pi authority and launch profiles unchanged.

Record the newly observed stale UI awaiting-outcome label as a separate bounded
backlog item; do not change control semantics or UI in this milestone.
