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

# --- World model (brain/pet/worldmodel.py) ------------------------------------
# Persistent objects + places + action->outcome learning, grown from experience.
PET_WORLD_DB: str = os.environ.get("PET_WORLD_DB", os.path.join(PET_HOME, "world.db"))
# After a pet run with --log, queue JSONL lines into the world training queue (laptop
# consolidates later with world_train). Off by default on the Jetson.
PET_WORLD_QUEUE_LOG: bool = os.environ.get("PET_WORLD_QUEUE_LOG", "").strip().lower() in {
    "1", "true", "yes", "on",
}
PET_WORLD_TRAIN_SESSION: str = os.environ.get("PET_WORLD_TRAIN_SESSION", "")
# Labels the pet actively CHASES — highest interest, and it stays exciting even when
# familiar (it never gets bored of cats). Matched as substrings of a detection label
# (so YOLO "cat" and NanoOWL "a cat" both hit). Comma-separated.
PET_CHASE_LABELS: list[str] = [s.strip() for s in os.environ.get(
    "PET_CHASE_LABELS", "cat,dog").split(",") if s.strip()]
# Labels that are interesting to APPROACH — medium pull that fades as they become
# familiar (curiosity + boredom-of-the-seen).
PET_INTEREST_LABELS: list[str] = [s.strip() for s in os.environ.get(
    "PET_INTEREST_LABELS", "person,bird,teddy bear,sports ball,bottle,cup").split(",") if s.strip()]
# Minimum interest (0..1) for something in view to become a target it steers toward.
PET_TARGET_MIN_INTEREST: float = float(os.environ.get("PET_TARGET_MIN_INTEREST", "0.35"))
# Keep pursuing a target's last-seen bearing for this many cycles after it leaves
# view (a short "where'd it go?" search) before giving up and wandering.
PET_TARGET_LOST_TICKS: int = int(os.environ.get("PET_TARGET_LOST_TICKS", "3"))

# --- Purposeful motion (de-twitch) --------------------------------------------
# Strides per forward decision, scaled by mood.explore_bias between these bounds.
# Longer, committed bursts read as intentional instead of one-step shuffling; the
# Pi reflex still guards every stride.
PET_WALK_STEPS_MIN: int = int(os.environ.get("PET_WALK_STEPS_MIN", "1"))
PET_WALK_STEPS_MAX: int = int(os.environ.get("PET_WALK_STEPS_MAX", "3"))
# EMA smoothing on the desired heading (0 = react instantly, →1 = very smooth) to
# stop jittery per-tick steering, plus a forward deadband: if the desired heading is
# within this many degrees of straight ahead, just walk instead of emitting a
# micro-turn — the direct fix for the constant little wiggles.
PET_HEADING_SMOOTH: float = float(os.environ.get("PET_HEADING_SMOOTH", "0.5"))
PET_FORWARD_DEADBAND_DEG: float = float(os.environ.get("PET_FORWARD_DEADBAND_DEG", "18"))
# Every N body cycles (when not chasing), pause to "look around": rotate the body to
# sweep the fixed forward sonar across its surroundings and fill the costmap beyond
# the forward cone — this is what builds the top-down surroundings map on the
# dashboard. Reads as surveying; reflex-safe (only turns). 0 disables.
PET_SCAN_EVERY: int = int(os.environ.get("PET_SCAN_EVERY", "20"))

# --- Battery-aware behavior (uses RobotStatus.battery_v) ----------------------
# Below LOW_V the pet slows down (caps gait speed) to ease the current draw; below
# CRITICAL_V it stops wandering and rests to protect the cells / avoid a brownout.
# Volts for a 2S Li-ion pack: full ~8.4, nominal 7.4, get-it-on-the-charger ~6.6,
# hard floor ~6.0. No battery sensor (None) -> these never trigger.
PET_BATTERY_LOW_V: float = float(os.environ.get("PET_BATTERY_LOW_V", "6.6"))
PET_BATTERY_CRITICAL_V: float = float(os.environ.get("PET_BATTERY_CRITICAL_V", "6.2"))
PET_BATTERY_LOW_SPEED: int = int(os.environ.get("PET_BATTERY_LOW_SPEED", "55"))

# Reflex clearance handed to each walk (Pi aborts the stride below it).
PET_REFLEX_CM: float = float(os.environ.get("PET_REFLEX_CM", str(llm_config.AGENT_REFLEX_CM)))

# --- Emoting (dog-like body language) -----------------------------------------
# The pet should express with its body, not just walk. On every mood change it
# does that mood's signature move; between steps it sprinkles smaller fidgets with
# probability PET_EMOTE_CHANCE so it always reads as a living creature.
PET_EMOTE: bool = os.environ.get("PET_EMOTE", "1").strip().lower() not in {"0", "false", "no", "off"}
PET_EMOTE_CHANCE: float = float(os.environ.get("PET_EMOTE_CHANCE", "0.15"))

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
