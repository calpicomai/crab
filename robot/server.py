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
import asyncio
import threading
import queue
import time
from dataclasses import asdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, ConfigDict, ValidationError

from shared import (
    ACTION_PATHS,
    AUDIO_SESSION_HEADER, AUDIO_SEQUENCE_HEADER, AUDIO_LEASE_HEADER, AudioEnvelope,
    AUDIO_HEALTH_PATH, AUDIO_SELF_TEST_PATH, AUDIO_STOP_PATH, CONTROL_LEASE_PATH,
    COMPANION_EVENTS_PATH, StopCommand, ResumeCommand, TurnStepsCommand, MotionResult,
    AUDIO_PLAY_PATH,
    AUDIO_STREAM_PATH,
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

from shared.health import InputHealth
from shared.speech import interpret
from .control import CommandLeases
from . import config
from .audio import PiMic, PiSpeaker
from .camera import PiCamera
from .gait import GaitEngine
from .sensors import BatterySensor, DistanceSensor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("picrawler.server")
leases = CommandLeases()
command_lock = threading.Lock()
# This short lock orders STOP against lease consumption + latch clearing for
# RESUME. Never hold it while moving, reading sensors or stopping audio.
control_transition_lock = threading.Lock()
companion_events: queue.Queue = queue.Queue(maxsize=32)
events_lock = threading.Lock()
companion_urgent = None

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

# Battery voltage monitor (optional) — reported on /status so the brain can slow/rest
# the pet when the pack runs low. Missing robot_hat -> simulate (healthy voltage).
battery_sensor = (
    BatterySensor(
        channel=config.BATTERY_ADC_CHANNEL,
        scale=config.BATTERY_SCALE,
        simulate=config.SIMULATE,
    )
    if config.BATTERY_ENABLED
    else None
)

engine.battery_fn = battery_sensor.read_v if battery_sensor is not None else lambda: None

# Audio device (optional) — mic captured here + streamed to the brain (Whisper);
# speaker plays WAVs the brain sends (Piper). Missing ALSA/device -> simulate.
mic = PiMic(rate=config.MIC_RATE, device=config.MIC_DEVICE, simulate=config.SIMULATE) if config.AUDIO_ENABLED else None
speaker = PiSpeaker(device=config.SPEAKER_DEVICE, simulate=config.SIMULATE) if config.AUDIO_ENABLED else None

if mic is not None and speaker is not None:
    mic.suppress = speaker.suppressed

# --- Simulator world (dev / off-hardware) -------------------------------------
# When simulating and PICRAWLER_SIM_WORLD=1, back the gait/sonar/camera with a 2D
# world so the robot actually moves in a space and the sonar sees real obstacles.
# A live dashboard is served at /sim. Not part of the robot<->brain protocol.
sim_world = None
_sim_brain: dict = {}  # latest telemetry pushed by the brain loop, for the dashboard
if config.SIM_WORLD and engine.simulate:
    from .simworld import build_scenario

    sim_world = build_scenario(config.SIM_SCENARIO)
    engine.world = sim_world
    if distance_sensor is not None:
        distance_sensor.world = sim_world
    if camera is not None:
        camera.world = sim_world
    logger.info("SIM_WORLD enabled (scenario=%s) — dashboard at /sim", config.SIM_SCENARIO)


def _status_with_distance():
    """Engine status augmented with the current ultrasonic clearance."""
    status = engine.get_status()
    if distance_sensor is not None:
        status.distance_cm = distance_sensor.read_cm()
    if battery_sensor is not None:
        status.battery_v = battery_sensor.read_v()
    status.observed_at = time.time()
    status.boot_id = leases.boot_id
    status.motion_state = engine.motion_state()
    for name, device in (("ultrasonic", distance_sensor), ("battery", battery_sensor),
                         ("camera", camera), ("microphone", mic), ("speaker", speaker)):
        if device is None:
            status.inputs[name] = InputHealth(available=False, provenance="unknown",
                                              failure_reason="disabled by configuration")
        else:
            report = device.input_health() if hasattr(device, "input_health") else device.health()
            if hasattr(report, "model_dump"):
                report = report.model_dump()
            status.inputs[name] = InputHealth.model_validate(report)
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


@app.exception_handler(RequestValidationError)
async def invalid_command(_request: Request, exc: RequestValidationError):
    # Do not echo arbitrary JSON input (NaN/Infinity cannot be serialized safely).
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]})



