"""Pet-specific configuration (env-overridable). LLM/backend knobs are reused
from brain/agent/config.py; robot/reflex/steer knobs from brain/config.py."""

from __future__ import annotations

import os

from ..agent import config as llm_config  # noqa: F401 (re-exported for convenience)

# Where the pet's persistent self lives (identity + memory), so it's the SAME pet
# across runs. Defaults to a stable per-user dir outside the repo.
PET_HOME: str = os.environ.get("PET_HOME", os.path.expanduser("~/.picrawler_pet"))
PET_MEMORY_DB: str = os.environ.get("PET_MEMORY_DB", os.path.join(PET_HOME, "memory.db"))
PET_IDENTITY_FILE: str = os.environ.get("PET_IDENTITY_FILE", os.path.join(PET_HOME, "identity.json"))
# Optional name for a NEW pet; an existing pet keeps its saved name unless this
# differs. None -> a fresh pet names itself.
PET_NAME: str | None = os.environ.get("PET_NAME") or None

# Seconds between the slow "mind" reflections (VLM look + speak + remember). The
# fast "body" loop runs continuously underneath regardless.
PET_REFLECT_S: float = float(os.environ.get("PET_REFLECT_S", "4.0"))
# Reflections between character re-summaries (the personality-growth step).
PET_EVOLVE_EVERY: int = int(os.environ.get("PET_EVOLVE_EVERY", "8"))
# How many body cycles it commits to a turn direction before reconsidering —
# steering hysteresis that stops the left-right rock (the "pans side to side").
PET_HYSTERESIS_TICKS: int = int(os.environ.get("PET_HYSTERESIS_TICKS", "3"))
# Anti-spin escape: after this many consecutive turn-only cycles with no forward
# progress, force one reflex-protected probe step so a boxed-in / mis-sensing pet
# can't circle forever (the Pi reflex still aborts the stride if it's truly
# blocked). 0 disables the escape.
PET_ANTISPIN_TICKS: int = int(os.environ.get("PET_ANTISPIN_TICKS", "6"))

# Reflex clearance handed to each walk (Pi aborts the stride below it).
PET_REFLEX_CM: float = float(os.environ.get("PET_REFLEX_CM", str(llm_config.AGENT_REFLEX_CM)))

# --- Emoting (dog-like body language) -----------------------------------------
# The pet should express with its body, not just walk. On every mood change it
# does that mood's signature move; between steps it sprinkles smaller fidgets with
# probability PET_EMOTE_CHANCE so it always reads as a living creature.
PET_EMOTE: bool = os.environ.get("PET_EMOTE", "1").strip().lower() not in {"0", "false", "no", "off"}
PET_EMOTE_CHANCE: float = float(os.environ.get("PET_EMOTE_CHANCE", "0.4"))

# --- Voice out (Piper TTS) -----------------------------------------------------
# Off by default (needs piper + a voice model). Turn on with PET_VOICE=1 and point
# PET_VOICE_MODEL at a Piper .onnx voice. Synthesis runs on the Jetson; playback
# goes to the sink: "pi" (default — POST the WAV to the robot's /audio/play so it
# comes out of the Pi's speaker) or "local" (aplay here, for laptop dev). Missing
# pieces -> the pet stays text-only (see brain/pet/voice.py).
PET_VOICE: bool = os.environ.get("PET_VOICE", "").strip().lower() in {"1", "true", "yes", "on"}
PET_VOICE_MODEL: str | None = os.environ.get("PET_VOICE_MODEL") or None
PET_VOICE_PLAYER: str = os.environ.get("PET_VOICE_PLAYER", "aplay -q")
PET_AUDIO_SINK: str = os.environ.get("PET_AUDIO_SINK", "pi").strip().lower()  # pi | local

# --- Voice in (spoken commands: Pi mic -> Jetson Whisper STT) ------------------
# On by default, but degrades to off if faster-whisper isn't installed or the mic
# stream is unreachable. The pet mic lives on the Pi (robot's /audio/stream);
# faster-whisper runs here. PET_WAKE_WORD (default the pet's name at runtime, if
# set) gates which utterances it reacts to; empty = react to all speech.
PET_STT: bool = os.environ.get("PET_STT", "1").strip().lower() not in {"0", "false", "no", "off"}
WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "base")
WHISPER_DEVICE: str = os.environ.get("WHISPER_DEVICE", "auto")
WHISPER_COMPUTE: str = os.environ.get("WHISPER_COMPUTE", "int8")
PET_WAKE_WORD: str = os.environ.get("PET_WAKE_WORD", "")
