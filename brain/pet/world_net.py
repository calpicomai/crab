"""Tiny neural outcome predictor for the world model (laptop train → Jetson runtime).

Replaces Laplace-smoothed ``WorldModel.predict()`` when a trained weights file
exists. A single hidden-layer MLP maps (clearance band, mood, action) →
(reflex_p, progress_p). Training uses pure Python — no torch/sklearn required.

This is the first *learnable* neural piece of the world model: weights update from
experience logs / the outcomes table, then deploy like ``world.db``.

    python -m brain.pet.world_train train-net --db ~/.picrawler_pet/world.db
    # or from pet JSONL:
    python -m brain.pet.world_train train-net --jsonl run.jsonl

On the Jetson, set ``PET_WORLD_NET=~/.picrawler_pet/world_net.json`` (or place the
file at the default path next to ``world.db``).
"""

from __future__ import annotations

import json
import math
import os
import random
import re
from pathlib import Path

# Default weights path sits beside world.db in PET_HOME.
from . import config as pet_config

PET_WORLD_NET: str = os.environ.get(
    "PET_WORLD_NET",
    os.path.join(pet_config.PET_HOME, "world_net.json"),
)

_CLEARANCE = ("tight", "near", "open")
_ACTIONS = ("walk", "turn", "stand", "sit")
_MOODS = (
    "curious", "playful", "alert", "bored", "sleepy", "happy", "scared",
    "excited", "calm", "?", "unknown",
)
# input = 3 clearance + len(MOODS) mood + len(ACTIONS) action
_IN = len(_CLEARANCE) + len(_MOODS) + len(_ACTIONS)
_HIDDEN = 12
_OUT = 2
_MIN_SAMPLES = 6


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _parse_context(context: str) -> tuple[str, str]:
    """``tight/curious`` → (clearance band, mood)."""
    parts = (context or "?/?").split("/", 1)
    band = parts[0].strip().lower() if parts else "open"
    mood = parts[1].strip().lower() if len(parts) > 1 else "?"
    if band not in _CLEARANCE:
        band = "open"
    if mood not in _MOODS:
        mood = "?"
    return band, mood


def encode(context: str, action: str) -> list[float]:
    band, mood = _parse_context(context)
    act = action.strip().lower()
    if act not in _ACTIONS:
        act = "walk"
    vec = [0.0] * _IN
    vec[_CLEARANCE.index(band)] = 1.0
    vec[len(_CLEARANCE) + _MOODS.index(mood)] = 1.0
    vec[len(_CLEARANCE) + len(_MOODS) + _ACTIONS.index(act)] = 1.0
    return vec


def _matvec(w: list[list[float]], x: list[float]) -> list[float]:
    return [sum(wi * xi for wi, xi in zip(row, x)) for row in w]


def _add_vec(a: list[float], b: list[float]) -> list[float]:
    return [x + y for x, y in zip(a, b)]


def _rand_weights() -> dict:
    rng = random.Random(42)

    def layer(r: int, c: int) -> list[list[float]]:
        scale = math.sqrt(2.0 / (r + c))
        return [[rng.uniform(-scale, scale) for _ in range(c)] for _ in range(r)]

    return {
        "version": 1,
        "in": _IN,
        "hidden": _HIDDEN,
        "out": _OUT,
        "w1": layer(_HIDDEN, _IN),
        "b1": [0.0] * _HIDDEN,
        "w2": layer(_OUT, _HIDDEN),
        "b2": [0.0] * _OUT,
        "samples": 0,
    }


def forward(weights: dict, x: list[float]) -> tuple[float, float]:
    h = _add_vec(_matvec(weights["w1"], x), weights["b1"])
    h = [_sigmoid(v) for v in h]
    o = _add_vec(_matvec(weights["w2"], h), weights["b2"])
    return _sigmoid(o[0]), _sigmoid(o[1])


def predict(weights: dict, context: str, action: str) -> dict:
    reflex_p, progress_p = forward(weights, encode(context, action))
    return {
        "n": int(weights.get("samples", 0)),
        "reflex_p": reflex_p,
        "progress_p": progress_p,
        "neural": True,
    }


def _load(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        if data.get("version") != 1:
            return None
        return data
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def load(path: str | None = None) -> dict | None:
    return _load(path or PET_WORLD_NET)


def save(weights: dict, path: str | None = None) -> Path:
    dest = Path(path or PET_WORLD_NET)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(weights, indent=2))
    return dest


def predict_if_loaded(context: str, action: str, path: str | None = None) -> dict | None:
    w = load(path)
    if w is None or int(w.get("samples", 0)) < _MIN_SAMPLES:
        return None
    return predict(w, context, action)


