#!/usr/bin/env bash
# Run the PiCrawler command server on the Raspberry Pi — one command.
#
#   bash robot/run.sh
#
# (Run robot/setup.sh once first to build the venv.) For autostart on boot, use
# the systemd unit in robot/systemd/ instead.
set -euo pipefail

NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "robot/.venv not found — run 'bash robot/setup.sh' first." >&2
  exit 1
fi

cd "$REPO_ROOT"  # so `import shared` / `robot` resolve
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"

cat <<EOF
================= PiCrawler robot server =================
 SAFETY: charge the 2S pack and ELEVATE the robot / keep legs clear —
 it homes to 'stand' on startup (PICRAWLER_HOME_ON_START=none to skip).
 Serving on 0.0.0.0:${PICRAWLER_PORT:-8000}${IP:+    (this Pi: ${IP})}
 On the Jetson point the brain at it:  ROBOT_HOST=${IP:-<this-pi-ip>}
 Autostart alternative: robot/systemd/picrawler-server.service
=========================================================
EOF

exec "$VENV/bin/python" -m robot.server
