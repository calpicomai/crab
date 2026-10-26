"""Local WebSocket/polling measurements with an isolated simulated Pi server.

This development harness never reads the configured robot URL, opens hardware,
or runs an inference model. The reusable server fixture binds only loopback and
uses explicit simulated motion plus synthetic, labelled sensor samples.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import platform
import queue
import resource
import socket
import statistics
import threading
import time
import uuid
from unittest.mock import patch

import httpx
import uvicorn

from shared.health import InputHealth


class Sample:
    """Explicit synthetic sensor used only by this development harness."""

    def __init__(self, value):
        self.value = value

    def read_v(self):
        return self.value

    read_cm = read_v

    def health(self):
        return InputHealth(
            available=self.value is not None, provenance="simulated",
            captured_at=time.time() if self.value is not None else None,
            source="explicit realtime test fixture",
            failure_reason=None if self.value is not None else "fixture sensor disconnected")


class SimulatedServer:
    """Real uvicorn socket; explicit simulated devices and isolated server state.

    Instances must run sequentially because the production app owns module-level
    devices. The ExitStack restores every patched setting, including import-time
    hardware gates. Existing environments and personal data remain untouched.
    """

    def __init__(self, **hub_overrides):
        self.hub_overrides = hub_overrides

    def __enter__(self):
        from robot import config
        from robot.control import CommandLeases
        from robot.gait import GaitEngine
        self._stack = ExitStack()
        for name, value in {
            "SIMULATE": True, "ACTUATORS_ENABLED": False, "HOME_ON_START": "none",
            "CAMERA_ENABLED": False, "AUDIO_ENABLED": False, "SIM_WORLD": False,
        }.items():
            self._stack.enter_context(patch.object(config, name, value))
        from robot import server
        self.module = server
        self.engine = GaitEngine(simulate=True)
        self.battery, self.sonar = Sample(7.4), Sample(100.0)
        self.engine.battery_fn, self.engine.clearance_fn = self.battery.read_v, self.sonar.read_cm
        self.leases = CommandLeases()
        for name, value in {
            "engine": self.engine, "leases": self.leases,
            "battery_sensor": self.battery, "distance_sensor": self.sonar,
            "camera": None, "mic": None, "speaker": None, "sim_world": None,
            "command_lock": threading.Lock(), "control_transition_lock": threading.Lock(),
            "companion_events": queue.Queue(maxsize=32), "companion_urgent": None,
            "events_lock": threading.Lock(),
        }.items():
            self._stack.enter_context(patch.object(server, name, value))
        self._stack.enter_context(patch.object(
            server, "realtime_hub", server._make_realtime_hub(**self.hub_overrides)))
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}"
        self.http = httpx.Client(base_url=self.base_url, timeout=2.0, trust_env=False)
        cfg = uvicorn.Config(server.app, host="127.0.0.1", port=self.port,
                             log_level="critical", access_log=False, loop="asyncio",
                             ws_max_size=65536, ws_max_queue=4, ws_per_message_deflate=False,
                             lifespan="on", timeout_graceful_shutdown=1)
        self.uvicorn = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.uvicorn.run,
                                       kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 4
        while not self.uvicorn.started:
            if not self.thread.is_alive() or time.monotonic() >= deadline:
                self.__exit__(None, None, None)
                raise RuntimeError("isolated simulator server did not start")
            time.sleep(.005)
        return self

    def __exit__(self, *exc):
        self.uvicorn.should_exit = True
        self.http.close()
        self.thread.join(timeout=3)
        self.socket.close()
        self._stack.close()
        if self.thread.is_alive():
            raise RuntimeError("isolated simulator server did not shut down")


class RealtimePeer:
    """Typed raw peer for protocol integration tests; optional application pings."""

    def __init__(self, server, *, heartbeat=True):
        self.server, self.heartbeat = server, heartbeat
        self._closed = threading.Event()
        self._ping_thread = None

    def __enter__(self):
        from websockets.sync.client import connect
        from shared.realtime import WS_PATH
        self._connection_context = connect(self.server.ws_url + WS_PATH, proxy=None,
                                           open_timeout=2, close_timeout=.5, ping_interval=None,
                                           compression=None, max_size=65536, max_queue=4)
        self.socket = self._connection_context.__enter__()
        self.hello = self.receive()
        if self.hello.type != "hello":
            self._connection_context.__exit__(None, None, None)
            raise AssertionError(f"expected hello, got {self.hello.type}")
        if self.heartbeat:
            self._ping_thread = threading.Thread(target=self._pings, daemon=True)
            self._ping_thread.start()
        return self

    def _pings(self):
        from shared.realtime import PingRequest
        while not self._closed.wait(min(self.hello.heartbeat_s, .1)):
            try:
                self.send(PingRequest(**self.envelope()))
            except Exception:
                return

    def envelope(self, **extra):
        return {"boot_id": self.hello.boot_id, "session_id": self.hello.session_id,
                "request_id": uuid.uuid4().hex, **extra}

    def send(self, message):
        from shared.realtime import encode_message
        self.socket.send(encode_message(message))

    def receive(self, timeout=2):
        from shared.realtime import parse_server_message
        message = parse_server_message(self.socket.recv(timeout=timeout))
        if message.type in {"hello", "status"}:
            self.control_epoch = max(getattr(self, "control_epoch", 0), message.control_epoch)
        return message

    def until(self, kind, *, request_id=None, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.receive(max(.001, deadline-time.monotonic()))
            if message.type == kind and (request_id is None or message.request_id == request_id):
                return message
        raise TimeoutError(f"no {kind} message")

    def lease(self, *, control_epoch=None):
        from shared.realtime import LeaseRequest
        request = LeaseRequest(**self.envelope(), control_epoch=(
            self.control_epoch if control_epoch is None else control_epoch))
        self.send(request)
        return self.until("lease", request_id=request.request_id)

    def command(self, action, payload=None, *, lease=True, request_id=None):
        from shared.realtime import CommandRequest
        payload = dict(payload or {})
        if lease:
            grant = self.lease()
            if not grant.ok:
                raise AssertionError(f"lease rejected: {grant.reason}")
            payload.update(lease_id=grant.lease_id, command_id=payload.get("command_id", uuid.uuid4().hex))
        request = CommandRequest(**self.envelope(**({"request_id": request_id} if request_id else {})),
                                 action=action, payload=payload)
        self.send(request)
        return self.until("result", request_id=request.request_id).response

    def __exit__(self, *exc):
        self._closed.set()
        self._connection_context.__exit__(*exc)
        if self._ping_thread:
            self._ping_thread.join(timeout=.5)


def summary_ms(values):
    values = sorted(values)
    return {"samples": len(values),
            "median_ms": round(statistics.median(values), 3) if values else None,
            "p95_ms": round(values[min(len(values)-1, int(len(values)*.95))], 3) if values else None,
            "max_ms": round(max(values), 3) if values else None}


def memory_report():
    """Linux process RSS, including server/client/test interpreter, not Jetson RAM."""
    fields = {}
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, value, _unit = line.split()
                fields[key.rstrip(":") + "_mib"] = round(int(value) / 1024, 2)
    if not fields:
        fields["peak_rss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)
    fields["scope"] = "One Linux development process: uvicorn, client, fixtures; no models or hardware"
    return fields


class EventMeasurements:
    def __init__(self):
        self.started = {}
        self.delays = []
        self.delivered = []
        self.lock = threading.Lock()

    def put_nowait(self, event):
        # Matches InputWorker's queue interface, and the WS event callback.
        if hasattr(event, "model_dump"):
            event = event.model_dump(mode="json")
        text = event.get("text") if isinstance(event, dict) else None
        received = time.monotonic()
        with self.lock:
            if text in self.started:
                self.delays.append((received-self.started[text])*1000)
                self.delivered.append(text)


def measure_events(server, *, transport, count, baseline):
    from shared import COMPANION_EVENTS_PATH
    measurements = EventMeasurements()
    if transport == "polling":
        from .live_feed_benchmark import historical_module
        old = historical_module(baseline, "brain/pet/inputs.py", "brain.pet._realtime_baseline")
        worker = old.InputWorker(server.base_url, server.base_url, measurements, use_camera=False)
    else:
        from .realtime import RealtimeClient
        worker = RealtimeClient(server.base_url, on_event=measurements.put_nowait)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while True:
            health = worker.health()
            ready = (health.get("ready") if transport == "websocket" else
                     health.get("streams", {}).get("events", {}).get("available"))
            if ready:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{transport} input never became ready: {health}")
            time.sleep(.005)
        started = time.monotonic()
        # Deterministic phase sweep across the old 100 ms event poll interval.
        # Both transports receive the same sequence; the local OS adds jitter.
        for index in range(count):
            deadline = started + .023 + index * .137
            time.sleep(max(0.0, deadline-time.monotonic()))
            text = f"measurement event {index}"
            with measurements.lock:
                measurements.started[text] = time.monotonic()
            response = server.http.post(COMPANION_EVENTS_PATH, json={"text": text})
            response.raise_for_status()
            if response.json().get("ok") is not True:
                raise RuntimeError(f"event rejected: {response.text}")
        deadline = time.monotonic() + 2
        while len(measurements.delivered) < count and time.monotonic() < deadline:
            time.sleep(.005)
        if len(measurements.delivered) != count or len(set(measurements.delivered)) != count:
            raise RuntimeError(f"{transport} lost/duplicated events: {measurements.delivered}")
        return {"delivery": summary_ms(measurements.delays), "delivered": len(measurements.delivered),
                "ordered": measurements.delivered == [f"measurement event {i}" for i in range(count)],
                "host_process_threads": threading.active_count(),
                "health": worker.health()}
    finally:
        worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=24)
    parser.add_argument("--baseline", default="e5ea52c")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 8 <= args.events <= 200:
        parser.error("--events must be 8..200")
    cpu_start, wall_start = time.process_time(), time.monotonic()
    with SimulatedServer() as server:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        before = measure_events(server, transport="polling", count=args.events, baseline=args.baseline)
        after = measure_events(server, transport="websocket", count=args.events, baseline=args.baseline)
        memory = memory_report()
    elapsed = time.monotonic()-wall_start
    result = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "host": {"system": platform.system(), "machine": platform.machine(),
                 "python": platform.python_version()},
        "baseline_commit": args.baseline,
        "scope": "Real loopback HTTP/WebSocket sockets, explicit simulated Pi; no LAN, Pi/Jetson hardware or inference",
        "metric": "HTTP event submission start to companion input callback, monotonic clock on one host",
        "schedule": {"events": args.events, "first_offset_s": .023, "spacing_s": .137},
        "polling": before, "websocket": after, "memory": memory,
        "process": {"elapsed_s": round(elapsed, 3),
                    "cpu_s": round(time.process_time()-cpu_start, 3)},
        "limitations": ["Small synthetic sample; OS scheduler and interpreter warmup affect timings",
                        "No sensor exposure, camera, media, model or physical actuation latency measured",
                        "Combined host process memory is not Jetson model residency or Pi server sizing"],
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
