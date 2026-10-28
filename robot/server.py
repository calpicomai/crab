"""FastAPI command server — runs on the Raspberry Pi.

Each endpoint parses a command model from shared/protocol.py, invokes the
matching GaitEngine ability, and returns a uniform CommandResponse. Paths come
from shared.ACTION_PATHS so the client and server can never disagree.

Run directly:
    PICRAWLER_SIMULATE=1 python -m robot.server        # dev / off-hardware
    python -m robot.server                             # real inputs; actuators disabled by default
or via the systemd unit (robot/systemd/picrawler-server.service).
"""

from __future__ import annotations

import json
import logging
import asyncio
import threading
import queue
import time
from uuid import uuid4
from dataclasses import asdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket
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
from shared.realtime import WS_PATH, MAX_MESSAGE_BYTES
from .control import CommandLeases
from .realtime import RealtimeHub
from .telemetry import TelemetryStore
from .lifecycle import (Shutdown, OwnedRoute, OwnedServer, cleanup_owners,
                        request_workers, stop_workers, WorkerCapacityError, SHUTDOWN_TIMEOUT_S)
from shared.vitals import VitalsSampler
from . import config
from .audio import PiMic, PiSpeaker
from .camera import PiCamera
from .gait import GaitEngine
from .sensors import BatterySensor, DistanceSensor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("picrawler.server")
_host_vitals = VitalsSampler("pi")
leases = CommandLeases()
command_lock = threading.Lock()
# This short lock orders STOP against lease consumption + latch clearing for
# RESUME. Never hold it while moving, reading sensors or stopping audio.
control_transition_lock = threading.Lock()
companion_events: queue.Queue = queue.Queue(maxsize=32)
events_lock = threading.Lock()
companion_urgent = None
companion_event_generation = 0
_shutdown: Shutdown | None = None

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
        encoder=config.CAMERA_ENCODER,
        mjpeg_bitrate=config.CAMERA_MJPEG_BITRATE,
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
# clearance between gait cycles and aborts early if something's too close.
# Missing required clearance inhibits motion; only explicit simulation is synthetic.
if distance_sensor is not None:
    engine.clearance_fn = distance_sensor.read_cm

# Battery voltage monitor (optional). Pi motion guards enforce power caps;
# missing robot_hat or a failed read reports unknown and inhibits motion.
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
# speaker plays local WAVs. Missing ALSA/device remains explicitly unavailable.
mic = PiMic(rate=config.MIC_RATE, device=config.MIC_DEVICE, simulate=config.SIMULATE) if config.AUDIO_ENABLED else None
speaker = PiSpeaker(device=config.SPEAKER_DEVICE, simulate=config.SIMULATE,
                    backend=config.SPEAKER_BACKEND, verified_board=config.SPEAKER_VERIFIED_BOARD,
                    expected_pin=config.SPEAKER_EXPECTED_PIN, gain=config.SPEAKER_GAIN) if config.AUDIO_ENABLED else None

if mic is not None and speaker is not None:
    mic.suppress = speaker.suppressed

# --- Simulator world (dev / off-hardware) -------------------------------------
# When simulating and PICRAWLER_SIM_WORLD=1, back the gait/sonar/camera with a 2D
# world so the robot actually moves in a space and the sonar sees real obstacles.
# A live dashboard is served at /sim. Not part of the robot<->brain protocol.
sim_world = None
_telemetry = TelemetryStore()
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
    """Optional staged pose, off by default and subject to every Pi motion guard.

    Physical startup motion requires explicit actuator/homing configuration at
    the hardware acceptance gate. No startup vendor construction is automatic.
    """
    if _closing():
        return
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


def _closing():
    # A shutdown fence belongs to exactly one boot's authority.
    return (_shutdown is not None and _shutdown.boot_id == leases.boot_id
            and _shutdown.closing.is_set())


