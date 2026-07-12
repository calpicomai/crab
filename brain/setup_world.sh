#!/usr/bin/env bash
# Install deps for the world-model TUI (laptop / off-body teaching).
set -euo pipefail
NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
cd "$REPO_ROOT"
if [ ! -x brain/.venv/bin/python ]; then
  bash brain/setup.sh
fi
brain/.venv/bin/pip install -r brain/requirements-world.txt
echo "Ready: python -m brain.pet.world_tui"
