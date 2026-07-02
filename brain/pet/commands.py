"""Turn a heard phrase into a dog command — so spoken commands work even with no
VLM. Pure and unit-testable: ``interpret(text) -> Command | None`` (None = not a
direct command, let the mind/VLM handle it as free-form speech).

Deliberately simple keyword matching over the transcript; the VLM (when present)
handles everything richer via the ``heard`` context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Command:
    name: str
    reply: str
    gesture: str | None = None   # a brain/pet/expressions gesture to perform
    pose: str | None = None      # "sit" | "stand"
    mood: str | None = None      # a mood to nudge toward
    goal: str | None = None      # set/clear a roaming goal ("" clears)


# (keywords, Command). First match wins; keep specific phrases before loose ones.
_RULES: list[tuple[tuple[str, ...], Command]] = [
    (("good boy", "good girl", "good dog", "good pup", "well done"),
     Command("praise", "yippee!", gesture="wag", mood="excited")),
    (("sit", "lie down", "lay down"), Command("sit", "okay, sitting.", pose="sit", mood="cautious")),
    (("stay", "wait", "stop", "freeze", "halt"), Command("stay", "staying put.", pose="sit")),
    (("come", "here", "come here", "come back"), Command("come", "coming!", gesture="approach", mood="excited")),
    (("spin", "roll over", "twirl"), Command("spin", "wheee!", gesture="spin", mood="playful")),
    (("shake", "paw", "high five"), Command("shake", "paw!", gesture="shake", mood="playful")),
    (("play", "let's play", "fetch"), Command("play", "let's play!", gesture="playbow", mood="playful")),
    (("explore", "go", "wander", "off you go", "go play"),
     Command("explore", "exploring!", mood="curious", goal="explore")),
    (("no", "bad dog", "stop that", "leave it"), Command("scold", "sorry...", mood="cautious")),
    (("stand", "up", "stand up"), Command("stand", "standing.", pose="stand")),
]


def interpret(text: str) -> Command | None:
    if not text:
        return None
    # Normalize: lowercase, punctuation -> spaces, collapse — so "Good boy!" and
    # "sit." match, without tripping on substrings ("sit" inside "situation").
    clean = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()
    t = f" {clean} "
    for keywords, cmd in _RULES:
        for kw in keywords:
            if f" {kw} " in t:
                return cmd
    return None