def _latch_shutdown():
    # STOP itself never waits for motion, sensors, audio or metrics. Repeat it
    # under the short admission transaction so an older RESUME cannot win.
    engine.stop()
    with control_transition_lock:
        leases.invalidate(advance_control_epoch=True)
        realtime_hub.invalidate_commands()
        engine.stop()


async def _cleanup_shutdown():
    operations = [("realtime", realtime_hub.close, True),
                  ("companion events", _clear_companion_events, False),
                  ("host vitals", _host_vitals.close, False)]
    if speaker is not None:
        operations.append(("speaker/amplifier", speaker.close, False))
    if camera is not None:
        operations.append(("camera", camera.stop, False))
    await cleanup_owners(operations)
    logger.info("Pi shutdown wait ended; cleanup and in-flight native work may remain incomplete")


def _start_owner(owner, start, close):
    if owner.closing.is_set():
        return
    try:
        start()
    finally:
        # A native startup may return after the bounded shutdown wait. It must
        # release its own late-acquired resources rather than resurrect capture.
        if owner.closing.is_set():
            close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _shutdown
    owner = _shutdown = Shutdown(_latch_shutdown, _cleanup_shutdown, leases.boot_id)
    try:
        await _observation_result(request_workers.submit(_start_owner, owner, _host_vitals.start, _host_vitals.close))
        if _closing():
            raise RuntimeError("server shutdown requested during startup")
        if camera is not None:
            await _observation_result(request_workers.submit(_start_owner, owner, camera.start, camera.stop))
        if _closing():
            raise RuntimeError("server shutdown requested during startup")
        await _observation_result(request_workers.submit(_home_on_start))
        if _closing():
            raise RuntimeError("server shutdown requested during startup")
        yield
    finally:
        await owner.close()


app = FastAPI(title="PiCrawler Robot Server", version="0.1.0", lifespan=lifespan)
app.router.route_class = OwnedRoute


async def _observation_result(future):
    """An observation cannot retain a connection while its native read stalls."""
    owner = _shutdown
    if owner is None or owner.boot_id != leases.boot_id:
        return await future
    stopping = asyncio.create_task(owner.ready.wait())
    try:
        done, _ = await asyncio.wait((future, stopping), return_when=asyncio.FIRST_COMPLETED)
        if stopping in done:
            future.cancel()
            return None
        return future.result()
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)


async def _observation_stream(iterator):
    sentinel = object()
    try:
        while not _closing():
            item = await _observation_result(request_workers.submit(next, iterator, sentinel))
            if item is sentinel or item is None:
                return
            yield item
    finally:
        # close() may itself wait on a driver. It shares bounded cleanup ownership.
        try:
            await asyncio.wait_for(request_workers.submit(iterator.close), .1)
        except Exception:
            logger.warning("observation iterator cleanup pending or failed", exc_info=True)


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
        "camera_capture": camera.stats() if camera is not None else {"available": False},
        "audio": audio_health(),
        "realtime": realtime_hub.health(),
        "limitations": ["no measured odometry or joint feedback", "no cliff guarantee",
                        "stop takes effect between vendor motion boundaries", "physical power cutoff required"],
    }