def _training_rows_from_outcomes(rows: list[dict]) -> list[tuple[list[float], float, float]]:
    out: list[tuple[list[float], float, float]] = []
    for row in rows:
        ctx, act = row["context"], row["action"]
        tries = max(1, int(row.get("tries", 1)))
        reflex = float(row.get("reflex", 0)) / tries
        prog = float(row.get("progressed", 0)) / tries
        # Expand each aggregate row by its try count (capped) for more gradient signal.
        reps = min(tries, 20)
        x = encode(ctx, act)
        for _ in range(reps):
            out.append((x, reflex, prog))
    return out


def _training_rows_from_jsonl(path: Path) -> list[tuple[list[float], float, float]]:
    out: list[tuple[list[float], float, float]] = []
    mood_re = re.compile(r"^[a-z_]+$")
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            action = str(rec.get("action") or "walk").lower()
            mood = str(rec.get("mood") or "?").lower()
            if not mood_re.match(mood):
                mood = "?"
            dist = rec.get("distance_cm")
            if dist is None:
                band = "open"
            elif dist < 30:
                band = "tight"
            elif dist < 80:
                band = "near"
            else:
                band = "open"
            ctx = f"{band}/{mood}"
            if "reflex" in rec:
                reflex = 1.0 if rec["reflex"] else 0.0
            else:
                reflex = 0.0 if rec.get("forward_clear", True) else 1.0
            prog = 0.0 if reflex > 0.5 else (1.0 if action == "walk" else 0.0)
            out.append((encode(ctx, action), reflex, prog))
    return out


def train(
    samples: list[tuple[list[float], float, float]],
    *,
    epochs: int = 400,
    lr: float = 0.08,
) -> dict:
    """Train a fresh MLP on (features, reflex_rate, progress_rate) tuples."""
    if len(samples) < _MIN_SAMPLES:
        raise ValueError(f"need at least {_MIN_SAMPLES} samples, got {len(samples)}")
    w = _rand_weights()
    for _ in range(epochs):
        for x, t_reflex, t_prog in samples:
            # Forward
            z1 = _add_vec(_matvec(w["w1"], x), w["b1"])
            h = [_sigmoid(v) for v in z1]
            z2 = _add_vec(_matvec(w["w2"], h), w["b2"])
            o0, o1 = _sigmoid(z2[0]), _sigmoid(z2[1])
            # BCE gradients on outputs
            d_o0 = o0 - t_reflex
            d_o1 = o1 - t_prog
            d_z2 = [d_o0 * o0 * (1 - o0), d_o1 * o1 * (1 - o1)]
            # Backprop to hidden
            d_h = [0.0] * _HIDDEN
            for j in range(_HIDDEN):
                d_h[j] = (d_z2[0] * w["w2"][0][j] + d_z2[1] * w["w2"][1][j])
            d_z1 = [d_h[j] * h[j] * (1 - h[j]) for j in range(_HIDDEN)]
            # Update w2, b2
            for k in range(_OUT):
                dz = d_z2[k]
                for j in range(_HIDDEN):
                    w["w2"][k][j] -= lr * dz * h[j]
                w["b2"][k] -= lr * dz
            # Update w1, b1
            for j in range(_HIDDEN):
                dz = d_z1[j]
                for i in range(_IN):
                    w["w1"][j][i] -= lr * dz * x[i]
                w["b1"][j] -= lr * dz
    w["samples"] = len(samples)
    return w


def train_from_outcomes(rows: list[dict], **kwargs) -> dict:
    return train(_training_rows_from_outcomes(rows), **kwargs)


def train_from_jsonl(path: str | Path, **kwargs) -> dict:
    return train(_training_rows_from_jsonl(Path(path)), **kwargs)


def _self_test() -> int:
    samples = []
    for band in ("tight", "near", "open"):
        for mood in ("curious", "bored"):
            for action in ("walk", "turn"):
                reflex = 0.9 if band == "tight" and action == "walk" else 0.1
                prog = 1.0 - reflex if action == "walk" else 0.0
                samples.append((encode(f"{band}/{mood}", action), reflex, prog))
    w = train(samples, epochs=600)
    p = predict(w, "tight/curious", "walk")
    p_open = predict(w, "open/bored", "walk")
    assert p["reflex_p"] > p_open["reflex_p"], (p, p_open)
    assert p["neural"]
    print(f"  train-net self-test: tight/curious walk reflex~{p['reflex_p']:.0%}")
    print("All world_net self-test assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
