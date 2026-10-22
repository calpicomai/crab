"""Independent, bounded HTTP polling for the companion's input channels.

Only a completed robot-status request publishes a behavior sample. Perception
can update its latest snapshot without making old robot status appear newly
received. Capture timestamps belong to the producers and are never rewritten.
"""
from __future__ import annotations

import math
import queue
import threading
import time

import httpx

from shared import COMPANION_EVENTS_PATH
from shared.local import require_local_url
from ..client import RobotClient


def _latest(q, item):
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        q.put_nowait(item)


class InputWorker:
    """At most three workers and one request per channel; no request backlog."""

    def __init__(self, base_url, perception_url, heard, *, use_camera=True,
                 status_interval_s=0.2, events_interval_s=0.1,
                 perception_interval_s=0.2):
        self.base_url = require_local_url(base_url)
        self.perception_url = require_local_url(perception_url)
        self.heard, self.use_camera = heard, use_camera
        self.samples = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._snapshot = None
        self._intervals = {
            "status": self._interval(status_interval_s),
            "events": self._interval(events_interval_s),
            "perception": self._interval(perception_interval_s),
        }
        self._metrics = {name: {"available": False, "pending": False, "polls": 0,
                               "received_at": None, "latency_ms": None,
                               "failure_reason": "not polled yet"}
                         for name in self._intervals}
        if not use_camera:
            self._metrics["perception"]["failure_reason"] = "camera input disabled"
        targets = [("status", self._status_loop), ("events", self._events_loop)]
        if use_camera:
            targets.append(("perception", self._perception_loop))
        self.threads = tuple(threading.Thread(target=target, daemon=True,
                                             name=f"companion-input-{name}")
                             for name, target in targets)

    @staticmethod
    def _interval(value):
        value = float(value)
        if not math.isfinite(value) or value < 0.01:
            raise ValueError("input polling interval must be finite and at least 0.01 seconds")
        return value

    def _begin(self, name):
        started = time.monotonic()
        with self._lock:
            self._metrics[name]["pending"] = True
            self._metrics[name]["polls"] += 1
        return started

    def _finish(self, name, started, reason):
        with self._lock:
            metrics = self._metrics[name]
            metrics.update(pending=False, available=reason is None,
                           latency_ms=round((time.monotonic() - started) * 1000, 2),
                           failure_reason=reason)
            if reason is None:
                metrics["received_at"] = time.time()

    def _wait(self, name, started):
        self.stop_event.wait(max(0.0, self._intervals[name] - (time.monotonic() - started)))

    def _status_loop(self):
        with RobotClient(self.base_url, timeout=0.8) as client:
            while not self.stop_event.is_set():
                started = self._begin("status")
                status, reason = None, None
                try:
                    response = client.get_status()
                    if response.ok and response.status:
                        status = response.status.model_dump(mode="json")
                    else:
                        reason = response.detail or "robot returned no status"
                except Exception as exc:
                    reason = str(exc)[:240]
                self._finish("status", started, reason)
                if self.stop_event.is_set():
                    break
                with self._lock:
                    snapshot = self._snapshot
                # This is the sole producer. A fresh detector result never
                # republishes an old status or clears a connection failure.
                _latest(self.samples, (status, snapshot, reason))
                self._wait("status", started)

    def _events_loop(self):
        with httpx.Client(timeout=0.5, trust_env=False, follow_redirects=False) as http:
            while not self.stop_event.is_set():
                started, reason = self._begin("events"), None
                try:
                    response = http.get(self.base_url.rstrip("/") + COMPANION_EVENTS_PATH)
                    response.raise_for_status()
                    events = response.json().get("events", [])
                    if not isinstance(events, list):
                        raise ValueError("invalid companion event response")
                    for event in events:
                        if self.stop_event.is_set():
                            break
                        if not isinstance(event, dict):
                            continue
                        stamp = event.get("timestamp")
                        if (isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                                and math.isfinite(stamp) and 0 <= time.time() - stamp <= 5
                                and isinstance(event.get("text"), str)):
                            _latest(self.heard, event)
                except Exception as exc:
                    reason = str(exc)[:240]
                self._finish("events", started, reason)
                self._wait("events", started)

    def _perception_loop(self):
        with httpx.Client(timeout=0.5, trust_env=False, follow_redirects=False) as http:
            while not self.stop_event.is_set():
                started, reason, snapshot = self._begin("perception"), None, None
                try:
                    response = http.get(self.perception_url.rstrip("/") + "/snapshot")
                    response.raise_for_status()
                    snapshot = response.json()
                    if not isinstance(snapshot, dict):
                        raise ValueError("invalid perception snapshot")
                except Exception as exc:
                    reason = str(exc)[:240]
                    snapshot = None
                self._finish("perception", started, reason)
                if self.stop_event.is_set():
                    break
                with self._lock:
                    # Store the payload unchanged, including old capture times.
                    # Coordinator's InputHealth rejects stale or unsupported data.
                    self._snapshot = snapshot
                self._wait("perception", started)

    def health(self):
        with self._lock:
            streams = {name: dict(metrics) for name, metrics in self._metrics.items()}
        return {"streams": streams, "workers": len(self.threads),
                "running_workers": sum(thread.is_alive() for thread in self.threads),
                "closed": self.stop_event.is_set(),
                "freshness": "HTTP receipt metadata; capture freshness remains in status/snapshot inputs"}

    def start(self):
        with self._lifecycle_lock:
            if self._started or self.stop_event.is_set():
                return
            self._started = True
            for thread in self.threads:
                thread.start()

    def close(self):
        with self._lifecycle_lock:
            self.stop_event.set()
            started = self._started
        if not started:
            return
        # Each worker owns/closes its persistent client in its context manager.
        # Request timeouts bound normal shutdown; never join a network call forever.
        deadline = time.monotonic() + 2.0
        for thread in self.threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