@app.get(CAMERA_STREAM_PATH)
def camera_stream() -> StreamingResponse:
    """MJPEG stream (multipart/x-mixed-replace) the Jetson perception pulls from."""
    if camera is None:
        return Response(status_code=503, content="camera disabled")
    return StreamingResponse(
        _observation_stream(camera.mjpeg_frames()),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"X-Crab-Provenance": "simulated" if camera.simulate else "real",
                 "X-Crab-Boot-Id": leases.boot_id,
                 "Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get(CAMERA_FRAME_PATH)
def camera_frame() -> Response:
    """One atomic newest JPEG/metadata sample; no stale server-side frame queue."""
    if camera is None:
        return Response(status_code=503, content="camera disabled", headers={"Cache-Control": "no-store"})
    frame = camera.get_snapshot(timeout=0)
    if frame is None:
        return Response(status_code=503, content="no fresh frame", headers={"Cache-Control": "no-store"})
    return Response(content=frame.data, media_type="image/jpeg", headers={
        "X-Crab-Provenance": frame.provenance,
        "X-Crab-Boot-Id": leases.boot_id,
        "X-Crab-Captured-At": str(frame.captured_at),
        "X-Crab-Sequence": str(frame.sequence),
        "X-Crab-Frame-Age-Ms": str(round(max(0, time.time()-frame.captured_at)*1000, 1)),
        "Cache-Control": "no-store, max-age=0", "X-Accel-Buffering": "no",
    })


@app.get(AUDIO_STREAM_PATH)
def audio_stream() -> Response:
    """Raw mic PCM (S16_LE mono @ MIC_RATE) the Jetson pulls for Whisper STT."""
    if mic is None:
        return Response(status_code=503, content="audio disabled")
    return StreamingResponse(_observation_stream(mic.stream()), media_type="audio/L16",
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
    owner = _shutdown
    if _closing():
        return {"ok": False, "played": False, "reason": "server shutting down"}
    if speaker is None:
        return {"ok": False, "played": False, "reason": "audio disabled"}
    def authorize():
        with control_transition_lock:
            return "server shutting down" if owner is not _shutdown or _closing() else None
    return speaker.self_test(play=cmd.play, authorize=authorize)


@app.post(AUDIO_STOP_PATH)
def audio_stop(request: Request) -> Response:
    try:
        envelope = AudioEnvelope(session_id=request.headers.get(AUDIO_SESSION_HEADER),
                                 sequence=request.headers.get(AUDIO_SEQUENCE_HEADER))
    except ValidationError:
        return JSONResponse(status_code=422, content={"ok": False, "reason": "audio session and sequence headers required"})
    with control_transition_lock:
        leases.invalidate()
        realtime_hub.invalidate_commands()
    result = speaker.stop(**envelope.model_dump()) if speaker else {"ok": True, "played": False, "reason": "audio disabled"}
    return JSONResponse(result)


@app.post(AUDIO_PLAY_PATH)
async def audio_play(request: Request) -> Response:
    if _closing():
        return JSONResponse(status_code=503, content={"ok": False, "played": False, "reason": "server shutting down"})
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
    def authorize():
        with control_transition_lock:
            return ("server shutting down" if _closing() else
                    leases.consume(request.headers.get(AUDIO_LEASE_HEADER),
                                   f"audio:{envelope.session_id}:{envelope.sequence}"))
    try:
        result = await request_workers.submit(speaker.play_result, bytes(data), **envelope.model_dump(),
                                              authorize=authorize)
    except WorkerCapacityError as exc:
        return JSONResponse(status_code=503, content={"ok": False, "played": False, "reason": str(exc)})
    return JSONResponse(status_code=200 if result.get("ok") or result.get("simulate") else 503, content=result)


class CompanionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)


@app.post(COMPANION_EVENTS_PATH)
def companion_event(event: CompanionEvent) -> dict:
    global companion_urgent
    if _closing():
        return {"ok": False, "reason": "server shutting down"}
    control_epoch = leases.control_epoch
    generation = companion_event_generation
    item = {"text": event.text, "timestamp": time.time(), "source": "dashboard",
            "event_id": uuid4().hex}
    receipt = {"event_id": item["event_id"], "boot_id": leases.boot_id, "session_id": None}
    command = interpret(event.text)
    urgent = command and command.name in {"stop", "stay", "scold"}
    # STOP remains effective even if a slow companion cannot receive its text.
    if urgent:
        stop(StopCommand())
        control_epoch = leases.control_epoch
    receipt["control_epoch"] = control_epoch
    with events_lock:
        if _closing():
            return {"ok": False, "reason": "server shutting down"}
        if not urgent and generation != companion_event_generation:
            return {"ok": False, "reason": "input preceded stop or session change"}
        delivered = realtime_hub.publish_event(item, control_epoch=control_epoch, urgent=bool(urgent),
                                                receipt=receipt)
        if delivered is not None:
            return {"ok": delivered, **receipt,
                    **({} if delivered else {"reason": "companion event queue unavailable or full"})}
        # Legacy HTTP consumption has no WS session. These fields preserve its
        # origin for observation without granting it any new control authority.
        item.update(boot_id=receipt["boot_id"], session_id=None)
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
    return {"ok": True, **receipt}


@app.get(COMPANION_EVENTS_PATH)
def read_companion_events() -> dict:
    global companion_urgent
    events = []
    with events_lock:
        if realtime_hub.has_owner:
            return {"events": [], "owner": "websocket"}
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
    with control_transition_lock:
        return {"ok": False, "reason": "server shutting down"} if _closing() else leases.issue()


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
    # brain telemetry pushed to /sim/brain (camera, vitals and companion panels), so you
    # can watch the live pet at /sim. The top-down map is the only sim-only panel.
    from .sim_view import DASHBOARD_HTML

    return HTMLResponse(DASHBOARD_HTML)


def _dashboard_state() -> dict:
    brain, revision = _telemetry.snapshot()
    now = time.time()
    state = {"enabled": sim_world is not None,
             **(sim_world.state() if sim_world is not None else {}),
             "brain": brain, "revision": revision, "server_at": now, "server_boot_id": leases.boot_id,
             "telemetry_age_ms": None if not brain else round(max(0, now-brain["_received_at"])*1000, 1)}
    state.update(host_vitals=_host_vitals.snapshot(),
                 robot_runtime={"simulate": engine.simulate, "motion": engine.motion_state()},
                 camera_capture=camera.stats() if camera is not None else {"available": False, "reason": "camera disabled"})
    return state


@app.get("/sim/state")
def sim_state() -> Response:
    return JSONResponse(_dashboard_state(), headers={"Cache-Control": "no-store"})


@app.get("/sim/clock")
def sim_clock() -> Response:
    # Calibration must not wait for host metrics, camera statistics or telemetry
    # copying/serialization. This route grants no control session or authority.
    return JSONResponse({"server_at": time.time(), "server_boot_id": leases.boot_id},
                        headers={"Cache-Control": "no-store"})


async def _dashboard_events(request: Request):
    # No per-viewer event queue or historical replay. Slow readers resume with
    # the newest snapshot after backpressure clears. Browser rendering is bounded too.
    while not _closing() and not await request.is_disconnected():
        state = await _observation_result(request_workers.submit(_dashboard_state))
        if state is None or _closing():
            return
        yield "event: state\ndata: " + json.dumps(state, separators=(",", ":"), allow_nan=False) + "\n\n"
        await asyncio.sleep(0.1)


@app.get("/sim/events")
async def sim_events(request: Request) -> StreamingResponse:
    return StreamingResponse(_dashboard_events(request), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })


