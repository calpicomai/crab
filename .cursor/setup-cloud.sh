#!/usr/bin/env bash
# Idempotent cloud-agent install — run from repo root on every agent start.
#
# Builds robot/.venv and brain/.venv (simulate mode; no Pi/Jetson hardware).
# Jetson-only scripts (setup_perception.sh, setup_agent.sh, setup_voice.sh) are
# intentionally skipped — they need CUDA/Ollama/Piper binaries.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ensure_venv() {
  local node_dir="$1"
  local reqs="$2"
  local venv="$node_dir/.venv"

  if [ ! -x "$venv/bin/python" ]; then
    echo "Creating $venv ..."
    python3 -m venv --system-site-packages "$venv"
  fi
  "$venv/bin/pip" install --upgrade pip -q
  "$venv/bin/pip" install -r "$reqs" -q
}

ensure_venv robot "$REPO_ROOT/robot/requirements.txt"
ensure_venv brain "$REPO_ROOT/brain/requirements.txt"
brain/.venv/bin/pip install -r brain/requirements-perception.txt -r brain/requirements-agent.txt -q

echo "Cloud environment ready (simulate mode)."
echo "  Smoke test: bash sim.sh poles"
echo "  Dashboard:  http://localhost:8000/sim  (with sim server + --dashboard)"
