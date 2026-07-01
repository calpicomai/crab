#!/usr/bin/env bash
# Set up the brain (Jetson) node's virtual environment and dependencies.
#
# Run from anywhere:
#   bash brain/setup.sh
#
# Creates brain/.venv (which sidesteps the PEP 668 "externally-managed-environment"
# error) and installs brain/requirements.txt into it.
#
# The venv is created WITH --system-site-packages on purpose: on the Jetson,
# JetPack provides OpenCV-with-GStreamer (for the CSI camera) and the CUDA torch
# wheel in the SYSTEM Python, and an isolated venv cannot see them (perception
# then silently falls back to synthetic frames + the dummy detector). This lets
# the venv import those system libs while our pinned deps install into the venv.
#
# Perception's own deps (ultralytics for YOLO, and the NanoOWL stack) are
# JetPack-specific and installed separately — see brain/requirements-perception.txt.
set -euo pipefail

# Directory this script lives in (brain/), regardless of the caller's cwd.
NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"

echo "Creating venv at $VENV (with --system-site-packages for Jetson cv2/torch) ..."
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$NODE_DIR/requirements.txt"

# Report which perception libs the venv can see (informational; perception falls
# back to simulate/dummy for whichever are missing).
echo "Perception library visibility:"
"$VENV/bin/python" - <<'PY'
for mod in ("cv2", "torch", "ultralytics"):
    try:
        __import__(mod)
        print(f"  {mod}: OK")
    except Exception as exc:
        print(f"  {mod}: MISSING ({type(exc).__name__})")
PY

cat <<EOF

Brain node ready.

Run the Stage 1 movement test from the repo root (uses brain/config.py,
default target picrawler.local:8000):
    cd "$REPO_ROOT"
    brain/.venv/bin/python -m brain.test_movement

Override the robot address without editing code:
    ROBOT_HOST=192.168.1.42 brain/.venv/bin/python -m brain.test_movement

Perception (camera + detection): install its deps, then run the server:
    brain/.venv/bin/pip install -r brain/requirements-perception.txt
    brain/.venv/bin/python -m brain.perception.server   # then curl localhost:8100/snapshot
EOF
