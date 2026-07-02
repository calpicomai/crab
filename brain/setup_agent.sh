#!/usr/bin/env bash
# Set up the LLM agent brain (brain/agent/) on the Jetson.
#
# Run from anywhere:
#   bash brain/setup_agent.sh
#
# Installs the agent's Python deps (the openai SDK — a thin client) into brain/.venv
# AND stands up a LOCAL multimodal model server (Ollama) with a small VLM, so the
# pet/agent has a real mind. The code talks the OpenAI-compatible chat API, so it
# points at Ollama (:11434/v1) with only env vars — no code change, nothing cloud.
#
#   bash brain/setup_agent.sh                 # install deps + Ollama + pull qwen2.5vl:3b
#   OLLAMA_MODEL=qwen2.5:3b bash brain/setup_agent.sh   # a lighter text model instead
#   SETUP_OLLAMA=0 bash brain/setup_agent.sh  # deps only (you run your own llama.cpp)
#
# RAM (8GB shared): the VLM does the "seeing", so the agent unloads the YOLO/NanoOWL
# detectors on startup (AGENT_FREE_PERCEPTION_RAM=1); the pet keeps YOLO for chasing.
# Expect ~seconds per decision on an Orin Nano — fine, because the robot's fast reflex
# + costmap own real-time collision safety; the LLM only sets intent.
set -euo pipefail

NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"

if [ ! -x "$VENV/bin/pip" ]; then
  echo "brain/.venv not found — run 'bash brain/setup.sh' first." >&2
  exit 1
fi

echo "Installing agent deps into $VENV ..."
"$VENV/bin/pip" install -r "$NODE_DIR/requirements-agent.txt"

# ------------------------------------------------------------ local VLM (Ollama)
# Stand up a LOCAL multimodal model server so the pet/agent has a REAL mind. We use
# Ollama: on JetPack it installs a CUDA-enabled systemd service and speaks the
# OpenAI-compatible API the code already targets, so the pet swaps to it with only
# LLM_BASE_URL/LLM_MODEL — no code change, no cloud. Skip with SETUP_OLLAMA=0 (e.g.
# if you run your own llama.cpp `llama-server` — see the note at the end).
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5vl:3b}"   # multimodal, ~3GB Q4; fits the 8GB budget
OLLAMA_URL="http://localhost:11434"
CONF="$REPO_ROOT/crab.env"

if [ "${SETUP_OLLAMA:-1}" = "1" ]; then
  echo
  echo "== Local VLM (Ollama) =="
  if ! command -v ollama >/dev/null 2>&1; then
    echo "Installing Ollama (official installer — needs sudo + network) ..."
    curl -fsSL https://ollama.com/install.sh | sh \
      || echo "  WARNING: Ollama install failed — install it from https://ollama.com/download (or set SETUP_OLLAMA=0), then re-run."
  else
    echo "  ollama already installed ($(ollama --version 2>/dev/null | head -1))."
  fi

  if command -v ollama >/dev/null 2>&1; then
    # The installer usually starts a systemd service; fall back to a background daemon.
    if ! curl -fsS --max-time 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
      sudo systemctl start ollama 2>/dev/null || (ollama serve >/tmp/ollama.log 2>&1 &) || true
      for _ in $(seq 1 30); do curl -fsS --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1 && break; sleep 1; done
    fi
    if curl -fsS --max-time 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
      echo "  Pulling $OLLAMA_MODEL (a few GB; skipped if already present) ..."
      ollama pull "$OLLAMA_MODEL" || echo "  WARNING: pull failed — check disk/network and re-run."
      ollama run "$OLLAMA_MODEL" "reply with one word: ready" >/dev/null 2>&1 \
        && echo "  ✓ $OLLAMA_MODEL is serving at $OLLAMA_URL/v1"
      # If crab.env already exists (you've run brain/run.sh), point it at Ollama so
      # the launcher uses it automatically. (Don't CREATE crab.env here — run.sh's
      # wizard writes the rest of it, e.g. the robot address.)
      if [ -f "$CONF" ] && ! grep -q "11434/v1" "$CONF"; then
        { echo "LLM_BASE_URL=\"$OLLAMA_URL/v1\""; echo "LLM_MODEL=\"$OLLAMA_MODEL\""; } >> "$CONF"
        echo "  pointed $CONF at Ollama (LLM_BASE_URL/LLM_MODEL)."
      fi
    else
      echo "  WARNING: Ollama isn't responding on $OLLAMA_URL — start it (sudo systemctl start ollama) and re-run."
    fi
  fi
fi

cat <<EOF

Agent/pet brain ready.

Run from the repo root (robot + camera reachable; robot elevated):
   cd "$REPO_ROOT"
   bash brain/run.sh                                  # menu -> pet (uses the VLM if up)
   brain/.venv/bin/python -m brain.agent.loop         # or the free-roam agent
   LLM_BASE_URL=$OLLAMA_URL/v1 LLM_MODEL=$OLLAMA_MODEL brain/.venv/bin/python -m brain.pet

RAM (8GB shared): the VLM does the seeing. The agent unloads the detectors; the pet
keeps YOLO for chasing, so free NanoOWL if it's tight. Expect ~seconds/decision — fine,
the Pi reflex owns real-time safety. Lighter option: a text model + scene summary:
   OLLAMA_MODEL=qwen2.5:3b bash brain/setup_agent.sh   then run with LLM_MULTIMODAL=0

Prefer llama.cpp instead of Ollama? Set SETUP_OLLAMA=0, build llama-server with CUDA +
a Qwen2.5-VL-3B GGUF + mmproj, serve on :8080, and set LLM_BASE_URL=http://localhost:8080/v1.

Test the whole loop off-GPU (canned policy, no model needed):
   brain/.venv/bin/python -m brain.agent.loop --sim --max-ticks 5
EOF
