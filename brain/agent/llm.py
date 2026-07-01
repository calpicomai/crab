"""LLM brain wrapper — turns a camera frame + status into one narrated action.

Talks the OpenAI-compatible chat API (``openai`` SDK) pointed at a LOCAL server
(llama.cpp's llama-server by default), so it's fully on-device and the backend is
swappable by config. A ``MockBrain`` provides a canned rule-based policy so the
whole agent loop runs off-GPU (CI, dev laptop) with no model.

Both expose the same ``decide(...) -> Decision`` so loop.py doesn't care which is
active. The real brain sends the frame as an image (or a text summary when
``LLM_MULTIMODAL=0``); the mock ignores the pixels and reasons off the status.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import config, tools


@dataclass
class Decision:
    """One agent turn: a short spoken narration + the chosen tool call."""

    say: str
    tool_name: str
    tool_args: dict


def _context_line(status: dict, goal: str | None, last_action: str | None) -> str:
    pose = status.get("pose", "?")
    dist = status.get("distance_cm")
    dist_s = f"{dist:.0f}cm" if isinstance(dist, (int, float)) else "unknown"
    parts = [f"pose={pose}", f"forward_clearance={dist_s}"]
    if status.get("reflex_stopped"):
        parts.append("(last move was reflex-stopped by a close obstacle)")
    if last_action:
        parts.append(f"last_action={last_action}")
    parts.append(f"goal={goal}" if goal else "goal=free exploration")
    return "Status: " + ", ".join(parts)


class MockBrain:
    """Rule-based stand-in for the LLM so the loop is testable without a model."""

    def __init__(self) -> None:
        self._tick = 0

    def decide(self, image_b64, context_text, goal, status) -> Decision:  # noqa: ANN001
        self._tick += 1
        dist = status.get("distance_cm")
        blocked = status.get("reflex_stopped") or (isinstance(dist, (int, float)) and dist < 40)
        if status.get("pose") != "standing" and self._tick == 1:
            return Decision("Standing up to take a look around.", "stand", {})
        if blocked:
            side = "left" if self._tick % 2 == 0 else "right"
            deg = -35.0 if side == "left" else 35.0
            return Decision(f"Something's close ahead — turning {side} toward open space.", "turn", {"degrees": deg})
        if self._tick % 5 == 0:
            return Decision("Pausing to look around.", "get_status", {})
        return Decision("Path ahead looks clear — moving forward to explore.", "walk", {"steps": 2})


class LLMBrain:
    """Real brain: an OpenAI-compatible chat model (local llama-server by default)."""

    def __init__(self) -> None:
        # Imported lazily so the package imports without `openai` installed (the
        # mock path and tests don't need it).
        from openai import OpenAI

        self._client = OpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            timeout=config.LLM_TIMEOUT_S,
        )

    def decide(self, image_b64, context_text, goal, status) -> Decision:  # noqa: ANN001
        user_content: list[dict] = [{"type": "text", "text": context_text}]
        if config.LLM_MULTIMODAL and image_b64:
            user_content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
            )
        resp = self._client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": config.SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            tools=tools.TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=config.AGENT_TEMPERATURE,
            max_tokens=config.AGENT_MAX_TOKENS,
        )
        msg = resp.choices[0].message
        say = (msg.content or "").strip()

        # Preferred path: a proper tool call.
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            call = tool_calls[0]
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            return Decision(say or f"({name})", name, args if isinstance(args, dict) else {})

        # Fallback: a small model may embed the action as JSON in the content.
        parsed = _parse_content_action(say)
        if parsed is not None:
            name, args, spoken = parsed
            return Decision(spoken or say or f"({name})", name, args)

        # No usable action -> a harmless status read (the loop still narrates).
        return Decision(say or "Looking around.", "get_status", {})


def _parse_content_action(text: str):
    """Best-effort extraction of {"action"/"tool", "args", "say"} JSON from content.
    Returns (name, args, say) or None."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        blob = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    name = blob.get("action") or blob.get("tool") or blob.get("name")
    if name not in tools.TOOL_NAMES:
        return None
    args = blob.get("args") or blob.get("arguments") or {}
    return name, (args if isinstance(args, dict) else {}), blob.get("say") or blob.get("thought") or ""


def build_brain(simulate: bool):
    """MockBrain when simulating (or when `openai` isn't importable), else LLMBrain."""
    if simulate:
        return MockBrain()
    try:
        return LLMBrain()
    except Exception as exc:  # noqa: BLE001 - openai missing / bad config -> degrade to mock
        print(f"  (LLM backend unavailable at import: {exc}; using canned policy)")
        return MockBrain()
