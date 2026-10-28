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
# wheel in the SYSTEM Python. This exposes those system libraries to the venv;
# missing capture/models remain unavailable, never implicitly simulated.
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

# Import visibility only, not inference or physical acceptance.
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

Python dependency setup finished; device/runtime acceptance remains open.
Next: run passive local diagnostics, then follow docs/commissioning.md.
This report starts no companion, capture, playback or control connection:
    cd "$REPO_ROOT"
    bash brain/run.sh check -- --offline

Models, ROS, audio/microphone, perception and physical movement require separate
provisioning/acceptance. No driver, power-mode or service changes are made here.
EOF
