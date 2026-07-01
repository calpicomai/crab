"""FastAPI command server — runs on the Raspberry Pi.

Each endpoint parses a command model from shared/protocol.py, invokes the
matching GaitEngine ability, and returns a uniform CommandResponse. Paths come
from shared.ACTION_PATHS so the client and server can never disagree.

Run directly:
    PICRAWLER_SIMULATE=1 python -m robot.server        # dev / off-hardware
    python -m robot.server                             # on the Pi (real servos)
or via the systemd unit (robot/systemd/picrawler-server.service).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from shared import (
    ACTION_PATHS,
    HEALTH_PATH,
    Action,
    CommandResponse,
    GetStatusCommand,
    SitCommand,
    StandCommand,
    TurnCommand,
    WalkCommand,
)

from . import config
from .gait import GaitEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("picrawler.server")

app = FastAPI(title="PiCrawler Robot Server", version="0.1.0")

# Single engine instance, built at startup and shared across requests.
engine = GaitEngine(simulate=config.SIMULATE)


@app.get(HEALTH_PATH)
def health() -> dict[str, object]:
    """Liveness probe for systemd / monitoring. Does not move the robot."""
    return {"ok": True, "simulate": engine.simulate}


@app.post(ACTION_PATHS[Action.WALK], response_model=CommandResponse)
def walk(cmd: WalkCommand) -> CommandResponse:
    engine.walk(cmd.steps, cmd.speed)
    return CommandResponse(
        ok=True,
        action=Action.WALK,
        detail=f"walked {cmd.steps} step(s) at speed {cmd.speed}",
        status=engine.get_status(),
    )


@app.post(ACTION_PATHS[Action.TURN], response_model=CommandResponse)
def turn(cmd: TurnCommand) -> CommandResponse:
    engine.turn(cmd.degrees, cmd.speed)
    return CommandResponse(
        ok=True,
        action=Action.TURN,
        detail=f"turned {cmd.degrees} deg at speed {cmd.speed}",
        status=engine.get_status(),
    )


@app.post(ACTION_PATHS[Action.STAND], response_model=CommandResponse)
def stand(_cmd: StandCommand) -> CommandResponse:
    engine.stand()
    return CommandResponse(ok=True, action=Action.STAND, detail="standing", status=engine.get_status())


@app.post(ACTION_PATHS[Action.SIT], response_model=CommandResponse)
def sit(_cmd: SitCommand) -> CommandResponse:
    engine.sit()
    return CommandResponse(ok=True, action=Action.SIT, detail="sitting", status=engine.get_status())


@app.post(ACTION_PATHS[Action.GET_STATUS], response_model=CommandResponse)
def get_status(_cmd: GetStatusCommand) -> CommandResponse:
    return CommandResponse(ok=True, action=Action.GET_STATUS, detail="status", status=engine.get_status())


def main() -> None:
    import uvicorn

    logger.info("Starting PiCrawler server on %s:%d (simulate=%s)", config.HOST, config.PORT, engine.simulate)
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
