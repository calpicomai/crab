#!/usr/bin/env bash
# Non-interactive smoke test for off-hardware / cloud CI.
#
# Exercises the simulate stack end-to-end: costmap self-test, dummy perception,
# sim-world robot server, movement link, pet + agent canned loops.
#
#   bash test_sim.sh              # run all checks (exit 0 = pass)
#   bash test_sim.sh --quick      # skip pet/agent loops (faster)
#
# Requires: bash .cursor/setup-cloud.sh  (or robot/setup.sh + brain/setup.sh)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"
mkdir -p brain/.run

PY="$REPO_ROOT/brain/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$REPO_ROOT/robot/.venv/bin/python"
fi
if [ ! -x "$PY" ]; then
  echo "ERROR: no venv found — run: bash .cursor/setup-cloud.sh" >&2
  exit 1
fi

QUICK=0
if [ "${1:-}" = "--quick" ]; then QUICK=1; fi

PASS=0
FAIL=0
SRV_PID=""
PERC_PID=""

pass() { echo "  ok  $*"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL $*" >&2; FAIL=$((FAIL + 1)); }

cleanup() {
  [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null || true
  [ -n "$PERC_PID" ] && kill "$PERC_PID" 2>/dev/null || true
  wait "$SRV_PID" 2>/dev/null || true
  wait "$PERC_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_url() {
  local url="$1" tries="${2:-40}"
  for _ in $(seq 1 "$tries"); do
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  return 1
}

echo "== PiCrawler simulate smoke test =="
echo "python: $PY"
echo

# --- Off-network unit checks -----------------------------------------------
echo "[1/6] costmap self-test"
if "$PY" -m brain.costmap >/dev/null 2>&1; then
  pass "brain.costmap assertions"
else
  fail "brain.costmap self-test"
fi

echo "[2/6] perception (dummy, in-process)"
if PERCEPTION_SIMULATE=1 "$PY" -m brain.test_perception --frames 1 --backend dummy >/dev/null 2>&1; then
  pass "brain.test_perception dummy backend"
else
  fail "brain.test_perception"
fi

# --- Sim-world robot server ------------------------------------------------
echo "[3/6] sim-world robot server"
LOG="$REPO_ROOT/brain/.run/test-sim-server.log"
PICRAWLER_SIMULATE=1 PICRAWLER_SIM_WORLD=1 PICRAWLER_SIM_SCENARIO=poles \
  "$PY" -m robot.server >"$LOG" 2>&1 &
SRV_PID=$!

if wait_url http://localhost:8000/health; then
  pass "robot server /health"
else
  fail "robot server did not start (see $LOG)"
  tail -20 "$LOG" >&2 || true
fi

if curl -fsS --max-time 5 http://localhost:8000/health | grep -q '"simulate":true'; then
  pass "health reports simulate:true"
else
  fail "health missing simulate:true"
fi

if curl -fsS --max-time 5 http://localhost:8000/sim/state | grep -q '"enabled":true'; then
  pass "sim world /sim/state"
else
  fail "/sim/state unavailable (is PICRAWLER_SIM_WORLD=1 set?)"
fi

CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8000/camera/frame)"
if [ "$CODE" = "200" ]; then
  pass "camera /camera/frame"
else
  fail "camera frame HTTP $CODE"
fi

# --- Movement link ---------------------------------------------------------
echo "[4/6] movement link"
if ROBOT_HOST=localhost "$PY" -m brain.test_movement --base-url http://localhost:8000 >/dev/null 2>&1; then
  pass "brain.test_movement stand/walk/sit"
else
  fail "brain.test_movement"
fi

# --- Perception HTTP server (dummy) ----------------------------------------
echo "[5/6] perception HTTP server"
PERCEPTION_SIMULATE=1 "$PY" -m brain.perception.server >"$REPO_ROOT/brain/.run/test-perception.log" 2>&1 &
PERC_PID=$!
if wait_url http://localhost:8100/health; then
  pass "perception server /health"
  if curl -fsS --max-time 10 http://localhost:8100/snapshot | grep -q '"backends"'; then
    pass "perception /snapshot"
  else
    fail "perception /snapshot"
  fi
else
  fail "perception server did not start"
fi
kill "$PERC_PID" 2>/dev/null || true
wait "$PERC_PID" 2>/dev/null || true
PERC_PID=""

# --- Brain loops (canned / --sim) ------------------------------------------
if [ "$QUICK" -eq 0 ]; then
  echo "[6/6] pet + agent canned loops"
  if "$PY" -m brain.pet --sim --max-ticks 8 --no-camera --base-url http://localhost:8000 >/dev/null 2>&1; then
    pass "brain.pet --sim"
  else
    fail "brain.pet --sim"
  fi
  if "$PY" -m brain.agent.loop --sim --max-ticks 2 --base-url http://localhost:8000 >/dev/null 2>&1; then
    pass "brain.agent.loop --sim"
  else
    fail "brain.agent.loop --sim"
  fi
else
  echo "[6/6] skipped (--quick)"
fi

echo
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "Smoke test FAILED." >&2
  exit 1
fi
echo "Smoke test OK — simulate stack is healthy."
exit 0
