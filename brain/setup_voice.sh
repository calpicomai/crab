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

echo "Installing STT (faster-whisper) into $VENV ..."
"$VENV/bin/pip" install faster-whisper

cat <<EOF

Voice backend deps ready.

STT (spoken commands): faster-whisper installed. The model (WHISPER_MODEL,
default 'base') auto-downloads on first run. On the Jetson, prefer GPU:
    WHISPER_DEVICE=cuda WHISPER_COMPUTE=float16 ...

TTS (the pet's voice): install Piper + fetch a voice model, then point at it:
    # Piper release binary + a .onnx voice from https://github.com/rhasspy/piper
    PET_VOICE=1 PET_VOICE_MODEL=/path/to/voice.onnx ...

On the PI (audio device): plug in a USB mic + speaker and
    sudo apt install -y alsa-utils
The pet's voice plays on the Pi by default (PET_AUDIO_SINK=pi); the Pi mic streams
to the Jetson for Whisper. Everything degrades to text-only if a piece is missing.

Then just run the pet as usual (brain/run.sh will ask about voice):
    bash brain/run.sh            # menu -> pet
    # or explicitly:
    PET_VOICE=1 PET_VOICE_MODEL=/path/voice.onnx brain/.venv/bin/python -m brain.pet
EOF
