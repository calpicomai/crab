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
#
# Detector weights are fetched too, so the first /snapshot doesn't stall on a
# download (or need the network at run time):
#   * YOLO (yolov8n.pt) is always pre-cached into the repo's data/ dir.
#   * NanoOWL (open-vocab) is opt-in — it needs torch2trt + TensorRT (JetPack) and
#     a built engine. Enable with `--nanoowl` or SETUP_NANOOWL=1.
#       bash brain/setup_perception.sh --nanoowl
set -euo pipefail

NANOOWL="${SETUP_NANOOWL:-0}"
for arg in "$@"; do
    case "$arg" in
        --nanoowl) NANOOWL=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$NODE_DIR")"
VENV="$NODE_DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"
DATA_DIR="$REPO_ROOT/data"

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
        # The wheel drops libcudss.so.0 inside the venv, which is NOT on the
        # dynamic-loader path — torch's dlopen still can't find it. Register the
        # directory with ldconfig so torch (and the server, and a future systemd
        # unit) can load it without any per-launch LD_LIBRARY_PATH.
        cudss_so="$(find "$VENV" -name 'libcudss.so.0' 2>/dev/null | head -1 || true)"
        if [ -n "$cudss_so" ]; then
            cudss_dir="$(dirname "$cudss_so")"
            echo "Registering cuDSS lib dir on the loader path: $cudss_dir"
            echo "$cudss_dir" | sudo tee /etc/ld.so.conf.d/crab-cudss.conf >/dev/null
            sudo ldconfig
        else
            echo "WARNING: installed nvidia-cudss-cu12 but couldn't find libcudss.so.0 to register."
        fi
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

# 3b) Pre-fetch the YOLO weights so the first /snapshot doesn't block on a download
#     (or fail with no network). Cache into the repo's data/ dir and point the
#     server at it; ultralytics downloads yolov8n.pt (~6MB) on first instantiation.
echo "== YOLO weights =="
mkdir -p "$DATA_DIR"
YOLO_WEIGHTS="${PERCEPTION_YOLO_WEIGHTS:-yolov8n.pt}"
if "$PY" - "$DATA_DIR" "$YOLO_WEIGHTS" <<'PY'
import os, sys, shutil
data_dir, weights = sys.argv[1], sys.argv[2]
dest = os.path.join(data_dir, os.path.basename(weights))
if os.path.exists(dest):
    print("already cached:", dest); sys.exit(0)
from ultralytics import YOLO
os.chdir(data_dir)          # ultralytics downloads a bare name into the CWD
m = YOLO(weights)           # triggers the download if it's a known model name
ckpt = getattr(m, "ckpt_path", None) or os.path.join(data_dir, os.path.basename(weights))
if os.path.abspath(ckpt) != os.path.abspath(dest) and os.path.exists(ckpt):
    shutil.copy(ckpt, dest)
print("cached YOLO weights ->", dest if os.path.exists(dest) else ckpt)
PY
then
    echo "  Set PERCEPTION_YOLO_WEIGHTS=$DATA_DIR/$(basename "$YOLO_WEIGHTS") to use the cached copy"
    echo "  (or just run the server from the repo root; ultralytics also caches by name)."
else
    echo "  WARNING: couldn't pre-fetch YOLO weights; they'll download on first /snapshot instead."
fi

# 4) NanoOWL (open-vocabulary) — OPT-IN (--nanoowl / SETUP_NANOOWL=1). Needs
#    torch2trt + TensorRT (JetPack) and a built image-encoder engine.
if [ "$NANOOWL" = "1" ]; then
    echo "== NanoOWL (open-vocabulary) =="
    "$PIP" install transformers
    # torch2trt + nanoowl from source (into the venv). Idempotent-ish: pip reports
    # "already satisfied" on a rebuild.
    tmp="$(mktemp -d)"
    for repo in torch2trt nanoowl; do
        if ! "$PY" -c "import $repo" 2>/dev/null; then
            echo "-- installing $repo from source --"
            git clone --depth 1 "https://github.com/NVIDIA-AI-IOT/$repo" "$tmp/$repo" \
                && ( cd "$tmp/$repo" && "$PIP" install . ) \
                || echo "  WARNING: $repo install failed (needs TensorRT/JetPack) — see notes below."
        fi
    done
    rm -rf "$tmp"
    # Build the image-encoder TensorRT engine the backend loads.
    engine="${PERCEPTION_NANOOWL_ENGINE:-$DATA_DIR/owl_image_encoder_patch32.engine}"
    if [ -f "$engine" ]; then
        echo "NanoOWL engine already present: $engine"
    elif "$PY" -c "import nanoowl, torch2trt" 2>/dev/null; then
        echo "Building NanoOWL image-encoder engine -> $engine (this takes a few minutes) ..."
        ( cd "$REPO_ROOT" && "$PY" -m nanoowl.build_image_encoder_engine "$engine" ) \
            && echo "  built $engine" \
            || echo "  WARNING: engine build failed — check TensorRT is installed (JetPack)."
    else
        echo "  torch2trt/nanoowl not importable — skipping engine build. Install TensorRT"
        echo "  (JetPack) and re-run with --nanoowl. See brain/requirements-perception.txt."
    fi
    echo "  Point the server at it:  PERCEPTION_BACKENDS=yolo,nanoowl "
    echo "  PERCEPTION_NANOOWL_ENGINE=$engine $PY -m brain.perception.server"
fi

echo
echo "Perception deps ready. Start the server:"
echo "    $PY -m brain.perception.server   # then curl localhost:8100/snapshot"
