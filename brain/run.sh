#!/usr/bin/env bash
# The easy button for the Jetson brain. Just:
#
#   bash brain/run.sh
#
# First run asks a few questions (with sensible defaults — press Enter) and saves
# them to crab.env, so you never retype flags/addresses. After that it shows a
# small menu: pet / wander / agent / check. It wires up perception + the VLM for
# you and degrades gracefully (no VLM -> canned voice; no perception -> sonar +
# reflex only).
#
# Power users can skip the menu:  bash brain/run.sh pet -- --goal "explore"
set -euo pipefail

NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"
CONF="$REPO_ROOT/crab.env"
RUN_DIR="$NODE_DIR/.run"
PY="$VENV/bin/python"

[ -x "$PY" ] || { echo "brain/.venv not found — run 'bash brain/setup.sh' first." >&2; exit 1; }
cd "$REPO_ROOT"
mkdir -p "$RUN_DIR"

# --- split off passthrough args (everything after `--`) and an optional mode --
MODE=""
EXTRA=()
if [ "${1:-}" != "" ] && [[ "${1}" != "--" ]]; then MODE="$1"; shift || true; fi
if [ "${1:-}" == "--" ]; then shift; EXTRA=("$@"); fi

# ------------------------------------------------------------------ helpers
ask() {  # ask "Prompt" "default" -> echoes answer (prompt to stderr so $() is clean)
  local p="$1" d="$2" v; read -r -p "$p [$d]: " v; echo "${v:-$d}"; }
askyn() { local p="$1" d="$2" v; read -r -p "$p (y/n) [$d]: " v; v="${v:-$d}"; [[ "$v" =~ ^[Yy] ]] && echo 1 || echo 0; }

wizard() {
  { echo; echo "First-time setup (saved to crab.env — press Enter for the default):"; } >&2
  local host name voice model dash vlm
  host="$(ask '  Robot address (Pi hostname or IP)' "${ROBOT_HOST:-picrawler.local}")"
  name="$(ask '  Name your pet (blank = it names itself)' "${PET_NAME:-}")"
  voice="$(askyn '  Speak out loud (Piper TTS, plays on the Pi)?' n)"
  model=""
  if [ "$voice" = "1" ]; then model="$(ask '    Piper voice model path (.onnx)' "${PET_VOICE_MODEL:-}")"; fi
  stt="$(askyn '  Listen for spoken commands (mic on the Pi -> Whisper)?' y)"
  dash="$(askyn '  Show the live dashboard at /sim?' y)"
  vlm="$(ask '  VLM server URL (blank = none, canned voice)' "${LLM_BASE_URL:-}")"
  cat > "$CONF" <<EOF
# crab per-rig preferences (git-ignored). Re-run: bash brain/run.sh reconfigure
ROBOT_HOST="$host"
PET_NAME="$name"
PET_VOICE="$voice"
PET_VOICE_MODEL="$model"
PET_STT="$stt"
PET_DASHBOARD="$dash"
LLM_BASE_URL="$vlm"
EOF
  echo "  saved $CONF" >&2
}

url_ok() { curl -fsS --max-time 3 "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ config
[ "$MODE" = "reconfigure" ] && { wizard; MODE=""; }
[ -f "$CONF" ] || wizard
# shellcheck disable=SC1090
source "$CONF"
export ROBOT_HOST PET_VOICE_MODEL
[ -n "${LLM_BASE_URL:-}" ] && export LLM_BASE_URL
BASE="http://${ROBOT_HOST}:${ROBOT_PORT:-8000}"

# ------------------------------------------------------------------ menu
if [ -z "$MODE" ]; then
  {
    echo; echo "What should ${PET_NAME:-your pet} do?"
    echo "  1) pet        — the creature (default)"
    echo "  2) wander     — plain reactive avoidance"
    echo "  3) agent      — VLM free-roam + narrate"
    echo "  4) check      — readiness checklist, no movement"
    echo "  5) reconfigure"
  } >&2
  read -r -p "pick [1]: " pick
  case "${pick:-1}" in
    1|"") MODE=pet ;; 2) MODE=wander ;; 3) MODE=agent ;;
    4) MODE=check ;; 5) wizard; source "$CONF"; MODE=pet ;;
    *) MODE=pet ;;
  esac
fi