@app.post("/sim/brain")
def sim_brain(payload: dict) -> dict:
    return _telemetry.publish(payload)


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


def _command_status(status_provider=None):
    if status_provider is None:
        return _status_with_distance()
    # Results must describe the completed control transition, even if a sensor
    # sample is slow. Preserve sensor capture times; refresh only Pi motion state.
    status = engine.get_status()
    cached = status_provider()
    if cached is not None:
        status.distance_cm = cached.distance_cm
        status.battery_v = cached.battery_v
        status.inputs = cached.inputs
    status.observed_at = time.time()
    status.boot_id = leases.boot_id
    status.motion_state = engine.motion_state()
    return status


def _execute(action: Action, cmd, operation, *, authorize=None, status_provider=None) -> CommandResponse:
    # Reject contention instead of queueing a command that can become stale.
    if not command_lock.acquire(blocking=False):
        return CommandResponse(ok=False, action=action, command_id=cmd.command_id, detail="motion busy; command not queued")
    try:
        # Admission and RESUME share STOP's short transaction. Movements and
        # sensor reads remain outside it so STOP never waits for a vendor gait.
        with control_transition_lock:
            reason = "server shutting down" if _closing() else (authorize() if authorize is not None else None)
            if reason is None:
                reason = leases.consume(cmd.lease_id, cmd.command_id)
            if reason is None and _closing():
                reason = "server shutting down"
            result = operation() if reason is None and action == Action.RESUME else None
        if reason:
            return CommandResponse(ok=False, action=action, command_id=cmd.command_id, detail=reason)
        if action != Action.RESUME:
            result = operation()
        motion = MotionResult.model_validate(asdict(result))
        return CommandResponse(ok=result.ok, action=action, command_id=cmd.command_id,
                               detail=result.reason, motion=motion,
                               status=_command_status(status_provider))
    finally:
        command_lock.release()


