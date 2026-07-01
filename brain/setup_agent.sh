#!/usr/bin/env bash
# Set up the LLM agent brain (brain/agent/) on the Jetson.
#
# Run from anywhere:
#   bash brain/setup_agent.sh
#
# Installs the agent's Python deps (just the openai SDK — a thin client) into
# brain/.venv. The MODEL SERVER is separate: the agent talks the OpenAI-compatible
# chat API to a LOCAL llama.cpp `llama-server` running a small multimodal model
# (VLM). Nothing here calls the cloud.
#
# You still need, once, on the Jetson (not automated — versions/paths vary):
#   1. Build/install llama.cpp with CUDA + multimodal (llama-server + libmtmd).
#   2. Fetch a small VLM in GGUF form + its mmproj (vision) file, e.g. a
#      Qwen2.5-VL-3B-Instruct GGUF (Q4) + mmproj. SmolVLM2-2.2B is a lighter
#      fallback if RAM/latency is tight.
#   3. Serve it (bind to the LAN so the brain can reach it; here it's local):
#        llama-server --host 0.0.0.0 --port 8080 \
#            -m qwen2.5-vl-3b-instruct-q4_k_m.gguf \
#            --mmproj mmproj-qwen2.5-vl-3b-f16.gguf
#
# RAM (8GB shared): the VLM does the "seeing", so the agent unloads the YOLO/
# NanoOWL detectors on startup to free RAM (AGENT_FREE_PERCEPTION_RAM=1). Expect
# ~seconds per decision on an Orin Nano — that's fine, because the robot's fast
# reflex + costmap own real-time collision safety; the LLM only sets intent.
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

cat <<EOF

Agent brain deps ready.

1) Start a local model server (llama.cpp) with a VLM, e.g.:
     llama-server --host 0.0.0.0 --port 8080 \\
         -m qwen2.5-vl-3b-instruct-q4_k_m.gguf --mmproj mmproj-qwen2.5-vl-3b-f16.gguf

2) Run the agent from the repo root (robot + camera reachable; robot elevated):
     cd "$REPO_ROOT"
     brain/.venv/bin/python -m brain.agent.loop                    # free-roam + narrate
     brain/.venv/bin/python -m brain.agent.loop --goal "find a person"

Swap backends without code changes (still local):
     LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen2.5-vl ...   # Ollama
     LLM_MULTIMODAL=0 LLM_MODEL=qwen2.5-3b-instruct ...               # text-only model

Test the whole loop off-GPU (canned policy, no model needed):
     brain/.venv/bin/python -m brain.agent.loop --sim --max-ticks 5
EOF