@app.get(HEALTH_PATH)
def health() -> dict[str, object]:
    """Liveness probe for systemd / monitoring. Does not move the robot."""
    status = _status_with_distance()
    return {
        "ok": True, "boot_id": leases.boot_id, "simulate": engine.simulate,
        "hardware_verified": False,
        "motion": status.motion_state,
        "inputs": {name: item.snapshot() for name, item in status.inputs.items()},
        "audio": audio_health(),
        "limitations": ["no measured odometry or joint feedback", "no cliff guarantee",
                        "stop takes effect between vendor motion boundaries", "physical power cutoff required"],
    }


@app.get(CAMERA_STREAM_PATH)
def camera_stream() -> StreamingResponse:
    """MJPEG stream (multipart/x-mixed-replace) the Jetson perception pulls from."""
    if camera is None:
        return Response(status_code=503, content="camera disabled")
    return StreamingResponse(
        camera.mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"X-Crab-Provenance": "simulated" if camera.simulate else "real"},
    )


@app.get(CAMERA_FRAME_PATH)
def camera_frame() -> Response:
    """A single most-recent JPEG (handy for a quick check / low-rate pulls)."""
    if camera is None:
        return Response(status_code=503, content="camera disabled")
    frame = camera.get_frame()
    if frame is None:
        return Response(status_code=503, content="no frame yet")
    return Response(content=frame, media_type="image/jpeg", headers={"X-Crab-Provenance": "simulated" if camera.simulate else "real",
                 "X-Crab-Captured-At": str(camera.health().captured_at or 0)})


@app.get(AUDIO_STREAM_PATH)
def audio_stream() -> Response:
    """Raw mic PCM (S16_LE mono @ MIC_RATE) the Jetson pulls for Whisper STT."""
    if mic is None:
        return Response(status_code=503, content="audio disabled")
    return StreamingResponse(mic.stream(), media_type="audio/L16",
                             headers={"X-Audio-Provenance": "simulated" if mic.simulate else "real"})


@app.get(AUDIO_HEALTH_PATH)
def audio_health() -> dict:
    return {"speaker": speaker.health() if speaker else {"available": False, "reason": "disabled"},
            "microphone": mic.health() if mic else {"available": False, "reason": "disabled"},
            "last_playback": speaker.last_result if speaker else None}


class AudioSelfTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    play: bool = False


@app.post(AUDIO_SELF_TEST_PATH)
def audio_self_test(cmd: AudioSelfTest) -> dict:
    if speaker is None:
        return {"ok": False, "played": False, "reason": "audio disabled"}
    return speaker.self_test(play=cmd.play)


@app.post(AUDIO_STOP_PATH)
def audio_stop(request: Request) -> Response:
    try:
        envelope = AudioEnvelope(session_id=request.headers.get(AUDIO_SESSION_HEADER),
                                 sequence=request.headers.get(AUDIO_SEQUENCE_HEADER))
    except ValidationError:
        return JSONResponse(status_code=422, content={"ok": False, "reason": "audio session and sequence headers required"})
    leases.invalidate()
    result = speaker.stop(**envelope.model_dump()) if speaker else {"ok": True, "played": False, "reason": "audio disabled"}
    return JSONResponse(result)


@app.post(AUDIO_PLAY_PATH)
async def audio_play(request: Request) -> Response:
    if speaker is None:
        return JSONResponse(status_code=503, content={"ok": False, "played": False, "reason": "audio disabled"})
    try:
        envelope = AudioEnvelope(session_id=request.headers.get(AUDIO_SESSION_HEADER),
                                 sequence=request.headers.get(AUDIO_SEQUENCE_HEADER))
    except ValidationError:
        return JSONResponse(status_code=422, content={"ok": False, "played": False, "reason": "audio session and sequence headers required"})
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 2 * 1024 * 1024:
            return JSONResponse(status_code=413, content={"ok": False, "played": False, "reason": "WAV exceeds 2 MiB limit"})
    result = await asyncio.to_thread(speaker.play_result, bytes(data), **envelope.model_dump(),
        authorize=lambda: leases.consume(request.headers.get(AUDIO_LEASE_HEADER),
                                         f"audio:{envelope.session_id}:{envelope.sequence}"))
    return JSONResponse(status_code=200 if result.get("played") or result.get("simulate") else 503, content=result)


class CompanionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)


@app.post(COMPANION_EVENTS_PATH)
def companion_event(event: CompanionEvent) -> dict:
    global companion_urgent
    item = {"text": event.text, "timestamp": time.time(), "source": "dashboard"}
    command = interpret(event.text)
    urgent = command and command.name in {"stop", "stay", "scold"}
    with events_lock:
        if urgent:
            # Stop also works if the brain/model is down or its input queue is full.
            companion_urgent = item
            while not companion_events.empty():
                companion_events.get_nowait()
        else:
            try:
                companion_events.put_nowait(item)
            except queue.Full:
                return {"ok": False, "reason": "input queue full"}
    if urgent:
        stop(StopCommand())
    return {"ok": True}


@app.get(COMPANION_EVENTS_PATH)
def read_companion_events() -> dict:
    global companion_urgent
    events = []
    with events_lock:
        if companion_urgent is not None:
            # A stop remains latched even if its text expires before delivery.
            events.append(companion_urgent)
            companion_urgent = None
        while True:
            try:
                event = companion_events.get_nowait()
            except queue.Empty:
                break
            if time.time() - event["timestamp"] <= 5:
                events.append(event)
    return {"events": events}


@app.post(CONTROL_LEASE_PATH)
def command_lease() -> dict:
    return leases.issue()


# --------------------------------------------------------------------------- #
# Simulator dashboard + control (dev-only; present only when SIM_WORLD is on).
# NOT part of the robot<->brain command protocol.
# --------------------------------------------------------------------------- #
class ObstacleBody(BaseModel):
    action: str = "add"           # "add" | "remove"
    cx: float
    cy: float
    r: float = 14.0
    label: str = "obstacle"


class ControlBody(BaseModel):
    action: str                   # "pause" | "resume" | "reset" | "scenario"
    scenario: str | None = None


@app.get("/sim")
def sim_dashboard() -> Response:
    # Served even without the sim world: on the real robot the page degrades to the
    # brain telemetry pushed to /sim/brain (camera + costmap + pet panels), so you
    # can watch the live pet at /sim. The top-down map is the only sim-only panel.
    from .sim_view import DASHBOARD_HTML

    return HTMLResponse(DASHBOARD_HTML)


@app.get("/sim/state")
def sim_state() -> dict:
    if sim_world is None:
        # No 2D world (real robot): still hand the dashboard the pushed telemetry
        # so its camera/costmap/pet panels render; enabled=False tells the page to
        # skip the top-down map and use the costmap radar instead.
        return {"enabled": False, "brain": _sim_brain}
    return {"enabled": True, **sim_world.state(), "brain": _sim_brain}


@app.post("/sim/brain")
def sim_brain(payload: dict) -> dict:
    """The brain pushes telemetry (mood, gesture, costmap, narration, ...) here."""
    _sim_brain.clear()
    _sim_brain.update(payload or {})
    _sim_brain["_received_at"] = time.time()
    return {"ok": True}


@app.post("/sim/obstacle")
def sim_obstacle(body: ObstacleBody) -> dict:
    if sim_world is None:
        return {"ok": False, "error": "sim disabled"}
    if body.action == "remove":
        sim_world.remove_near(body.cx, body.cy)
    else:
        sim_world.add_obstacle(body.cx, body.cy, body.r, body.label)
    return {"ok": True, "obstacles": len(sim_world.obstacles)}


@app.post("/sim/control")
def sim_control(body: ControlBody) -> dict:
    if sim_world is None:
        return {"ok": False, "error": "sim disabled"}
    if body.action == "pause":
        sim_world.set_paused(True)
    elif body.action == "resume":
        sim_world.set_paused(False)
    elif body.action == "reset":
        sim_world.reset()
    elif body.action == "scenario" and body.scenario:
        from .simworld import build_scenario

        sim_world.clear_obstacles()
        for o in build_scenario(body.scenario).obstacles:
            sim_world.add_obstacle(o.cx, o.cy, o.r, o.label)
        sim_world.reset()
    return {"ok": True, "paused": sim_world.paused}


