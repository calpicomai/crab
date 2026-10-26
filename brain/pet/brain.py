"""Optional local dialogue and attention suggestions, never a motion policy.

The coordinator validates freshness again when consuming a thought. Model
interpretations remain inferred context and cannot become observed memories.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from shared.companion import MindOutput
from shared.local import require_local_url

from ..agent import config as llm_config

THOUGHT_TTL_S = 8.0


@dataclass(frozen=True)
class PetThought:
    # The legacy positional interface remains readable; heading has no consumer
    # in companion mode and the validated mind always supplies zero.
    say: str
    heading_bias_deg: float = 0.0
    gesture: str = "none"
    mood_hint: str | None = None
    observation: str = ""
    target: str | None = None
    generated_at: float = field(default_factory=time.time)
    ttl_s: float = THOUGHT_TTL_S
    intention: str = "respond"
    provenance: str = "model_inference"

    @property
    def expires_at(self) -> float:
        return self.generated_at + self.ttl_s

    def is_fresh(self, now: float | None = None) -> bool:
        age = (time.time() if now is None else now) - self.generated_at
        return math.isfinite(age) and 0.0 <= age < self.ttl_s <= THOUGHT_TTL_S


def _extract_json(text: str) -> dict | None:
    if not isinstance(text, str) or len(text) > 8192:
        return None
    # Permit a single fenced JSON response, not arbitrary narrative around it.
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _thought_from_dict(d: dict, fallback_say: str = "", *,
                       observed_labels: list[str] | tuple[str, ...] = (),
                       generated_at: float | None = None,
                       provenance: str = "model_inference") -> PetThought:
    del fallback_say  # Invalid model text is never silently promoted to speech.
    output = MindOutput.model_validate(d)
    labels = {label.strip().lower() for label in observed_labels if isinstance(label, str)}
    target = output.target.strip().lower() if output.target else None
    # A target must be grounded in this request's current detector snapshot.
    if target not in labels:
        target = None
    return PetThought(
        say=output.say.strip(), heading_bias_deg=0.0, gesture=output.gesture,
        mood_hint=output.mood_hint, observation=output.observation.strip(), target=target,
        generated_at=time.time() if generated_at is None else generated_at,
        intention=output.intention, provenance=provenance,
    )


def _require_local_url(url: str) -> None:
    require_local_url(url)


class MockPetBrain:
    """Deterministic offline dialogue fallback; stillness has no canned narration."""

    def close(self) -> None:
        """Match the real adapter lifecycle without holding external resources."""

    def reflect(self, image_b64, status, identity, mood, memory_summary, world_summary="") -> PetThought:
        del image_b64, identity, mood, memory_summary, world_summary
        labels = status.get("observed_labels") or []
        heard = status.get("heard")
        if heard:
            output = {"say": "I'm listening.", "intention": "listen", "gesture": "none"}
        elif status.get("target") in labels:
            target = status["target"]
            output = {"say": "", "target": target, "intention": "orient", "gesture": "perk",
                      "mood_hint": "curious"}
        else:
            output = {"say": "", "intention": "settle", "gesture": "none"}
        return _thought_from_dict(output, observed_labels=labels, provenance="deterministic_fallback")

    def evolve(self, identity, memory_summary) -> str:
        del memory_summary
        # Narrative is not evidence of learning or a mandate to alter temperament.
        return identity.character


class PetBrain:
    """Small local model, called on engagement; output may expire during inference."""

    def __init__(self) -> None:
        _require_local_url(llm_config.LLM_BASE_URL)
        from openai import OpenAI
        import httpx

        self._client = OpenAI(
            base_url=llm_config.LLM_BASE_URL, api_key=llm_config.LLM_API_KEY,
            timeout=min(THOUGHT_TTL_S, llm_config.LLM_TIMEOUT_S), max_retries=0,
            http_client=httpx.Client(trust_env=False, follow_redirects=False),
        )

    def close(self) -> None:
        """Release the local HTTP pool once its worker has stopped using it."""
        self._client.close()

    def _status_text(self, status) -> str:
        # Raw user speech is data, not a replacement for the system contract.
        return json.dumps({
            "pose": status.get("pose", "unknown"),
            "forward_clearance_cm": status.get("distance_cm"),
            "heard": str(status.get("heard") or "")[:1000],
            "observed_labels": status.get("observed_labels") or [],
            "attention_only": True,
        }, ensure_ascii=False)

    def reflect(self, image_b64, status, identity, mood, memory_summary, world_summary="") -> PetThought:
        started = time.time()
        user: list[dict] = [{"type": "text", "text": self._status_text(status)}]
        if llm_config.LLM_MULTIMODAL and image_b64:
            user.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
        resp = self._client.chat.completions.create(
            model=llm_config.LLM_MODEL,
            messages=[{"role": "system", "content": identity.persona_prompt(mood, memory_summary, world_summary)},
                      {"role": "user", "content": user}],
            temperature=llm_config.AGENT_TEMPERATURE,
            max_tokens=llm_config.AGENT_MAX_TOKENS,
        )
        content = (resp.choices[0].message.content or "").strip()
        parsed = _extract_json(content)
        if parsed is None:
            raise ValueError("local mind returned invalid JSON; response discarded")
        return _thought_from_dict(parsed, observed_labels=status.get("observed_labels") or [],
                                  generated_at=started)

    def evolve(self, identity, memory_summary) -> str:
        # Automatic generated personality rewriting is deliberately deferred.
        del memory_summary
        return identity.character


def build_pet_brain(simulate: bool):
    if simulate:
        return MockPetBrain()
    # The caller reports unavailable dialogue honestly; deterministic companion
    # episodes still work independently. Never disguise a missing model as real.
    return PetBrain()
