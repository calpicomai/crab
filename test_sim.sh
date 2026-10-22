#!/usr/bin/env bash
# Development-only validation. Never connects to a configured physical robot.
# All servers bind loopback on dedicated ports; all memories live in a temp dir.
# Usage: bash test_sim.sh [--unit|--quick]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"
PY="$REPO_ROOT/brain/.venv/bin/python"
[ -x "$PY" ] || PY="$REPO_ROOT/robot/.venv/bin/python"
[ -x "$PY" ] || { echo 'Run setup.sh to create the project venvs.' >&2; exit 1; }
TEST_TMP="$(mktemp -d)"
export PET_HOME="$TEST_TMP/pet"
export PET_MEMORY_DB="$PET_HOME/memory.db"
export PET_IDENTITY_FILE="$PET_HOME/identity.json"
export PET_WORLD_DB="$PET_HOME/world.db"
export PERCEPTION_OUTPUT_DIR="$TEST_TMP/perception"
SRV_PID=""
PET_PID=""
cleanup() {
  [ -z "$PET_PID" ] || kill "$PET_PID" 2>/dev/null || true
  [ -z "$PET_PID" ] || wait "$PET_PID" 2>/dev/null || true
  [ -z "$SRV_PID" ] || kill "$SRV_PID" 2>/dev/null || true
  [ -z "$SRV_PID" ] || wait "$SRV_PID" 2>/dev/null || true
  rm -rf "$TEST_TMP"
}
trap cleanup EXIT INT TERM

"$PY" -m unittest discover -s tests -p 'test_*.py'
PERCEPTION_SIMULATE=1 "$PY" -m brain.test_offbody
"$PY" -m brain.pet.scenarios --all --output "$TEST_TMP/scenarios.json"
[ "${1:-}" != '--unit' ] || exit 0

# Select an unused loopback port without touching the user's robot configuration.
TEST_PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
TEST_URL="http://127.0.0.1:$TEST_PORT"
PICRAWLER_HOST=127.0.0.1 PICRAWLER_PORT="$TEST_PORT" PICRAWLER_SIMULATE=1 \
  PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_SIM_WORLD=1 PICRAWLER_SIM_SCENARIO=room \
  PICRAWLER_HOME_ON_START=none "$PY" -m robot.server >"$TEST_TMP/server.log" 2>&1 &
SRV_PID=$!
READY=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 1 "$TEST_URL/health" >/dev/null 2>&1; then READY=1; break; fi
  kill -0 "$SRV_PID" 2>/dev/null || break
  sleep 0.1
done
if [ "$READY" != 1 ]; then cat "$TEST_TMP/server.log" >&2; exit 1; fi

"$PY" -m brain.test_movement --steps 1 --base-url "$TEST_URL"
"$PY" - "$TEST_URL" <<'PY'
import json, sys, urllib.request
url = sys.argv[1]
health = json.load(urllib.request.urlopen(url + '/health'))
assert health['simulate'] is True, health
state = json.load(urllib.request.urlopen(url + '/sim/state'))
assert state['enabled'] is True, state
with urllib.request.urlopen(url + '/camera/frame') as response:
    assert response.headers.get_content_type() == 'image/jpeg'
    assert response.read().startswith(b'\xff\xd8')
print('HTTP simulator health, virtual body and explicit synthetic camera passed.')
PY
if [ "${1:-}" != '--quick' ]; then
  "$PY" -m brain.pet --sim --duration 7 --no-stt --no-voice --no-camera --dashboard \
    --base-url "$TEST_URL" --log "$TEST_TMP/pet.jsonl" >"$TEST_TMP/pet.log" 2>&1 &
  PET_PID=$!
  "$PY" - "$TEST_URL" <<'PYTEST'
import json, sys, time, urllib.request
url = sys.argv[1]
def get():
    return json.load(urllib.request.urlopen(url + '/sim/state'))['brain']
def send(text):
    req=urllib.request.Request(url+'/companion/events', data=json.dumps({'text': text}).encode(),
                               headers={'Content-Type': 'application/json'})
    assert json.load(urllib.request.urlopen(req))['ok']
def until(predicate, message):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        state=get()
        if predicate(state): return state
        time.sleep(.08)
    raise AssertionError(message+': '+str(get()))
until(lambda b: b.get('inputs'), 'companion telemetry unavailable')
send('hello')
until(lambda b: any(i['kind']=='acknowledge' for i in b.get('intentions',[])), 'greeting not acknowledged')
send('good crab, stop')
until(lambda b: b.get('stopped'), 'stop did not override praise')
send('resume')
until(lambda b: b.get('stopped') is False, 'explicit resume failed')
print('Live simulated companion greeting, priority stop and resume passed.')
PYTEST
  wait "$PET_PID"
  PET_PID=""
fi
printf '%s\n' 'Simulation validation passed. Hardware motion, audio and Jetson inference remain pending.'