def _execute(action: Action, cmd, operation) -> CommandResponse:
    # Reject contention instead of queueing a command that can become stale.
    if not command_lock.acquire(blocking=False):
        return CommandResponse(ok=False, action=action, command_id=cmd.command_id, detail="motion busy; command not queued")
    try:
        if action == Action.RESUME:
            # Without this ordering a resume could consume its old lease, then
            # run after STOP and silently clear the newly established stop latch.
            with control_transition_lock:
                reason = leases.consume(cmd.lease_id, cmd.command_id)
                result = operation() if reason is None else None
        else:
            reason = leases.consume(cmd.lease_id, cmd.command_id)
            result = operation() if reason is None else None
        if reason:
            return CommandResponse(ok=False, action=action, command_id=cmd.command_id, detail=reason)
        motion = MotionResult.model_validate(asdict(result))
        return CommandResponse(ok=result.ok, action=action, command_id=cmd.command_id,
                               detail=result.reason, motion=motion, status=_status_with_distance())
    finally:
        command_lock.release()


@app.post(ACTION_PATHS[Action.WALK], response_model=CommandResponse)
def walk(cmd: WalkCommand) -> CommandResponse:
    return _execute(Action.WALK, cmd, lambda: engine.walk(cmd.steps, cmd.speed, min_clearance_cm=cmd.min_clearance_cm))


@app.post(ACTION_PATHS[Action.TURN], response_model=CommandResponse)
def turn(cmd: TurnCommand) -> CommandResponse:
    return _execute(Action.TURN, cmd, lambda: engine.turn(cmd.degrees, cmd.speed))


@app.post(ACTION_PATHS[Action.TURN_STEPS], response_model=CommandResponse)
def turn_steps(cmd: TurnStepsCommand) -> CommandResponse:
    return _execute(Action.TURN_STEPS, cmd, lambda: engine.turn_steps(cmd.direction, cmd.cycles, cmd.speed))


@app.post(ACTION_PATHS[Action.STAND], response_model=CommandResponse)
def stand(cmd: StandCommand) -> CommandResponse:
    return _execute(Action.STAND, cmd, engine.stand)


@app.post(ACTION_PATHS[Action.SIT], response_model=CommandResponse)
def sit(cmd: SitCommand) -> CommandResponse:
    return _execute(Action.SIT, cmd, engine.sit)


@app.post(ACTION_PATHS[Action.STOP], response_model=CommandResponse)
def stop(cmd: StopCommand) -> CommandResponse:
    with control_transition_lock:
        leases.invalidate()
        result = engine.stop()
    if speaker:
        speaker.stop()
    status = engine.get_status()
    status.observed_at = time.time()
    status.boot_id = leases.boot_id
    status.motion_state = engine.motion_state()
    return CommandResponse(ok=True, action=Action.STOP, detail="stop latched; active vendor boundary may still be finishing",
                           motion=MotionResult.model_validate(asdict(result)), status=status)


@app.post(ACTION_PATHS[Action.RESUME], response_model=CommandResponse)
def resume(cmd: ResumeCommand) -> CommandResponse:
    return _execute(Action.RESUME, cmd, engine.resume)


@app.post(ACTION_PATHS[Action.GET_STATUS], response_model=CommandResponse)
def get_status(_cmd: GetStatusCommand) -> CommandResponse:
    return CommandResponse(ok=True, action=Action.GET_STATUS, detail="status", status=_status_with_distance())


@app.post(ACTION_PATHS[Action.TEST_LEG], response_model=CommandResponse)
def test_leg(cmd: TestLegCommand) -> CommandResponse:
    # Maintenance stays Pi-local; the companion cannot calibrate or exercise legs.
    return CommandResponse(ok=False, action=Action.TEST_LEG, detail="leg diagnostics are Pi-local and require explicit hardware approval")


def main() -> None:
    import uvicorn

    logger.info("Starting PiCrawler server on %s:%d (simulate=%s)", config.HOST, config.PORT, engine.simulate)
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
