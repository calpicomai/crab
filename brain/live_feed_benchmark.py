"""Reproduce local live-feed delay/backpressure comparisons without hardware.

The baseline comes from the named local git commit. HTTP clients are replaced by
bounded delay fixtures; no requests, camera capture, inference or actuators run.
"""
from __future__ import annotations

import argparse
import importlib
import json
import queue
import statistics
import subprocess
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BASELINE = "5053600"


def historical_module(commit, path, name):
    source = subprocess.run(["git", "show", f"{commit}:{path}"], check=True,
                            capture_output=True, text=True).stdout
    module = types.ModuleType(name)
    module.__package__ = name.rpartition(".")[0]
    exec(compile(source, f"{commit}:{path}", "exec"), module.__dict__)
    return module


class Response:
    def __init__(self, data):
        self.data, self.status_code = data, 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class PollFixture:
    def __init__(self, detector_delay):
        self.detector_delay = detector_delay
        self.started = time.monotonic()
        self.event_delivered = False
        self.lock = threading.Lock()
        self.counts = {"status": 0, "events": 0, "perception": 0}

    def robot(self, *args, **kwargs):
        fixture = self
        class Client:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def close(self): pass
            def get_status(self):
                time.sleep(.005)
                with fixture.lock:
                    fixture.counts["status"] += 1
                data = {"fixture_captured_mono": time.monotonic(), "inputs": {}, "simulate": True}
                return SimpleNamespace(ok=True, detail="", status=SimpleNamespace(model_dump=lambda **kw: data))
        return Client()

    def http(self, *args, **kwargs):
        fixture = self
        class Client:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def close(self): pass
            def get(self, url, **kwargs):
                if url.endswith('/snapshot'):
                    time.sleep(fixture.detector_delay)
                    with fixture.lock:
                        fixture.counts["perception"] += 1
                    return Response({"inputs": {}, "detections": [], "fixture": "500ms detector"})
                time.sleep(.005)
                with fixture.lock:
                    fixture.counts["events"] += 1
                    events = [] if fixture.event_delivered else [{"text": "hello Crab", "timestamp": time.time(),
                                                                  "fixture_created_mono": fixture.started}]
                    fixture.event_delivered = True
                return Response({"events": events})
        return Client()


def summarize_ms(values):
    if not values:
        return {"samples": 0, "median_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": round(statistics.median(values), 2),
            "p95_ms": round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 2)}


def measure_polls(module, duration, detector_delay):
    fixture, heard = PollFixture(detector_delay), queue.Queue()
    ages, first_status, first_event = [], None, None
    with patch.object(module, "RobotClient", fixture.robot), patch.object(module.httpx, "Client", fixture.http):
        worker = module.InputWorker("http://127.0.0.1:1", "http://127.0.0.1:2", heard)
        start = time.monotonic()
        fixture.started = start
        worker.start()
        try:
            while time.monotonic()-start < duration:
                try:
                    status, snapshot, reason = worker.samples.get(timeout=.005)
                    now = time.monotonic()
                    if status:
                        first_status = first_status if first_status is not None else (now-start)*1000
                        ages.append((now-status["fixture_captured_mono"])*1000)
                except queue.Empty:
                    pass
                try:
                    event = heard.get_nowait()
                    first_event = first_event if first_event is not None else (time.monotonic()-event["fixture_created_mono"])*1000
                except queue.Empty:
                    pass
        finally:
            worker.close()
    return {"status_age_at_consumer": summarize_ms(ages), "first_status_ms": round(first_status, 2) if first_status else None,
            "first_event_ms": round(first_event, 2) if first_event else None, "completed_polls": fixture.counts,
            "measurement_window_s": duration}


class PublishFixture:
    def __init__(self):
        self.active = self.peak = self.calls = 0
        self.deliveries = []
        self.lock = threading.Lock()

    def post(self, url, *, json, **kwargs):
        tick = json["fixture_tick"]
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        # A delayed first request exposes rollback in independently sent posts.
        time.sleep(.6 if tick == 0 else .05)
        with self.lock:
            self.deliveries.append(tick)
            self.active -= 1
        return Response({"ok": True})

    def client(self, *args, **kwargs):
        fixture = self
        class Client:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def close(self): pass
            def post(self, *args, **kwargs): return fixture.post(*args, **kwargs)
        return Client()


def measure_publisher(module, count=30):
    fixture = PublishFixture()
    with patch.object(module.httpx, "post", fixture.post), patch.object(module.httpx, "Client", fixture.client):
        publisher = module.Dashboard("http://127.0.0.1:1", True)
        start = time.monotonic()
        for i in range(count):
            publisher.push({"fixture_tick": i})
            time.sleep(.005)
        # Complete the bounded first and most-recent posts (no server/network).
        deadline = start + 2
        while time.monotonic() < deadline:
            with fixture.lock:
                idle = fixture.active == 0
                delivered = bool(fixture.deliveries)
            health = publisher.health() if hasattr(publisher, "health") else {}
            if idle and delivered and not health.get("pending", False):
                break
            time.sleep(.01)
        metrics = publisher.health() if hasattr(publisher, "health") else None
        if hasattr(publisher, "close"):
            publisher.close()
    regressions = sum(b < a for a, b in zip(fixture.deliveries, fixture.deliveries[1:]))
    return {"submitted": count, "http_requests": fixture.calls, "peak_concurrent_http": fixture.peak,
            "delivered_ticks": fixture.deliveries, "final_tick": fixture.deliveries[-1] if fixture.deliveries else None,
            "out_of_order_regressions": regressions, "publisher_health": metrics}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--duration", type=float, default=2.2)
    parser.add_argument("--detector-delay", type=float, default=.5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not .8 <= args.duration <= 10 or not 0 <= args.detector_delay <= 1:
        parser.error("duration must be .8..10s; detector delay 0..1s")
    baseline_inputs = historical_module(args.baseline, "brain/pet/loop.py", "brain.pet._benchmark_baseline")
    baseline_dashboard = historical_module(args.baseline, "brain/dashboard.py", "brain._benchmark_baseline_dashboard")
    current_inputs = importlib.import_module("brain.pet.inputs")
    current_dashboard = importlib.import_module("brain.dashboard")
    result = {
        "scope": "Development-host delay fixtures; no real network, camera, hardware or inference",
        "baseline_commit": args.baseline,
        "fixture": {"status_transport_ms": 5, "event_transport_ms": 5,
                    "detector_ms": args.detector_delay*1000, "publisher_first_post_ms": 600,
                    "publisher_other_posts_ms": 50, "publisher_burst_spacing_ms": 5},
        "polling": {"before": measure_polls(baseline_inputs, args.duration, args.detector_delay),
                    "after": measure_polls(current_inputs, args.duration, args.detector_delay)},
        "publishing": {"before": measure_publisher(baseline_dashboard),
                       "after": measure_publisher(current_dashboard)},
    }
    data = json.dumps(result, indent=2, sort_keys=True)+"\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data)
    print(data, end="")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
