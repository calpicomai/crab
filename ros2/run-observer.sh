#!/usr/bin/env bash
# Explicit optional process only. No installers, saved config, control or capture.
set -eo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "${ROS_DISTRO:-}" != humble ]; then
  echo 'Source /opt/ros/humble/setup.bash and the built crab_observation_msgs overlay first.' >&2
  exit 2
fi
observer_python="${CRAB_OBSERVER_PYTHON:-$repo_root/brain/.venv/bin/python}"
if [ ! -x "$observer_python" ]; then
  echo 'Existing matching Python environment unavailable; this launcher does not install dependencies.' >&2
  exit 2
fi
"$observer_python" -c 'import sys; assert sys.version_info[:2] == (3,10), "Humble binary modules require Python 3.10"; import rclpy, httpx, pydantic; from crab_observation_msgs.msg import ObservationSnapshot' || exit 2
cd "$repo_root"
export PYTHONDONTWRITEBYTECODE=1 ROS_LOCALHOST_ONLY=1
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-173}"
exec "$observer_python" -m brain.ros_observer.node "$@"
