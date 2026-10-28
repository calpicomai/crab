#!/usr/bin/env bash
# Set up the robot (Raspberry Pi) node's virtual environment and dependencies.
#
# Run from anywhere:
#   bash robot/setup.sh
#
# Creates robot/.venv (which sidesteps the PEP 668 "externally-managed-environment"
# error on Raspberry Pi OS Bookworm) and installs robot/requirements.txt into it.
#
# The venv is created WITH --system-site-packages on purpose: the SunFounder
# hardware libraries (robot_hat, picrawler) are installed into the SYSTEM Python
# by SunFounder's installer, and an isolated venv cannot see them.
# --system-site-packages exposes those system libraries to this environment.
# Missing hardware support remains unavailable; simulation requires explicit
# PICRAWLER_SIMULATE=1. Installation does not establish hardware acceptance.
set -euo pipefail

# Directory this script lives in (robot/), regardless of the caller's cwd.
NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"

echo "Creating venv at $VENV (with --system-site-packages for picrawler/robot_hat) ..."
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$NODE_DIR/requirements.txt"

# Report imports only; this does not construct a crawler or verify hardware.
if "$VENV/bin/python" -c "import picrawler, robot_hat" 2>/dev/null; then
    echo "picrawler/robot_hat imports succeeded; physical readiness remains unverified."
else
    echo "WARNING: picrawler/robot_hat NOT importable in the venv."
    echo "  Hardware capabilities remain unavailable; there is no implicit simulation."
    echo "  Review docs/commissioning.md before any separately approved driver installation."
fi

cat <<EOF

Python dependency setup finished; hardware readiness has not been established.
Next: follow docs/commissioning.md, including storage/configuration checks and
explicit approval before starting the server. The guide provides a profile with
actuators, homing, camera, microphone/speaker and sensor access disabled.

For hardware-free development only, use explicit simulation:
    cd "$REPO_ROOT"
    PICRAWLER_SIMULATE=1 PICRAWLER_HOST=127.0.0.1 PICRAWLER_CAMERA_ENABLED=0 PICRAWLER_AUDIO_ENABLED=0 robot/.venv/bin/python -m robot.server
EOF
