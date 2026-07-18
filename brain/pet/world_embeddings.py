"""Local text embeddings for semantic concept matching (laptop train → Jetson runtime).

Training uses Ollama's embedding API (``nomic-embed-text`` by default). Runtime on
the Jetson compares detector labels to stored concept vectors with cosine similarity
— still no LLM per tick, only a small numpy dot product.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct

WORLD_EMBED_BASE_URL: str = os.environ.get(
    "WORLD_EMBED_BASE_URL",
    os.environ.get("WORLD_LLM_BASE_URL", os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")),
)
WORLD_EMBED_MODEL: str = os.environ.get("WORLD_EMBED_MODEL", "nomic-embed-text")
WORLD_EMBED_API_KEY: str = os.environ.get("WORLD_EMBED_API_KEY", os.environ.get("LLM_API_KEY", "sk-local"))
WORLD_EMBED_THRESHOLD: float = float(os.environ.get("WORLD_EMBED_THRESHOLD", "0.52"))
WORLD_EMBED_DIM: int = int(os.environ.get("WORLD_EMBED_DIM", "64"))
# Jetson default: keyword match only. Enable to embed each detector label at runtime
# (needs the same Ollama embed model as training — still no VLM per tick).
WORLD_EMBED_AT_RUNTIME: bool = os.environ.get("WORLD_EMBED_AT_RUNTIME", "").strip().lower() in {
    "1", "true", "yes", "on",
}


def _simulate_vector(text: str, dim: int = WORLD_EMBED_DIM) -> list[float]:
    """Deterministic pseudo-embedding for tests / offline."""
    seed = hashlib.sha256(text.strip().lower().encode()).digest()
    out = []
    for i in range(dim):
        chunk = seed[i % len(seed): (i % len(seed)) + 4]
        if len(chunk) < 4:
            chunk = (chunk + seed)[:4]
        val = struct.unpack("!i", chunk.ljust(4, b"\0")[:4])[0]
        out.append((val % 1000) / 1000.0 - 0.5)
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


def embed_text(text: str, *, simulate: bool = False) -> list[float]:
    text = text.strip()
    if not text:
        return _simulate_vector("empty")
    if simulate:
        return _simulate_vector(text)
    from openai import OpenAI

    client = OpenAI(base_url=WORLD_EMBED_BASE_URL, api_key=WORLD_EMBED_API_KEY, timeout=60.0)
    resp = client.embeddings.create(model=WORLD_EMBED_MODEL, input=text)
    return list(resp.data[0].embedding)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def parse_embedding(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        vec = json.loads(raw)
        return [float(x) for x in vec] if isinstance(vec, list) and vec else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def serialize_embedding(vec: list[float] | None) -> str:
    if not vec:
        return "[]"
    return json.dumps([round(float(x), 6) for x in vec])


def concept_embed_text(spec: dict) -> str:
    """Text blob to embed for a concept — canonical + gloss + keywords."""
    kw = spec.get("keywords") or []
    if isinstance(kw, str):
        kw = [kw]
    parts = [
        spec.get("canonical", ""),
        spec.get("category", ""),
        spec.get("description", ""),
        spec.get("note", ""),
        " ".join(kw[:20]),
    ]
    return ". ".join(p.strip() for p in parts if p and str(p).strip())


def best_embedding_match(
    label: str,
    concepts: list[tuple[object, list[float]]],
    *,
    threshold: float | None = None,
    simulate: bool = False,
) -> object | None:
    """Return the concept row with highest cosine similarity above threshold."""
    threshold = WORLD_EMBED_THRESHOLD if threshold is None else threshold
    query = embed_text(label, simulate=simulate)
    best_row = None
    best_score = threshold
    for row, vec in concepts:
        if not vec:
            continue
        score = cosine(query, vec)
        if score > best_score:
            best_score = score
            best_row = row
    return best_row
