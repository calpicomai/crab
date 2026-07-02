#!/usr/bin/env bash
# Set up voice I/O on the Jetson: Whisper STT (spoken commands) + Piper TTS (the
# pet's voice). The mic + speaker are on the PI; this installs the COMPUTE here.
#
#   bash brain/setup_voice.sh
#
# Audio device wiring lives on the Pi — plug a USB mic + speaker into the Pi and:
#   sudo apt install -y alsa-utils        # provides arecord / aplay
#   arecord -l   /   aplay -l             # find your device (e.g. "plughw:1,0")
#   # then set PICRAWLER_MIC_DEVICE / PICRAWLER_SPEAKER_DEVICE if the defaults miss.
set -euo pipefail

NODE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$NODE_DIR/.venv"
[ -x "$VENV/bin/pip" ] || { echo "brain/.venv not found — run 'bash brain/setup.sh' first." >&2; exit 1; }

REPO_ROOT="$(dirname "$NODE_DIR")"
CONF="$REPO_ROOT/crab.env"

echo "Installing STT (faster-whisper) into $VENV ..."
"$VENV/bin/pip" install faster-whisper

# ---------------------------------------------------------------- Piper (TTS)
# The pet's voice. Synthesis runs here (Jetson); the WAV is POSTed to the Pi and
# played on the Robot HAT speaker (PET_AUDIO_SINK=pi). Fetch the prebuilt Piper
# binary (reliable on aarch64) + a default voice and point crab.env at them, so
# the pet talks OUT LOUD instead of degrading to text. Skip with SETUP_PIPER=0.
PIPER_VER="${PIPER_VERSION:-2023.11.14-2}"
PIPER_DIR="$NODE_DIR/.piper"
VOICES_DIR="$NODE_DIR/.voices"
VOICE_NAME="${PIPER_VOICE:-en_US-lessac-medium}"   # default voice (auto-fetched)
if [ "${SETUP_PIPER:-1}" = "1" ]; then
  echo
  echo "== Piper TTS (the pet's voice) =="
  arch="$(uname -m)"
  if command -v piper >/dev/null 2>&1; then
    echo "  piper already on PATH ($(command -v piper))."
  elif [ -x "$PIPER_DIR/piper/piper" ]; then
    echo "  piper already at $PIPER_DIR/piper/piper."
  elif [[ "$arch" =~ ^(aarch64|x86_64|armv7l)$ ]]; then
    url="https://github.com/rhasspy/piper/releases/download/$PIPER_VER/piper_linux_${arch}.tar.gz"
    echo "  Downloading Piper ($arch) ..."
    mkdir -p "$PIPER_DIR"
    if curl -fsSL "$url" -o "$PIPER_DIR/piper.tgz" && tar -xzf "$PIPER_DIR/piper.tgz" -C "$PIPER_DIR"; then
      rm -f "$PIPER_DIR/piper.tgz"; echo "  ✓ piper at $PIPER_DIR/piper/piper"
    else
      echo "  WARNING: Piper download failed — grab a build from https://github.com/rhasspy/piper/releases"
    fi
  else
    echo "  (unknown arch '$arch' — install Piper manually from the releases page.)"
  fi
  # A default voice (.onnx + .onnx.json) from the Piper voices repo on Hugging Face.
  mkdir -p "$VOICES_DIR"
  onnx="$VOICES_DIR/$VOICE_NAME.onnx"
  if [ -f "$onnx" ]; then
    echo "  voice already at $onnx."
  else
    hf="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/$VOICE_NAME"
    echo "  Downloading voice $VOICE_NAME ..."
    if curl -fsSL "$hf.onnx" -o "$onnx" && curl -fsSL "$hf.onnx.json" -o "$onnx.json"; then
      echo "  ✓ voice at $onnx"
    else
      echo "  WARNING: voice download failed (only en_US-lessac-medium is auto-fetched; for"
      echo "  others grab .onnx + .onnx.json from https://huggingface.co/rhasspy/piper-voices)."
      onnx=""
    fi
  fi
  # Point crab.env at the voice so brain/run.sh speaks out loud automatically.
  if [ -n "$onnx" ] && [ -f "$CONF" ]; then
    set_conf() { if grep -q "^$1=" "$CONF"; then sed -i "s|^$1=.*|$1=\"$2\"|" "$CONF"; else echo "$1=\"$2\"" >> "$CONF"; fi; }
    set_conf PET_VOICE 1
    set_conf PET_VOICE_MODEL "$onnx"
    echo "  pointed $CONF at the voice (PET_VOICE=1, PET_VOICE_MODEL)."
  elif [ -n "$onnx" ]; then
    echo "  Run it with:  PET_VOICE=1 PET_VOICE_MODEL=$onnx bash brain/run.sh"
  fi
fi

cat <<EOF

Voice backend deps ready.

STT (spoken commands): faster-whisper installed. The model (WHISPER_MODEL,
default 'base') auto-downloads on first run. On the Jetson, prefer GPU:
    WHISPER_DEVICE=cuda WHISPER_COMPUTE=float16 ...

TTS (the pet's voice): Piper + the $VOICE_NAME voice fetched above (skip with
SETUP_PIPER=0; pick another with PIPER_VOICE=...). brain/run.sh adds Piper to PATH
and, if crab.env exists, now points at the voice; else pass it explicitly:
    PET_VOICE=1 PET_VOICE_MODEL=$VOICES_DIR/$VOICE_NAME.onnx bash brain/run.sh

On the PI (audio device):
    sudo apt install -y alsa-utils                 # arecord / aplay
  * SPEAKER = the Robot HAT's onboard speaker (I2S amp). Enable it once with
    SunFounder's installer so it becomes the default ALSA sink; then plain aplay
    (what the pet uses) plays through it:
        git clone https://github.com/sunfounder/robot-hat.git
        cd robot-hat && sudo bash i2samp.sh       # reboot; may need to run it 2x
        aplay /usr/share/sounds/alsa/Front_Center.wav   # should play from the HAT
    Leave PICRAWLER_SPEAKER_DEVICE empty so playback uses that default.
  * MIC = a USB microphone (the HAT has no mic in). Find it with `arecord -l` and,
    if it isn't the default, set PICRAWLER_MIC_DEVICE=plughw:<card>,<dev>.
The pet's voice plays on the Pi by default (PET_AUDIO_SINK=pi); the Pi mic streams
to the Jetson for Whisper. Everything degrades to text-only if a piece is missing.

Then just run the pet as usual (brain/run.sh will ask about voice):
    bash brain/run.sh            # menu -> pet
    # or explicitly:
    PET_VOICE=1 PET_VOICE_MODEL=/path/voice.onnx brain/.venv/bin/python -m brain.pet
EOF