# ------------------------------------------------------------------ preflight
echo "Checking the robot at $BASE ..."
if ! url_ok "$BASE/health"; then
  echo "  ✗ can't reach the robot. Is 'bash robot/run.sh' running on the Pi, and is" >&2
  echo "    ROBOT_HOST correct in crab.env (currently '$ROBOT_HOST')? Try its IP." >&2
  exit 1
fi
echo "  ✓ robot up"

# ------------------------------------------------------------------ check mode
if [ "$MODE" = "check" ]; then
  echo "Readiness (no motion):"
  url_ok "$BASE/health"          && echo "  ✓ robot server"        || echo "  ✗ robot server"
  url_ok "$BASE/camera/frame"    && echo "  ✓ camera frame"        || echo "  ✗ camera frame"
  if curl -fsS --max-time 3 "$BASE/health" 2>/dev/null | grep -q '"audio":{'; then
    echo "  ✓ audio device (Pi mic + speaker)"
  else
    echo "  – audio device off (no mic/speaker)"
  fi
  url_ok "http://localhost:8100/health" && echo "  ✓ perception (:8100)" || echo "  – perception not running (optional)"
  if [ -n "${LLM_BASE_URL:-}" ] && url_ok "${LLM_BASE_URL%/}/models"; then
    echo "  ✓ VLM ($LLM_BASE_URL)"
  else
    echo "  – no VLM (pet uses its canned voice)"
  fi
  dist="$(curl -fsS --max-time 3 -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/status" 2>/dev/null \
          | sed -n 's/.*"distance_cm":\([0-9.]*\).*/\1/p')"
  [ -n "$dist" ] && echo "  ✓ ultrasonic reads ${dist} cm" || echo "  – ultrasonic: no reading"
  exit 0
fi

# ------------------------------------------------------------------ perception (optional, best-effort)
PERC_PID=""
cleanup() { [ -n "$PERC_PID" ] && kill "$PERC_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
if ! url_ok "http://localhost:8100/health"; then
  echo "Starting perception (camera detectors) in the background ..."
  "$PY" -m brain.perception.server >"$RUN_DIR/perception.log" 2>&1 &
  PERC_PID=$!
  for _ in $(seq 1 20); do url_ok "http://localhost:8100/health" && break; sleep 0.5; done
  url_ok "http://localhost:8100/health" \
    && echo "  ✓ perception up" \
    || echo "  – perception didn't start (see $RUN_DIR/perception.log) — running on sonar + reflex"
else
  echo "  ✓ perception already running";
fi

# ------------------------------------------------------------------ VLM detect
HAS_VLM=0
if [ -n "${LLM_BASE_URL:-}" ] && url_ok "${LLM_BASE_URL%/}/models"; then
  HAS_VLM=1; echo "  ✓ VLM found at $LLM_BASE_URL — real voice"
else
  echo "  – no VLM reachable — canned voice"
fi

# ------------------------------------------------------------------ launch
# Run the loop in the FOREGROUND (not exec) so the cleanup trap can stop the
# perception child we started when the loop exits / you Ctrl+C.
FLAGS=()
case "$MODE" in
  pet)
    [ -n "${PET_NAME:-}" ] && FLAGS+=(--name "$PET_NAME")
    [ "${PET_VOICE:-0}" = "1" ] && FLAGS+=(--voice)
    [ "${PET_STT:-1}" = "0" ] && FLAGS+=(--no-stt)   # spoken commands off
    [ "${PET_DASHBOARD:-1}" = "1" ] && FLAGS+=(--dashboard)
    [ "$HAS_VLM" = "0" ] && FLAGS+=(--sim)   # canned inner voice when no VLM
    echo; echo "Launching pet ${FLAGS[*]} ${EXTRA[*]:-}"
    "$PY" -m brain.pet "${FLAGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
    ;;
  wander)
    echo; echo "Launching wander ${EXTRA[*]:-}"
    "$PY" -m brain.wander ${EXTRA[@]+"${EXTRA[@]}"}
    ;;
  agent)
    [ "$HAS_VLM" = "0" ] && FLAGS+=(--sim)
    echo; echo "Launching agent ${FLAGS[*]} ${EXTRA[*]:-}"
    "$PY" -m brain.agent.loop "${FLAGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
    ;;
  *)
    echo "Unknown mode '$MODE' (expected pet|wander|agent|check|reconfigure)." >&2
    exit 2 ;;
esac