def _dispatch(action: Action, cmd, authorize=None, status_provider=None) -> CommandResponse:
    """One canonical operation mapping shared by HTTP and WebSocket dispatch."""
    operations = {
        Action.WALK: lambda: engine.walk(cmd.steps, cmd.speed, min_clearance_cm=cmd.min_clearance_cm),
        Action.TURN: lambda: engine.turn(cmd.degrees, cmd.speed),
        Action.TURN_STEPS: lambda: engine.turn_steps(cmd.direction, cmd.cycles, cmd.speed),
        Action.STAND: engine.stand,
        Action.SIT: engine.sit,
        Action.RESUME: engine.resume,
    }
    if action in operations:
        return _execute(action, cmd, operations[action], authorize=authorize, status_provider=status_provider)
    if action == Action.STOP:
        return stop(cmd)
    if action == Action.GET_STATUS:
        return CommandResponse(ok=True, action=action, command_id=cmd.command_id, detail="status",
                               status=(status_provider or _status_with_distance)())
    return CommandResponse(ok=False, action=action, command_id=cmd.command_id,
                           detail="leg diagnostics are Pi-local and require explicit hardware approval")


@app.post(ACTION_PATHS[Action.WALK], response_model=CommandResponse)
def walk(cmd: WalkCommand) -> CommandResponse:
    return _dispatch(Action.WALK, cmd)


@app.post(ACTION_PATHS[Action.TURN], response_model=CommandResponse)
def turn(cmd: TurnCommand) -> CommandResponse:
    return _dispatch(Action.TURN, cmd)


@app.post(ACTION_PATHS[Action.TURN_STEPS], response_model=CommandResponse)
def turn_steps(cmd: TurnStepsCommand) -> CommandResponse:
    return _dispatch(Action.TURN_STEPS, cmd)


@app.post(ACTION_PATHS[Action.STAND], response_model=CommandResponse)
def stand(cmd: StandCommand) -> CommandResponse:
    return _dispatch(Action.STAND, cmd)


@app.post(ACTION_PATHS[Action.SIT], response_model=CommandResponse)
def sit(cmd: SitCommand) -> CommandResponse:
    return _dispatch(Action.SIT, cmd)


def _latch_stop(cmd: StopCommand) -> CommandResponse:
    """Only the short authority transaction/event fence; no device/status work."""
    with control_transition_lock:
        leases.invalidate(advance_control_epoch=True)
        realtime_hub.invalidate_commands()
        result = engine.stop()
    _clear_companion_events()
    return CommandResponse(ok=True, action=Action.STOP, command_id=cmd.command_id,
                           detail="stop latched; active vendor boundary may still be finishing; audio cancellation and status pending",
                           motion=MotionResult.model_validate(asdict(result)))


def _finish_stop(response: CommandResponse) -> CommandResponse:
    if speaker and not _closing():
        speaker.stop()
    status = engine.get_status()
    status.observed_at = time.time()
    status.boot_id = leases.boot_id
    status.motion_state = engine.motion_state()
    return response.model_copy(update={"status": status,
        "detail": "stop latched; active vendor boundary may still be finishing"})


