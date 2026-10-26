"""Conservative deterministic speech interpretation before optional local LLM.

Stop language takes priority over praise and social requests. Negated requests
never become movement. Unknown speech goes to dialogue only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    reply: str
    gesture: str | None = None
    pose: str | None = None
    mood: str | None = None
    goal: str | None = None


_STOP = Command("stay", "Staying here.", goal="")
_RULES = (
    (("sit", "lie down", "lay down"), Command("sit", "Settling here.", pose="sit")),
    (("stand", "stand up"), Command("stand", "Here I am.", pose="stand")),
    (("resume", "you can move now"), Command("resume", "Listening again.")),
    (("good boy", "good girl", "good dog", "good pup", "well done"),
     Command("praise", "Thanks!", gesture="wag", mood="excited")),
    (("hello", "hi", "hey"), Command("greet", "Hello.", gesture="perk", mood="curious")),
    (("play", "let us play", "fetch"), Command("play", "A little game?", mood="playful")),
    (("listen", "listen to me"), Command("listen", "I'm listening.")),
    (("come", "come here", "come back", "explore", "wander", "spin", "twirl",
      "roll over", "shake", "paw", "high five"),
     Command("unsupported", "I'll stay nearby. That movement isn't enabled.")),
)
_STOP_WORDS = re.compile(r"\b(?:stop|stay|wait|freeze|halt)\b|\bleave it\b")
_NEGATIVE_MOVEMENT = re.compile(
    r"\b(?:do not|dont|don't|never|no)\s+(?:please\s+)?"
    r"(?:move|go|walk|run|come|follow|chase|explore|wander|spin|stand|sit|play)\b"
)
_NEGATION = re.compile(r"\b(?:not|never|no|dont|don't|cannot|cant|can't)\b")


def interpret(text: str) -> Command | None:
    if not isinstance(text, str) or not text.strip():
        return None
    clean = text.lower().replace("’", "'").replace("let's", "let us")
    clean = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", clean)).strip()
    # Even ambiguous negated stop ('don't stop') cannot authorize motion.
    if _STOP_WORDS.search(clean) or _NEGATIVE_MOVEMENT.search(clean):
        return _STOP
    if _NEGATION.search(clean):
        return None
    padded = f" {clean} "
    for phrases, command in _RULES:
        if any(f" {phrase} " in padded for phrase in phrases):
            return command
    return None
