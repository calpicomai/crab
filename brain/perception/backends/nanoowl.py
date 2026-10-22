"""NanoOwlBackend — open-vocabulary detection via NVIDIA NanoOWL (OWL-ViT + TensorRT).

Text-prompted ("a person", "a red ball", ...), so the future LLM can steer what
the robot looks for at runtime via set_prompts() / the /prompts endpoint.

Requires (JetPack): torch, transformers, torch2trt, TensorRT, the `nanoowl`
package from source, and a prebuilt image-encoder engine:
    python3 -m nanoowl.build_image_encoder_engine data/owl_image_encoder_patch32.engine
Everything is imported lazily inside load().
"""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path

import numpy as np

from .. import config
from ..types import Detection
from .base import DetectorBackend

logger = logging.getLogger("perception.nanoowl")

_SUPPORTED_MODELS = {"google/owlvit-base-patch32", "google/owlvit-base-patch16",
                     "google/owlvit-large-patch14"}


def _cached_model(model: str) -> Path:
    """Resolve a complete existing HF snapshot without querying the network."""
    if model not in _SUPPORTED_MODELS:
        raise ValueError("NanoOWL requires a supported OWL-ViT model ID with an existing local HF cache")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import snapshot_download, constants

    if not constants.HF_HUB_OFFLINE:
        raise RuntimeError("Hugging Face was imported online; restart with HF_HUB_OFFLINE=1")
    path = Path(snapshot_download(repo_id=model, local_files_only=True))
    required = ["config.json", "preprocessor_config.json", "tokenizer_config.json", "vocab.json", "merges.txt"]
    for filename in required:
        file = path / filename
        if not file.is_file() or file.stat().st_size == 0:
            raise FileNotFoundError(f"NanoOWL cache incomplete: {filename}; provision model before runtime")
    weight_files = [path / name for name in ("model.safetensors", "pytorch_model.bin")]
    if not any(file.is_file() and file.stat().st_size for file in weight_files):
        indices = [path / name for name in ("model.safetensors.index.json", "pytorch_model.bin.index.json")]
        index = next((file for file in indices if file.is_file()), None)
        if index is None:
            raise FileNotFoundError("NanoOWL cache has no model weights")
        shards = list(json.loads(index.read_text())["weight_map"].values())
        if not shards or not all((path / shard).is_file() and (path / shard).stat().st_size for shard in shards):
            raise FileNotFoundError("NanoOWL cache has missing weight shards")
    return path


def _to_list(value) -> list:
    """Best-effort convert a torch tensor / ndarray / sequence to a Python list."""
    if value is None:
        return []
    for attr in ("detach", "cpu"):  # torch tensor -> cpu
        if hasattr(value, attr):
            value = getattr(value, attr)()
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


class NanoOwlBackend(DetectorBackend):
    name = "nanoowl"

    def __init__(
        self,
        model: str | None = None,
        engine: str | None = None,
        threshold: float | None = None,
        prompts: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.model = model or config.NANOOWL_MODEL
        self.engine = engine or config.NANOOWL_ENGINE
        self.threshold = threshold if threshold is not None else config.NANOOWL_THRESHOLD
        self.prompts = list(prompts if prompts is not None else config.NANOOWL_PROMPTS)
        self._predictor = None
        self._text_encodings = None

    def load(self) -> None:
        engine = Path(self.engine).expanduser()
        if not engine.is_file() or engine.stat().st_size == 0:
            raise FileNotFoundError("NanoOWL requires a local prebuilt TensorRT engine; provision before runtime")
        cached = _cached_model(self.model)
        from transformers.utils import is_offline_mode

        if not is_offline_mode():
            raise RuntimeError("Transformers was imported online; restart with TRANSFORMERS_OFFLINE=1")
        from nanoowl.owl_predictor import OwlPredictor  # heavy: torch2trt + TensorRT

        logger.info("Loading cached NanoOWL (%s, cache=%s, engine=%s)", self.model, cached, engine)
        self._predictor = OwlPredictor(self.model, image_encoder_engine=str(engine.resolve()))
        self._encode_prompts()
        self._loaded = True

    def unload(self) -> None:
        self._predictor = None
        self._text_encodings = None
        self._loaded = False
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def set_prompts(self, prompts: list[str]) -> None:
        self.prompts = list(prompts)
        if self._loaded:
            self._encode_prompts()

    def _encode_prompts(self) -> None:
        # Precompute text encodings so per-frame predict() only runs the image path.
        if self._predictor is not None and self.prompts:
            self._text_encodings = self._predictor.encode_text(self.prompts)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if self._predictor is None or not self.prompts:
            return []
        from PIL import Image

        # BGR (OpenCV) -> RGB PIL image.
        image = Image.fromarray(frame[:, :, ::-1])
        output = self._predictor.predict(
            image=image,
            text=self.prompts,
            text_encodings=self._text_encodings,
            threshold=self.threshold,
        )

        boxes = _to_list(getattr(output, "boxes", None))
        labels = _to_list(getattr(output, "labels", None))
        scores = _to_list(getattr(output, "scores", None))

        detections: list[Detection] = []
        for i, box in enumerate(boxes):
            x1, y1, x2, y2 = (int(v) for v in box[:4])
            label_idx = int(labels[i]) if i < len(labels) else 0
            label = self.prompts[label_idx] if 0 <= label_idx < len(self.prompts) else str(label_idx)
            score = float(scores[i]) if i < len(scores) else 0.0
            detections.append(
                Detection(label=label, score=score, box=[x1, y1, x2, y2], source=self.name)
            )
        return detections
