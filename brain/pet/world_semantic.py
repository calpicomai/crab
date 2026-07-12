"""LLM-powered semantic concepts for the world model (laptop training).

On the laptop a local VLM/text LLM turns images + notes + experience logs into
**concepts**: canonical labels, categories, rich keyword lists (for generalization
at runtime without an LLM on the Jetson), drive/valence, and descriptions.

Runtime on the robot is still lightweight SQLite lookup — no LLM required per tick.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from .worldmodel import DRIVES

WORLD_LLM_BASE_URL: str = os.environ.get(
    "WORLD_LLM_BASE_URL",
    os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
)
WORLD_LLM_MODEL: str = os.environ.get("WORLD_LLM_MODEL", os.environ.get("LLM_MODEL", "qwen2.5vl:3b"))
WORLD_LLM_API_KEY: str = os.environ.get("WORLD_LLM_API_KEY", os.environ.get("LLM_API_KEY", "sk-local"))
WORLD_LLM_MULTIMODAL: bool = os.environ.get("WORLD_LLM_MULTIMODAL", os.environ.get("LLM_MULTIMODAL", "1")).strip().lower() not in {
    "0", "false", "no", "off",
}


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


def _norm_list(items) -> list[str]:
    if not items:
        return []
    if isinstance(items, str):
        items = [x.strip() for x in items.split(",")]
    out = []
    for it in items:
        s = str(it).strip().lower()
        if s and s not in out:
            out.append(s)
    return out


def _coerce_spec(blob: dict, fallback_note: str = "") -> dict:
    drive = str(blob.get("drive", "approach")).strip().lower()
    if drive not in DRIVES:
        drive = "approach"
    try:
        valence = float(blob.get("valence", 0))
    except (TypeError, ValueError):
        valence = 0.0
    valence = max(-1.0, min(1.0, valence))
    try:
        weight = float(blob.get("weight", {"chase": 1.0, "approach": 0.6, "avoid": 0.0, "neutral": 0.12}[drive]))
    except (TypeError, ValueError):
        weight = 0.6
    weight = max(0.0, min(1.0, weight))
    canonical = str(blob.get("canonical") or blob.get("label") or "").strip().lower()
    if not canonical:
        raise ValueError("concept missing canonical/label")
    keywords = _norm_list(blob.get("keywords") or [])
    aliases = _norm_list(blob.get("aliases") or [])
    if canonical not in keywords:
        keywords.insert(0, canonical)
    for a in aliases:
        if a not in keywords:
            keywords.append(a)
    return {
        "canonical": canonical,
        "category": str(blob.get("category", "thing")).strip().lower()[:40],
        "description": str(blob.get("description", fallback_note))[:500],
        "drive": drive,
        "weight": weight,
        "valence": valence,
        "keywords": keywords,
        "aliases": aliases,
        "note": str(blob.get("note", fallback_note))[:300],
    }


_SYSTEM_TEXT = (
    "You extract robot-pet world knowledge into JSON. The pet uses keywords at runtime "
    "to recognize varied detector labels (YOLO/NanoOWL). Output ONLY one JSON object with: "
    "canonical (short noun, lowercase), category, description (1 sentence), "
    "drive (chase|approach|avoid|neutral), valence (-1..1), weight (0..1), "
    "keywords (array of 5-15 lowercase strings — synonyms, related words, detector labels), "
    "aliases (array), note (short). Be generous with keywords for generalization."
)

_SYSTEM_IMAGE = _SYSTEM_TEXT + " You also see a camera image from the pet's view."


def _llm_chat(messages: list[dict], *, simulate: bool, fallback: dict) -> dict:
    if simulate:
        return _coerce_spec(fallback)
    from openai import OpenAI

    client = OpenAI(base_url=WORLD_LLM_BASE_URL, api_key=WORLD_LLM_API_KEY, timeout=120.0)
    resp = client.chat.completions.create(
        model=WORLD_LLM_MODEL,
        temperature=0.2,
        max_tokens=500,
        messages=messages,
    )
    raw = (resp.choices[0].message.content or "").strip()
    blob = _extract_json(raw)
    if not blob:
        raise RuntimeError(f"LLM did not return JSON: {raw[:240]!r}")
    return _coerce_spec(blob)


def _image_b64(path: str) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    if len(data) > 8_000_000:
        return None
    ext = p.suffix.lower().lstrip(".") or "jpeg"
    if ext == "jpg":
        ext = "jpeg"
    return f"data:image/{ext};base64,{base64.b64encode(data).decode('ascii')}"


def analyze_text(text: str, *, simulate: bool = False) -> dict:
    text = text.strip()
    if not text:
        raise ValueError("empty text")
    low = text.lower()
    if simulate:
        if any(w in low for w in ("vacuum", "scary", "avoid", "hate")):
            return _coerce_spec({
                "canonical": "vacuum", "category": "appliance", "drive": "avoid", "valence": -0.85,
                "keywords": ["vacuum", "vacuum cleaner", "roomba", "cleaner", "robot vacuum"],
                "description": "Loud scary cleaning machine.", "note": text[:200],
            })
        if "cat" in low:
            return _coerce_spec({
                "canonical": "cat", "category": "animal", "drive": "chase", "valence": 0.8,
                "keywords": ["cat", "kitten", "tabby", "feline", "a cat", "kitty"],
                "description": "Beloved cat to chase and greet.", "note": text[:200],
            })
    return _llm_chat(
        [{"role": "system", "content": _SYSTEM_TEXT}, {"role": "user", "content": text}],
        simulate=simulate,
        fallback={"canonical": "thing", "drive": "approach", "keywords": [low.split()[-1]], "note": text},
    )


def analyze_image(path: str, note: str = "", *, simulate: bool = False) -> dict:
    note = note.strip()
    name = Path(path).stem.lower().replace("_", " ").replace("-", " ")
    if simulate:
        if "cat" in name or "cat" in note.lower():
            return analyze_text(note or f"a cat in {path}", simulate=True)
        return _coerce_spec({
            "canonical": name.split()[0] if name else "object",
            "drive": "approach", "valence": 0.2,
            "keywords": _norm_list(name.split()) or ["object"],
            "description": note or f"Something seen in {Path(path).name}",
            "note": note,
        })
    b64 = _image_b64(path)
    if b64 and WORLD_LLM_MULTIMODAL:
        user_content: list[dict] | str = [
            {"type": "text", "text": note or f"Describe what the pet should know about this scene ({Path(path).name})."},
            {"type": "image_url", "image_url": {"url": b64}},
        ]
        return _llm_chat(
            [{"role": "system", "content": _SYSTEM_IMAGE}, {"role": "user", "content": user_content}],
            simulate=False,
            fallback={"canonical": "object", "keywords": ["object"], "note": note},
        )
    return analyze_text(note or f"Image file {path} named {name}. What is this?", simulate=False)


def analyze_jsonl_line(line: str, *, simulate: bool = False) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return analyze_text(line, simulate=simulate)
    if not isinstance(rec, dict):
        return None
    target = rec.get("target")
    parts = []
    if target:
        parts.append(f"target was {target}")
    if rec.get("say"):
        parts.append(f"pet said: {rec['say']}")
    if rec.get("action"):
        parts.append(f"action: {rec['action']}")
    if rec.get("place"):
        parts.append(f"place: {rec['place']}")
    text = "; ".join(parts) or json.dumps(rec)[:400]
    if simulate and target:
        return analyze_text(f"chase {target}", simulate=True)
    return analyze_text(text, simulate=simulate)


def label_matches_concept(detection_label: str, keywords: list[str]) -> bool:
    """True if a detector label matches any concept keyword (runtime, no LLM)."""
    lab = detection_label.strip().lower()
    if not lab:
        return False
    tokens = set(re.split(r"[\s_/\-]+", lab))
    for kw in keywords:
        k = kw.strip().lower()
        if not k:
            continue
        if k == lab or k in lab or lab in k:
            return True
        if k in tokens or any(k in t or t in k for t in tokens):
            return True
    return False
