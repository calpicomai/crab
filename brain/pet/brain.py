"""The pet's inner voice — turns a camera frame + status + its identity/mood/memory
into an in-character reaction, and periodically re-condenses its character.

Reuses the agent's OpenAI-compatible backend settings (brain/agent/config.py), so
it runs against the same local llama-server (a VLM) and is swappable. A
``MockPetBrain`` gives canned, in-character reactions so the whole pet runs
off-GPU / before the model server exists — the pet still feels alive on mood +
memory alone.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

from ..agent import config as llm_config
from .mood import MOODS


@dataclass
class PetThought:
    say: str
    heading_bias_deg: float
    gesture: str
    mood_hint: str | None
    observation: str


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _thought_from_dict(d: dict, fallback_say: str) -> PetThought:
    try:
        bias = float(d.get("heading_bias_deg", 0.0) or 0.0)
    except (TypeError, ValueError):
        bias = 0.0
    bias = max(-60.0, min(60.0, bias))
    return PetThought(
        say=(d.get("say") or fallback_say or "").strip() or fallback_say,
        heading_bias_deg=bias,
        gesture=(d.get("gesture") or "none").strip().lower(),
        mood_hint=(d.get("mood_hint") or None),
        observation=(d.get("observation") or "").strip(),
    )


# In-character canned lines per mood for the mock brain (no model needed).
_CANNED = {
    "curious": ["what's over there?", "ooh, something new!", "let's go see..."],
    "excited": ["hi hi hi!", "a friend!!", "this is the best!"],
    "playful": ["wheee!", "catch me!", "bouncy bouncy"],
    "cautious": ["hmm... careful now", "that's a bit close", "tip-toeing"],
    "startled": ["eep! too close!", "nope nope backing up", "yikes!"],
    "bored": ["...anything happening?", "*sigh*", "kinda quiet here"],
    "sleepy": ["*yawn*", "maybe a little rest", "so sleepy..."],
}


class MockPetBrain:
    """Canned, in-character reactions driven by mood + status. No model."""

    def __init__(self) -> None:
        self._rng = random.Random(1234)
        self._flip = 1.0

    def reflect(self, image_b64, status, identity, mood, memory_summary) -> PetThought:  # noqa: ANN001
        heard = status.get("heard")
        if heard:
            return PetThought(f"{identity.name}: did you say '{heard}'?", self._rng.uniform(-8, 8),
                              "tilt", "curious", f"human said '{heard}'")
        dist = status.get("distance_cm")
        blocked = status.get("reflex_stopped") or (isinstance(dist, (int, float)) and dist < 40)
        line = self._rng.choice(_CANNED.get(mood, _CANNED["curious"]))
        say = f"{identity.name}: {line}"
        if blocked:
            self._flip *= -1.0
            return PetThought(say, 45.0 * self._flip, "backaway" if status.get("reflex_stopped") else "none",
                              "cautious", "something close ahead")
        obs = "open space ahead" if isinstance(dist, (int, float)) and dist > 60 else "a clear-ish path"
        gesture = "wiggle" if mood in ("excited", "playful") else ("tilt" if mood == "curious" else "none")
        return PetThought(say, self._rng.uniform(-15, 15), gesture, None, obs)

    def evolve(self, identity, memory_summary) -> str:  # noqa: ANN001
        top = ", ".join(identity.top_seen(3)) or "not much yet"
        traits = " and ".join(identity.seed_traits) or "growing"
        return (
            f"{identity.name} is a {traits} little robot who has been exploring for a while now, "
            f"often noticing {top}. It's slowly getting braver and more sure of its home."
        )


class PetBrain:
    """Real brain: an OpenAI-compatible (V)LM at the local llama-server."""

    def __init__(self) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            base_url=llm_config.LLM_BASE_URL,
            api_key=llm_config.LLM_API_KEY,
            timeout=llm_config.LLM_TIMEOUT_S,
        )

    def _status_text(self, status) -> str:  # noqa: ANN001
        dist = status.get("distance_cm")
        dist_s = f"{dist:.0f}cm" if isinstance(dist, (int, float)) else "unknown"
        extra = " (just had to stop — something was close!)" if status.get("reflex_stopped") else ""
        heard = status.get("heard")
        if heard:
            extra += f" Your human just said to you: \"{heard}\" — react to it."
        return f"pose={status.get('pose', '?')}, forward clearance={dist_s}{extra}"

    def reflect(self, image_b64, status, identity, mood, memory_summary) -> PetThought:  # noqa: ANN001
        system = identity.persona_prompt(mood, memory_summary)
        user: list[dict] = [{"type": "text", "text": self._status_text(status)}]
        if llm_config.LLM_MULTIMODAL and image_b64:
            user.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
        resp = self._client.chat.completions.create(
            model=llm_config.LLM_MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=llm_config.AGENT_TEMPERATURE,
            max_tokens=llm_config.AGENT_MAX_TOKENS,
        )
        content = (resp.choices[0].message.content or "").strip()
        parsed = _extract_json(content)
        if parsed is not None:
            return _thought_from_dict(parsed, fallback_say=content[:80])
        # Model didn't return JSON — treat the whole reply as the pet talking.
        return PetThought(content[:120] or f"{identity.name}: ...", 0.0, "none", None, "")

    def evolve(self, identity, memory_summary) -> str:  # noqa: ANN001
        prompt = (
            f"You are helping a robot pet named {identity.name} grow. Its temperament seed is "
            f"{', '.join(identity.seed_traits)}. Its current self-description: \"{identity.character}\". "
            f"Recent experiences:\n{memory_summary}\n\n"
            "In 1-2 warm sentences, write an UPDATED third-person description of who "
            f"{identity.name} is becoming, evolving from the current one based on these "
            "experiences (its growing likes, habits, and personality). Reply with only the description."
        )
        resp = self._client.chat.completions.create(
            model=llm_config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.6,
            max_tokens=120,
        )
        return (resp.choices[0].message.content or "").strip()


def build_pet_brain(simulate: bool):
    """MockPetBrain when simulating (or when the backend/openai is unavailable)."""
    if simulate:
        return MockPetBrain()
    try:
        return PetBrain()
    except Exception as exc:  # noqa: BLE001 - openai missing / bad config -> canned
        print(f"  ({exc}; using the pet's canned inner voice)")
        return MockPetBrain()
