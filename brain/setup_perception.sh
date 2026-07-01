#!/usr/bin/env bash
# Install the Jetson perception dependencies the RIGHT way. Run after brain/setup.sh.
#
#   bash brain/setup_perception.sh
#   TORCH_INDEX_URL=https://pypi.jetson-ai-lab.dev/jp6/cu126 bash brain/setup_perception.sh
#
# The camera is on the ROBOT (Pi), streamed to the Jetson as MJPEG, so the Jetson
# does NOT need OpenCV-with-GStreamer — it decodes frames with Pillow. It only
# needs torch (for the detectors) + ultralytics (YOLO). torch must be NVIDIA's
# Jetson wheel (the PyPI wheel has no CUDA), which is why this isn't a plain
# requirements.txt; ultralytics is pip-safe once torch is present (so it can't
# pull a CPU torch). ultralytics brings opencv-python for inference — fine, the
# camera pipeline itself lives on the Pi.
set -euo pipefail

NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$NODE_DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

if [ ! -x "$PY" ]; then
    echo "ERROR: $VENV not found. Run 'bash brain/setup.sh' first (it creates the"
    echo "venv with --system-site-packages so it can see the Jetson's system libs)."
    exit 1
fi

# 1) Base perception deps (server + simulate path) — safe to pip anywhere.
echo "== Installing base perception deps =="
"$PIP" install -r "$NODE_DIR/requirements-perception.txt"

# 2) PyTorch — MUST be the NVIDIA Jetson wheel (CUDA), never PyPI. If you pass
#    TORCH_INDEX_URL we install from it; otherwise we require torch already
#    present and print guidance rather than pulling a broken CPU build.
echo "== PyTorch (Jetson) =="
TORCH_INDEX_URL="${TORCH_INDEX_URL:-}"
if [ -n "$TORCH_INDEX_URL" ] && ! "$PY" -c "import torch" 2>/dev/null; then
    echo "Installing torch/torchvision from $TORCH_INDEX_URL ..."
    "$PIP" install --index-url "$TORCH_INDEX_URL" torch torchvision
fi

# import_err: print nothing + succeed if torch imports; else emit the traceback.
torch_import() { "$PY" -c "import torch" 2>&1; }

# The jetson-ai-lab torch wheels (>=2.8) link cuDSS, which JetPack 6.2 doesn't
# ship -> "libcudss.so.0: cannot open shared object file". Provide it via the pip
# wheel WITHOUT deps, so it doesn't pull CUDA 12.9 libs that would shadow the
# system CUDA 12.6 (nvidia-cublas/cuda-runtime/cusparse/nvjitlink-cu12).
if ! "$PY" -c "import torch" 2>/dev/null; then
    err="$(torch_import || true)"
    if printf '%s' "$err" | grep -q "libcudss"; then
        echo "torch needs cuDSS (libcudss.so.0); installing nvidia-cudss-cu12 (--no-deps) ..."
        "$PIP" install --no-deps nvidia-cudss-cu12
    fi
fi

# Final check — surface the REAL error instead of masking it.
if "$PY" -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"; then
    :
else
    echo "----------------------------------------------------------------------"
    echo "torch is installed but failed to import. Full error:"
    torch_import || true
    cat <<'MSG'
----------------------------------------------------------------------
Common JetPack 6.2 causes:
  * libcudart.so.12 / libcublasLt.so missing -> system CUDA not installed:
        sudo apt-get install -y nvidia-jetpack
  * libcudss.so.0 missing (torch >= 2.8) -> this script installs it, but if the
    wheel was unavailable: brain/.venv/bin/pip install --no-deps nvidia-cudss-cu12
If torch was never installed, pass the jetson-ai-lab index (JetPack 6.2 = cu126):
    TORCH_INDEX_URL=https://pypi.jetson-ai-lab.io/jp6/cu126 bash brain/setup_perception.sh
MSG
    exit 1
fi

# 3) Ultralytics (YOLO). torch is present now, so pip won't reinstall it.
echo "== Ultralytics (YOLO) =="
"$PIP" install ultralytics
"$PY" -c "import ultralytics; print('ultralytics', ultralytics.__version__)"

# NanoOWL (open-vocabulary) stays manual — it needs torch2trt + a built TensorRT
# engine; see brain/requirements-perception.txt.

echo
echo "Perception deps ready. Start the server:"
echo "    $PY -m brain.perception.server   # then curl localhost:8100/snapshot"
