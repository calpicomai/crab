#!/usr/bin/env bash
# Run inside the isolated development container, with /src mounted read-only.
set -eo pipefail
source /opt/ros/humble/setup.bash
set -u
test_root="$(mktemp -d /tmp/crab-ros-observer.XXXXXXXX)"
trap 'rm -rf "$test_root"' EXIT
colcon --log-base "$test_root/log" build --base-paths /src/ros2/src \
  --packages-select crab_observation_msgs --build-base "$test_root/build" \
  --install-base "$test_root/install" --event-handlers console_direct+
set +u
source "$test_root/install/local_setup.bash"
set -u
export PYTHONPATH="/src${PYTHONPATH:+:$PYTHONPATH}"
export ROS_LOCALHOST_ONLY=1 ROS_DOMAIN_ID=173 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
printf '\nActual ROS runtime dependency evidence:\n'
python -c 'import sys, rclpy, httpx, pydantic; print(sys.version); print("rclpy:", rclpy.__file__); print("pydantic:", pydantic.__version__, "httpx:", httpx.__version__)'
dpkg-query -W -f='${binary:Package}\t${Version}\n' \
  ros-humble-rclpy ros-humble-rmw-fastrtps-cpp ros-humble-rosidl-default-generators
python tests/ros_observer_runtime.py
