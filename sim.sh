#!/usr/bin/env bash
# Try the whole robot with NO hardware — one command.
#
#   bash sim.sh                 # default 'poles' scenario
#   bash sim.sh corridor        # pick a scenario (poles/room/corridor/slalom)
#
# Starts the world-backed robot server + the pet in simulation, then open the
# live dashboard at http://localhost:8000/sim (click the map to drop obstacles).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"
mkdir -p brain/.run

# Prefer a node venv (has fastapi/uvicorn/httpx/pillow); fall back to python3.
PY="$REPO_ROOT/brain/.venv/bin/python"
[ -x "$PY" ] || PY="$REPO_ROOT/robot/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

SCEN=""
if [ "${1:-}" != "" ] && [[ "$1" != --* ]]; then SCEN="$1"; shift; fi
if [ -z "$SCEN" ] && [ -t 0 ]; then read -r -p "Scenario (poles/room/corridor/slalom) [poles]: " SCEN 2>&1; fi
SCEN="${SCEN:-poles}"

echo "Starting the simulated robot (scenario: $SCEN) ..."
PICRAWLER_SIMULATE=1 PICRAWLER_SIM_WORLD=1 PICRAWLER_SIM_SCENARIO="$SCEN" \
  "$PY" -m robot.server >"brain/.run/sim-server.log" 2>&1 &
SRV=$!
trap 'kill "$SRV" 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 30); do
  curl -fsS --max-time 2 http://localhost:8000/health >/dev/null 2>&1 && break; sleep 0.5
done
if ! curl -fsS --max-time 2 http://localhost:8000/health >/dev/null 2>&1; then
  echo "sim server didn't start — see brain/.run/sim-server.log" >&2
  exit 1
fi

cat <<EOF
=========================================================
  Simulated PiCrawler is running.
  Open the dashboard:   http://localhost:8000/sim
  (click the map to drop obstacles; Ctrl+C to stop)
=========================================================
EOF

ROBOT_HOST=localhost "$PY" -m brain.pet --base-url http://localhost:8000 --sim --dashboard ${@+"$@"}
