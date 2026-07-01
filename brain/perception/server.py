"""FastAPI perception service — runs on the Jetson.

Mirrors the robot command server's style. Serves the latest detections and lets
a caller steer open-vocabulary prompts and manage which detectors are resident
(RAM budget).

    GET  /health   -> {ok, simulate, backends}
    GET  /snapshot -> PerceptionSnapshot (captures a frame + runs loaded backends)
    POST /prompts  -> {prompts:[...]}  set NanoOWL open-vocab prompts
    POST /load     -> {backend:"nanoowl"}  load a detector
    POST /unload   -> {backend:"nanoowl"}  free a detector's RAM

Run:
    python -m brain.perception.server
    PERCEPTION_SIMULATE=1 python -m brain.perception.server   # off-hardware
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel

from . import config
from .engine import PerceptionEngine
from .types import PerceptionSnapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("perception.server")

app = FastAPI(title="PiCrawler Perception Server", version="0.1.0")

# Single engine instance shared across requests.
engine = PerceptionEngine()


class PromptsBody(BaseModel):
    prompts: list[str]


class BackendBody(BaseModel):
    backend: str


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "simulate": engine.simulate, "backends": engine.loaded_backends()}


@app.get("/snapshot", response_model=PerceptionSnapshot)
def snapshot() -> PerceptionSnapshot:
    return engine.detect()


@app.post("/prompts")
def set_prompts(body: PromptsBody) -> dict[str, object]:
    engine.set_prompts(body.prompts)
    return {"ok": True, "prompts": engine.prompts}


@app.post("/load")
def load(body: BackendBody) -> dict[str, object]:
    engine.load(body.backend)
    return {"ok": True, "backends": engine.loaded_backends()}


@app.post("/unload")
def unload(body: BackendBody) -> dict[str, object]:
    engine.unload(body.backend)
    return {"ok": True, "backends": engine.loaded_backends()}


def main() -> None:
    import uvicorn

    logger.info(
        "Starting Perception server on %s:%d (simulate=%s, backends=%s)",
        config.HOST,
        config.PORT,
        engine.simulate,
        engine.loaded_backends(),
    )
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