def stop(cmd: StopCommand) -> CommandResponse:
    return _finish_stop(_latch_stop(cmd))


@app.post(ACTION_PATHS[Action.STOP], response_model=CommandResponse)
async def stop_http(cmd: StopCommand) -> CommandResponse:
    # Optional cleanup/result capacity can reject work; it cannot reject STOP.
    response = _latch_stop(cmd)
    try:
        return await stop_workers.submit(_finish_stop, response)
    except Exception:
        logger.warning("STOP latched; optional cleanup/status completion unavailable", exc_info=True)
        return response


@app.post(ACTION_PATHS[Action.RESUME], response_model=CommandResponse)
def resume(cmd: ResumeCommand) -> CommandResponse:
    return _dispatch(Action.RESUME, cmd)


@app.post(ACTION_PATHS[Action.GET_STATUS], response_model=CommandResponse)
def get_status(_cmd: GetStatusCommand) -> CommandResponse:
    return _dispatch(Action.GET_STATUS, _cmd)


@app.post(ACTION_PATHS[Action.TEST_LEG], response_model=CommandResponse)
def test_leg(cmd: TestLegCommand) -> CommandResponse:
    # Maintenance stays Pi-local; the companion cannot calibrate or exercise legs.
    return _dispatch(Action.TEST_LEG, cmd)


def _clear_companion_events():
    global companion_urgent, companion_event_generation
    with events_lock:
        companion_event_generation += 1
        companion_urgent = None
        while not companion_events.empty():
            companion_events.get_nowait()


def _session_lease(expected_epoch, register):
    with control_transition_lock:
        if _closing():
            return {"ok": False, "reason": "server shutting down", "control_epoch": leases.control_epoch}
        if expected_epoch != leases.control_epoch:
            return {"ok": False, "reason": "control epoch changed; input preceded stop or cancellation",
                    "control_epoch": leases.control_epoch}
        lease = leases.issue()
        lease["control_epoch"] = leases.control_epoch
        if not register(lease):
            return {"ok": False, "reason": "companion session cancelled or stopped",
                    "control_epoch": leases.control_epoch}
        return lease


def _make_realtime_hub(**overrides):
    # Resolve global devices/leases dynamically, including explicit test fakes.
    return RealtimeHub(boot_id_fn=lambda: leases.boot_id,
        epoch_fn=lambda: leases.control_epoch,
        status_fn=lambda: _status_with_distance(),
        dispatch_fn=lambda action, cmd, guard, status: _dispatch(action, cmd, guard, status),
        lease_fn=_session_lease,
        stop_fn=lambda response=None: _finish_stop(response) if response is not None else stop(StopCommand()),
        stop_latch_fn=lambda: _latch_stop(StopCommand()),
        clear_events_fn=_clear_companion_events, worker_fn=request_workers.submit,
        stop_worker_fn=stop_workers.submit, **overrides)


realtime_hub = _make_realtime_hub()


@app.websocket(WS_PATH)
async def companion_socket(websocket: WebSocket):
    if _closing():
        await websocket.close(code=1001)
        return
    # This machine-to-machine endpoint has no browser consumer. Browsers send
    # Origin; reject it before claiming control (closing an owner latches STOP).
    if websocket.headers.get("origin") is not None:
        await websocket.close(code=1008)
        return
    await realtime_hub.handle(websocket)


def main() -> None:
    import uvicorn

    logger.info("Starting PiCrawler server on %s:%d (simulate=%s)", config.HOST, config.PORT, engine.simulate)
    owned = OwnedServer(uvicorn.Config(app, host=config.HOST, port=config.PORT,
        ws_max_size=MAX_MESSAGE_BYTES, ws_max_queue=4, ws_per_message_deflate=False,
        timeout_graceful_shutdown=SHUTDOWN_TIMEOUT_S), lambda: _shutdown)
    owned.run()


if __name__ == "__main__":
    main()
