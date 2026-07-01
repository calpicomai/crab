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
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse

from shared import (
    ACTION_PATHS,
    CAMERA_FRAME_PATH,
    CAMERA_STREAM_PATH,
    HEALTH_PATH,
    Action,
    CommandResponse,
    GetStatusCommand,
    SitCommand,
    StandCommand,
    TestLegCommand,
    TurnCommand,
    WalkCommand,
)

from . import config
from .camera import PiCamera
from .gait import GaitEngine
from .sensors import DistanceSensor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("picrawler.server")

# Single engine instance, built at import and shared across requests.
engine = GaitEngine(simulate=config.SIMULATE)

# Camera (optional) — captures on the Pi, served as MJPEG to the Jetson.
camera = (
    PiCamera(
        width=config.CAMERA_WIDTH,
        height=config.CAMERA_HEIGHT,
        fps=config.CAMERA_FPS,
        quality=config.CAMERA_QUALITY,
        simulate=config.SIMULATE,
    )
    if config.CAMERA_ENABLED
    else None
)

# Ultrasonic distance sensor (optional) — read into the status response.
distance_sensor = (
    DistanceSensor(
        trig=config.ULTRASONIC_TRIG,
        echo=config.ULTRASONIC_ECHO,
        simulate=config.SIMULATE,
    )
    if config.ULTRASONIC_ENABLED
    else None
)

# Wire the ultrasonic into the gait engine's reflex: walk() then checks forward
# clearance between gait cycles and aborts early if something's too close. With no
# sensor (disabled / simulate laptop) the reflex stays inert.
if distance_sensor is not None:
    engine.clearance_fn = distance_sensor.read_cm


def _status_with_distance():
    """Engine status augmented with the current ultrasonic clearance."""
    status = engine.get_status()
    if distance_sensor is not None:
        status.distance_cm = distance_sensor.read_cm()
    return status


def _home_on_start() -> None:
    """Gently move to config.HOME_ON_START so the robot doesn't sit splayed.

    Runs the same staged, low-speed stand/sit as the commands. Skipped for
    "none" / unrecognised values. This is why it's safe to auto-start via
    systemd: only a controlled, one-leg-at-a-time motion happens at boot.
    """
    pose = config.HOME_ON_START
    if pose == "stand":
        logger.info("Homing to STAND on startup (staged)")
        engine.stand()
    elif pose == "sit":
        logger.info("Homing to SIT on startup (staged)")
        engine.sit()
    elif pose in ("", "none"):
        logger.info("No startup homing (PICRAWLER_HOME_ON_START=none)")
    else:
        logger.warning("Unknown PICRAWLER_HOME_ON_START=%r; skipping startup homing", pose)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if camera is not None:
        camera.start()
    _home_on_start()
    yield
    if camera is not None:
        camera.stop()


app = FastAPI(title="PiCrawler Robot Server", version="0.1.0", lifespan=lifespan)


@app.get(HEALTH_PATH)
def health() -> dict[str, object]:
    """Liveness probe for systemd / monitoring. Does not move the robot."""
    return {
        "ok": True,
        "simulate": engine.simulate,
        "camera": None if camera is None else {"enabled": True, "simulate": camera.simulate},
    }


@app.get(CAMERA_STREAM_PATH)
def camera_stream() -> StreamingResponse:
    """MJPEG stream (multipart/x-mixed-replace) the Jetson perception pulls from."""
    if camera is None:
        return Response(status_code=503, content="camera disabled")
    return StreamingResponse(
        camera.mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get(CAMERA_FRAME_PATH)
def camera_frame() -> Response:
    """A single most-recent JPEG (handy for a quick check / low-rate pulls)."""
    if camera is None:
        return Response(status_code=503, content="camera disabled")
    frame = camera.get_frame()
    if frame is None:
        return Response(status_code=503, content="no frame yet")
    return Response(content=frame, media_type="image/jpeg")


@app.post(ACTION_PATHS[Action.WALK], response_model=CommandResponse)
def walk(cmd: WalkCommand) -> CommandResponse:
    engine.walk(cmd.steps, cmd.speed, min_clearance_cm=cmd.min_clearance_cm)
    status = _status_with_distance()
    status.reflex_stopped = engine.reflex_stopped
    if engine.reflex_stopped:
        detail = f"reflex-stopped mid-walk (clearance {status.distance_cm}cm) during {cmd.steps}-step walk"
    else:
        detail = f"walked {cmd.steps} step(s) at speed {cmd.speed}"
    return CommandResponse(ok=True, action=Action.WALK, detail=detail, status=status)


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
    return CommandResponse(ok=True, action=Action.GET_STATUS, detail="status", status=_status_with_distance())


@app.post(ACTION_PATHS[Action.TEST_LEG], response_model=CommandResponse)
def test_leg(cmd: TestLegCommand) -> CommandResponse:
    """Diagnostic: gently move one leg to the standing pose (see robot/diagnose.py)."""
    engine.test_leg(cmd.leg, cmd.speed)
    return CommandResponse(
        ok=True,
        action=Action.TEST_LEG,
        detail=f"moved leg {cmd.leg} to standing pose at speed {cmd.speed}",
        status=engine.get_status(),
    )


def main() -> None:
    import uvicorn

    logger.info("Starting PiCrawler server on %s:%d (simulate=%s)", config.HOST, config.PORT, engine.simulate)
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
