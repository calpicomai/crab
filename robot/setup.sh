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
# by SunFounder's installer, and an isolated venv cannot see them (you'd get
# `ModuleNotFoundError: No module named 'picrawler'` and the GaitEngine would
# silently fall back to simulate mode even on the real robot). --system-site-packages
# lets the venv import those system libs while our pinned deps still install into
# the venv. If robot_hat/picrawler are missing entirely, GaitEngine simulates.
set -euo pipefail

# Directory this script lives in (robot/), regardless of the caller's cwd.
NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"

echo "Creating venv at $VENV (with --system-site-packages for picrawler/robot_hat) ..."
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$NODE_DIR/requirements.txt"

# Report whether the venv can see the SunFounder hardware libs. If not, the
# server will run in simulate mode (no servos move).
if "$VENV/bin/python" -c "import picrawler, robot_hat" 2>/dev/null; then
    echo "picrawler/robot_hat visible in the venv — real servo control available."
else
    echo "WARNING: picrawler/robot_hat NOT importable in the venv."
    echo "  The server will run in SIMULATE mode. Install SunFounder's picrawler/"
    echo "  robot_hat into the system Python (their installer), then re-run this script."
fi

cat <<EOF

Robot node ready.

Run the command server from the repo root (so 'import shared' resolves):
    cd "$REPO_ROOT"
    robot/.venv/bin/python -m robot.server

Bench-test without moving servos:
    cd "$REPO_ROOT"
    PICRAWLER_SIMULATE=1 robot/.venv/bin/python -m robot.server
EOF
