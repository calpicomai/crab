# ROS observation bridge — local verification, 2026-10-27

Baseline `17059297fb62a7df8dc42afc0421d91e1c8477ac`; local branch
`feat/ros-observation-bridge`. Core commit
`4ef6bf6b6265f46ec2945e882b3771cdfac4bd46`; publisher/runtime commit `b2c6c0d`.
Nothing pushed, published, merged, installed or run on a physical device.
No Pi source, default launcher, saved configuration, capture or control changes.

## Results

| Check | Result and evidence |
| --- | --- |
| Pure converter/worker/adapter tests |36PASS: `brain/.venv/bin/python -m unittest tests.test_ros_observer tests.test_ros_observer_node -q`. Covers malformed/null/missing metadata, clock bounds/directions/expiry/steps, replay/sequence/boot/publisher changes, delayed publication, bounded bodies/deadlines/latest-slot/shutdown, GET-only behavior and honest diagnostics. |
| Full project check | `bash test_sim.sh`: **383testsPASS**,13.113s unit stage; off-body checks, all9simulated scenarios, HTTP synthetic camera/motion protocol and live greeting/priority STOP/explicit Resume PASS. Final log `/tmp/crab-ros-full-final.log`. All fixture motion is explicit simulation. |
| Actual ROSIDL and DDS | `docker run --rm --network none --mount type=bind,source=/workspace/crab-dashboard-diagnosis,target=/src,readonly crab-observer-humble-test:local`: PASS, generated C/C++/Python interfaces and real two-process rclpy/FastDDS with loopback fake HTTP. Final log `/tmp/crab-ros-runtime-verified.log`. |
| Runtime fault cases | Typed delivery/identity; missing/null host and companion recovery; stale repeated source capture;500ms skew retained as untrusted; reboot/sequence reset recovery; HTTP503 diagnostics without old snapshot republication; stalled subscriber gets newest; only GET clock/state. PASS. |
| Runtime authority/QoS | Observer has **zero subscriptions and services**. Actual local Publisher objects declare KeepLast1; discovered reliability=best-effort, durability=volatile, lifespan2s. FastDDS discovery reports history UNKNOWN/depth0, so history/depth are checked on the actual local Publisher objects rather than inferred from discovery. PASS. |
| Shutdown | SIGTERM through the documented shell launcher exits0 in0.264s in the final run. Daemon worker has bounded join and late-output fence. No hardware shutdown/STOP latency claim. |
| Independent review | Read-only reviewer found no concrete freshness, clock, replay, queue, failure or control-boundary defects; independently ran36focused tests PASS. It did not rerun DDS or claim hardware validation. |
| Static checks | `git diff --check`, `bash -n ros2/test.sh ros2/run-observer.sh`, and Python3.10 AST syntax checks PASS. |

Observed runtime dependency evidence: official
`ros:humble-ros-base-jammy@sha256:d9bcf2e6f72df127fdee07048c2e87b1b4b9a59a74af0592f4489fc2844aa855`;
local test image
`sha256:9c1882ac088944c230ea0204232f9ba8f09497187ce96e7eedda99f576310328`.
Linux amd64 Jammy, Python3.10.12; rclpy3.3.22-1jammy.20260907.213431,
rmw-fastrtps-cpp6.2.10-1jammy.20260907.210327,
rosidl-default-generators1.2.1-1jammy.20260907.201146,
pydantic2.14.1/httpx0.28.1. Container has no external network, hardware mounts,
host-network access or Docker-socket mount. Read-only source mount; generated
overlay is temporary. This is actual middleware evidence, not mocked rclpy.

## Failures encountered and resolved

The core worker's proof-first tests initially failed with missing
`brain.ros_observer`. Root's first actual middleware run compiled ROSIDL but
failed with missing `brain.ros_observer.node`, before adapter implementation.
The first adapter run exposed Humble's IDL `byte` mapping: DiagnosticStatus.level
requires a length-one bytes value. Fixed the adapter, then actual DDS passed.
An added remote-history assertion subsequently failed because FastDDS endpoint
discovery reports UNKNOWN/0 for history/depth; changed the test to inspect real
local Publisher objects for those policies, keeping remote reliability,
durability/lifespan checks and slow-subscriber delivery checks. Final runs pass;
no product safeguard or timeout was relaxed to hide a failure.

Development container APT initially failed DNS; offline host-resolved wheels
were used with the already provisioned official ROS image. Docker's default
build metadata directory was read-only, so a task-private Docker config under
`/tmp` was used. No system DNS, mount, network or OS setting changed.

## Remaining gates and next work

No arm64 Jetson runtime/resource benchmark, physical observation comparison,
hardware operation, ROS installation, simulator physics or training occurred.
Jetson storage/journal warning remains unresolved. Accept storage separately,
then authorize/provision matching Humble/Python3.10 and dependencies before the
future owner-controlled startup in [the guide](../ros-observer.md). Validate
build/imports on arm64, start the observer alone with hardware-disabled current
profiles, compare identities/values/age and confirm stop/crash leaves companion
ownership unchanged. Measure CPU/RSS, bytes, drops and p95/p99 observation age
under idle/model load. Humble support ends May2027; plan the next platform stage.

[Crab Lab/autonomy design](../design/crab-lab-autonomy.md) and
[prioritized backlog](../backlog/robotics-systems.md) capture the owner's custom
controller, physics-first testing, vision/reasoning, offline improvement loop and
grounded decision-view goals. Those are next-phase designs, not implemented
features or a deployment authorization. UI-05 records the stale awaiting-outcome
label separately; no dashboard/control semantics changed in this milestone.
