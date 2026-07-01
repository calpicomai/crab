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

# Reflex clearance handed to each walk (Pi aborts the stride below it).
PET_REFLEX_CM: float = float(os.environ.get("PET_REFLEX_CM", str(llm_config.AGENT_REFLEX_CM)))
