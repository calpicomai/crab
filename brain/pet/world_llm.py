"""Optional LLM-assisted teaching for the world model (laptop / dev).

Parses natural-language instructions into structured ``WorldModel.teach()`` calls.
Uses the same local OpenAI-compatible API as the agent (Ollama, llama-server).
Fully optional — the TUI and ``teach()`` work without it.

    python -m brain.pet.world_llm "Vacuum cleaners are scary, stay away from them"
"""

from __future__ import annotations

import json
import os
import re

from .worldmodel import DRIVES

WORLD_LLM_BASE_URL: str = os.environ.get(
    "WORLD_LLM_BASE_URL",
    os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
)
WORLD_LLM_MODEL: str = os.environ.get("WORLD_LLM_MODEL", os.environ.get("LLM_MODEL", "qwen2.5:3b"))
WORLD_LLM_API_KEY: str = os.environ.get("WORLD_LLM_API_KEY", os.environ.get("LLM_API_KEY", "sk-local"))

_SYSTEM = (
    "You convert short instructions about what a robot pet should think about objects "
    "into JSON. Output ONLY one JSON object with keys: "
    "label (main noun, lowercase), drive (one of chase|approach|avoid|neutral), "
    "valence (number -1 to 1, negative=scary/disliked, positive=loved), "
    "aliases (array of alternate strings), note (short reason). "
    "Examples: 'chase cats' -> {\"label\":\"cat\",\"drive\":\"chase\",\"valence\":0.6,"
    "\"aliases\":[\"kitten\"],\"note\":\"loves cats\"}. "
    "'vacuum cleaners are terrifying' -> {\"label\":\"vacuum\",\"drive\":\"avoid\","
    "\"valence\":-0.9,\"aliases\":[\"vacuum cleaner\"],\"note\":\"scary loud thing\"}."
)


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


def parse_teaching(text: str, *, simulate: bool = False) -> dict:
    """Return a teach()-ready dict from natural language.

    Raises RuntimeError if the LLM is unreachable. With simulate=True, returns a
    canned parse (no network) for tests.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty instruction")

    if simulate:
        low = text.lower()
        if "avoid" in low or "scary" in low or "hate" in low or "afraid" in low:
            return {"label": "vacuum", "drive": "avoid", "valence": -0.8,
                    "aliases": ["vacuum cleaner"], "note": text[:80]}
        if "chase" in low or "love" in low and "cat" in low:
            return {"label": "cat", "drive": "chase", "valence": 0.7,
                    "aliases": ["kitten"], "note": text[:80]}
        return {"label": low.split()[-1], "drive": "approach", "valence": 0.2,
                "aliases": [], "note": text[:80]}

    from openai import OpenAI

    client = OpenAI(base_url=WORLD_LLM_BASE_URL, api_key=WORLD_LLM_API_KEY, timeout=45.0)
    resp = client.chat.completions.create(
        model=WORLD_LLM_MODEL,
        temperature=0.1,
        max_tokens=200,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text},
        ],
    )
    raw = (resp.choices[0].message.content or "").strip()
    blob = _extract_json(raw)
    if not blob:
        raise RuntimeError(f"LLM did not return JSON: {raw[:200]!r}")

    drive = str(blob.get("drive", "approach")).strip().lower()
    if drive not in DRIVES:
        drive = "approach"
    try:
        valence = float(blob.get("valence", 0))
    except (TypeError, ValueError):
        valence = 0.0
    valence = max(-1.0, min(1.0, valence))
    label = str(blob.get("label", "")).strip().lower()
    if not label:
        raise RuntimeError(f"LLM JSON missing label: {blob}")
    aliases = blob.get("aliases") or []
    if not isinstance(aliases, list):
        aliases = []
    note = str(blob.get("note", text))[:200]
    return {
        "label": label,
        "drive": drive,
        "valence": valence,
        "aliases": [str(a).strip().lower() for a in aliases if str(a).strip()],
        "note": note,
    }


def apply_teaching(world, text: str, *, simulate: bool = False) -> dict:
    """Parse *text* and call ``world.teach()``. Returns the teach dict."""
    spec = parse_teaching(text, simulate=simulate)
    world.teach(
        spec["label"],
        drive=spec["drive"],
        valence=spec["valence"],
        aliases=spec.get("aliases"),
        note=spec.get("note", ""),
    )
    return spec


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="LLM-assisted world-model teaching.")
    parser.add_argument("instruction", nargs="+", help="Natural-language teaching.")
    parser.add_argument("--db", default=None, help="World DB path (default PET_WORLD_DB).")
    parser.add_argument("--simulate", action="store_true", help="Canned parse, no LLM.")
    args = parser.parse_args(argv)

    from . import config as pet_config
    from .worldmodel import WorldModel

    wm = WorldModel(args.db or pet_config.PET_WORLD_DB)
    try:
        spec = apply_teaching(wm, " ".join(args.instruction), simulate=args.simulate)
        print(f"Taught: {spec}")
        print(wm.summary())
    finally:
        wm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
