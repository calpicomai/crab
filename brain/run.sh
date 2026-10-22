#!/usr/bin/env bash
# Local readiness and stationary companionship. No automatic model/service/power changes.
set -euo pipefail
NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
PY="$NODE_DIR/.venv/bin/python"
CONF="$REPO_ROOT/crab.env"
[ -x "$PY" ] || { echo "Run bash brain/setup.sh to create the Python environment." >&2; exit 1; }
cd "$REPO_ROOT"
export PATH="$NODE_DIR/.piper/piper:$NODE_DIR/.venv/bin:$PATH"
MODE="${1:-}"
[ "$#" -eq 0 ] || shift
[ "${1:-}" != "--" ] || shift
if [ -f "$CONF" ]; then
  set -a
  source "$CONF"
  set +a
fi
if [ "$MODE" = reconfigure ]; then
  [ ! -f "$CONF" ] || cp -p "$CONF" "$CONF.backup-$(date +%Y%m%d%H%M%S)"
  read -r -p "Pi hostname [${ROBOT_HOST:-picrawler.local}]: " value
  ROBOT_HOST="${value:-${ROBOT_HOST:-picrawler.local}}"
  read -r -p "Companion name [${PET_NAME:-Crab}]: " value
  PET_NAME="${value:-${PET_NAME:-Crab}}"
  # Shell-safe values; preserve all unrelated preferences in existing configuration.
  printf '\n# Companion preferences\nROBOT_HOST=%q\nPET_NAME=%q\n' "$ROBOT_HOST" "$PET_NAME" >> "$CONF"
  export ROBOT_HOST PET_NAME
  MODE=check
fi
if [ -z "$MODE" ]; then
  if [ -t 0 ]; then
    echo "1) interaction   2) quiet companionship   3) readiness (no motion/sound)"
    read -r -p "Choose [1]: " choice
    case "${choice:-1}" in 2) MODE=quiet;; 3) MODE=check;; *) MODE=pet;; esac
  else
    MODE=pet
  fi
fi
case "$MODE" in
  check|readiness) exec "$PY" -m brain.pet.readiness "$@" ;;
  pet|interaction|quiet)
    FLAGS=(--mode interaction)
    [ "$MODE" != quiet ] || FLAGS=(--mode quiet)
    [ -z "${PET_NAME:-}" ] || FLAGS+=(--name "$PET_NAME")
    [ "${PET_DASHBOARD:-1}" != 1 ] || FLAGS+=(--dashboard)
    [ "${PET_VOICE:-0}" != 1 ] || FLAGS+=(--voice)
    [ "${PET_STT:-1}" != 0 ] || FLAGS+=(--no-stt)
    # Models are provisioned/started separately after measuring the Jetson budget.
    [ -n "${LLM_BASE_URL:-}" ] || FLAGS+=(--no-llm)
    exec "$PY" -m brain.pet "${FLAGS[@]}" "$@" ;;
  wander|agent)
    echo "Legacy navigation is simulation-only and is not a companion mode. Use pet, quiet or check." >&2
    exit 2 ;;
  *) echo "Modes: pet | quiet | check | reconfigure. Supervised exploration is planned." >&2; exit 2 ;;
esac
