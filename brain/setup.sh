#!/usr/bin/env bash
# Set up the brain (Jetson) node's virtual environment and dependencies.
#
# Run from anywhere:
#   bash brain/setup.sh
#
# Creates brain/.venv (isolated from the system Python, so it sidesteps the
# PEP 668 "externally-managed-environment" error) and installs
# brain/requirements.txt into it. Later stages add perception / STT / TTS / LLM
# deps that assume CUDA/JetPack; those are installed in their own stages.
set -euo pipefail

# Directory this script lives in (brain/), regardless of the caller's cwd.
NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"

echo "Creating venv at $VENV ..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$NODE_DIR/requirements.txt"

cat <<EOF

Brain node ready.

Run the Stage 1 movement test from the repo root (uses brain/config.py,
default target picrawler.local:8000):
    cd "$REPO_ROOT"
    brain/.venv/bin/python -m brain.test_movement

Override the robot address without editing code:
    ROBOT_HOST=192.168.1.42 brain/.venv/bin/python -m brain.test_movement
EOF
