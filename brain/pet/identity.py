"""The pet's persistent, evolving identity — the heart of "you name it, it grows
its own personality from experience."

On first boot it takes the name you give (or picks its own) plus a small random
temperament *seed*. From then on its **character** — a short natural-language
self-summary — is re-condensed from its memories every so often (see
brain/pet/brain.py:evolve) and saved, so across runs it's the *same* pet becoming
more itself. This is prompt/summary + tally based growth that persists on-device;
it is NOT model fine-tuning (a far-later stage), but it does make each pet a
distinct individual that references its own history.

Stored as a small JSON file so it's easy to inspect / back up / reset.
"""

from __future__ import annotations

import json
import os
import random
import time

# Temperament seed pools. A fresh pet rolls a name (if unnamed) + a couple of
# traits; experience grows the rest.
_NAME_POOL = [
    "Nibbles", "Pixel", "Scout", "Waffle", "Biscuit", "Clank", "Pepper", "Mochi",
    "Gizmo", "Tofu", "Sprocket", "Marbles", "Dot", "Fern", "Bumble", "Noodle",
]
_TRAIT_POOL = [
    "curious", "shy", "bold", "playful", "cautious", "affectionate", "mischievous",
    "calm", "skittish", "brave", "gentle", "energetic", "watchful", "friendly",
]


class PetIdentity:
    """Load-or-create a pet's persistent identity at ``path`` (a JSON file)."""

    def __init__(self, path: str, name: str | None = None) -> None:
        self.path = path
        if path != ":memory:" and os.path.exists(path):
            self._load()
            # An explicit --name can (re)name an existing pet.
            if name and name != self.name:
                self.name = name
                self.save()
        else:
            self._create(name)

    # ------------------------------------------------------------------ #
    def _create(self, name: str | None) -> None:
        rng = random.Random()
        self.name: str = name or rng.choice(_NAME_POOL)
        self.born_ts: float = time.time()
        self.seed_traits: list[str] = rng.sample(_TRAIT_POOL, 2)
        # The character starts as a seed sentence; brain.evolve() grows it.
        self.character: str = (
            f"{self.name} is a {' and '.join(self.seed_traits)} little robot pet, "
            "brand new to the world and still figuring everything out."
        )
        self.tallies: dict[str, int] = {}  # thing seen -> times
        self.reflections: int = 0
        self.save()

    def _load(self) -> None:
        with open(self.path) as fh:
            data = json.load(fh)
        self.name = data.get("name", "Pet")
        self.born_ts = data.get("born_ts", time.time())
        self.seed_traits = data.get("seed_traits", [])
        self.character = data.get("character", "")
        self.tallies = data.get("tallies", {})
        self.reflections = data.get("reflections", 0)

    def save(self) -> None:
        if self.path == ":memory:":
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(
                {
                    "name": self.name,
                    "born_ts": self.born_ts,
                    "seed_traits": self.seed_traits,
                    "character": self.character,
                    "tallies": self.tallies,
                    "reflections": self.reflections,
                },
                fh,
                indent=2,
            )

    # ------------------------------------------------------------------ #
    def note_seen(self, labels: list[str]) -> None:
        """Tally things it has seen — the raw counts behind familiarity/recognition."""
        for label in labels:
            key = label.strip().lower()
            if key:
                self.tallies[key] = self.tallies.get(key, 0) + 1

    def familiar_with(self, label: str) -> int:
        return self.tallies.get(label.strip().lower(), 0)

    def evolve(self, new_character: str) -> None:
        """Replace the character self-summary (produced by the brain) and persist."""
        new_character = (new_character or "").strip()
        if new_character:
            self.character = new_character
        self.reflections += 1
        self.save()

    def age_str(self) -> str:
        secs = max(0.0, time.time() - self.born_ts)
        if secs < 3600:
            return f"{int(secs / 60)} min old"
        if secs < 86400:
            return f"{secs / 3600:.1f} hours old"
        return f"{secs / 86400:.1f} days old"

    def top_seen(self, n: int = 5) -> list[str]:
        return [k for k, _ in sorted(self.tallies.items(), key=lambda kv: -kv[1])[:n]]

    def persona_prompt(self, mood: str, memory_summary: str) -> str:
        """The in-character system prompt: who the pet is, plus how to respond."""
        familiar = ", ".join(self.top_seen()) or "nothing in particular yet"
        return (
            f"You ARE {self.name}, a small four-legged robot pet ({self.age_str()}). "
            f"You are not an assistant — you are a curious little creature with your "
            f"own feelings, exploring your home.\n"
            f"Your temperament seed: {', '.join(self.seed_traits) or 'unformed'}.\n"
            f"Who you've become so far: {self.character}\n"
            f"Things you've seen a lot: {familiar}.\n"
            f"Recent memories:\n{memory_summary}\n"
            f"Right now you feel: {mood}.\n\n"
            "Each turn you see one camera image and a status line (pose, forward "
            "clearance in cm). React like a pet: notice things, remember when you've "
            "seen something before, and let your mood and curiosity guide you. A "
            "fast reflex keeps you safe, so you only choose intent, never servo "
            "detail. Respond ONLY with a JSON object: {\"say\": a short first-person "
            "remark in your own voice, \"heading_bias_deg\": -60..60 (negative=left, "
            "positive=right; which way you want to drift), \"gesture\": one of "
            "\"none\"|\"wiggle\"|\"tilt\"|\"approach\"|\"backaway\"|\"rest\", "
            "\"mood_hint\": one word for how you feel now, \"observation\": a few "
            "words on what you see to remember}."
        )
