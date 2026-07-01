#!/usr/bin/env bash
# Set up the robot (Raspberry Pi) node's virtual environment and dependencies.
#
# Run from anywhere:
#   bash robot/setup.sh
#
# Creates robot/.venv (isolated from the system Python, so it sidesteps the
# PEP 668 "externally-managed-environment" error on Raspberry Pi OS Bookworm)
# and installs robot/requirements.txt into it.
#
# The SunFounder hardware libraries (robot_hat, picrawler) are installed
# separately by the SunFounder installer on the Pi; the GaitEngine falls back to
# simulate mode if they are missing.
set -euo pipefail

# Directory this script lives in (robot/), regardless of the caller's cwd.
NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"

echo "Creating venv at $VENV ..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$NODE_DIR/requirements.txt"

cat <<EOF

Robot node ready.

Run the command server from the repo root (so 'import shared' resolves):
    cd "$REPO_ROOT"
    robot/.venv/bin/python -m robot.server

Bench-test without moving servos:
    cd "$REPO_ROOT"
    PICRAWLER_SIMULATE=1 robot/.venv/bin/python -m robot.server
EOF
