"""Bounded, latest-only dashboard publishing; never queues stale behavior state."""
from __future__ import annotations

import copy
import threading
import time
import uuid

import httpx
from shared.clock import CaptureClock
from shared.local import require_local_url


class Dashboard:
    def __init__(self, base_url: str | None, enabled: bool) -> None:
        self.url = base_url.rstrip("/") + "/sim/brain" if enabled and base_url else None
        if self.url:
            require_local_url(self.url)
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._publisher_id = uuid.uuid4().hex
        self._publisher_started_at_ns = time.time_ns()
        self._sequence = 0
        self._sent = self._dropped = 0
        self._last_latency_ms = self._last_success_at = None
        self._last_error = None
        self._thread = None
        if self.url:
            self._thread = threading.Thread(target=self._run, daemon=True, name="dashboard-publisher")
            self._thread.start()

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def push(self, state: dict) -> None:
        if not self.url:
            return
        # The full history stays in the local log. Only the visible window travels
        # to the dashboard, and nested values cannot mutate under the worker.
        visible = dict(state)
        for key, limit in (("intentions", 12), ("movements", 6)):
            if isinstance(visible.get(key), list):
                visible[key] = visible[key][-limit:]
        payload = copy.deepcopy(visible)
        with self._condition:
            if self._closed:
                return
            self._sequence += 1
            produced_mono = time.monotonic()
            payload.update(_publisher_id=self._publisher_id, _sequence=self._sequence,
                           _publisher_started_at_ns=self._publisher_started_at_ns,
                           _produced_at=time.time())
            # Earliest possible Pi time of production gives the browser an
            # upper source age. Never use a raw Jetson stamp as Pi time.
            try:
                anchor = CaptureClock.model_validate(payload.get("pi_clock"))
                payload["_produced_at_pi_lower"] = anchor.remote_bounds(payload["_produced_at"], produced_mono)[0]
            except (ValueError, TypeError):
                payload["_produced_at_pi_lower"] = None
            if self._pending is not None:
                self._dropped += 1
            self._pending = payload
            self._condition.notify()

    def health(self) -> dict:
        with self._condition:
            return {"enabled": self.enabled, "sent": self._sent, "replaced": self._dropped,
                    "pending": self._pending is not None, "last_latency_ms": self._last_latency_ms,
                    "last_success_at": self._last_success_at, "failure_reason": self._last_error}

    def _run(self) -> None:
        # A persistent connection and one in-flight request: a slow network can
        # replace the pending sample, but cannot create threads or a stale queue.
        with httpx.Client(timeout=1.0, trust_env=False, follow_redirects=False) as client:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._pending is not None)
                    if self._pending is None:
                        return
                    payload, self._pending = self._pending, None
                started = time.monotonic()
                payload["_sent_at"] = time.time()
                try:
                    response = client.post(self.url, json=payload)
                    response.raise_for_status()
                    acknowledgement = response.json()
                    if not isinstance(acknowledgement, dict):
                        raise ValueError("invalid dashboard acknowledgement")
                    if acknowledgement.get("ok") is not True:
                        raise ValueError(str(acknowledgement.get("reason") or "dashboard rejected telemetry"))
                    with self._condition:
                        self._sent += 1
                        self._last_success_at = time.time()
                        self._last_error = None
                except Exception as exc:
                    with self._condition:
                        self._last_error = str(exc)[:240]
                finally:
                    with self._condition:
                        self._last_latency_ms = round((time.monotonic() - started) * 1000, 1)

    def close(self, timeout: float = 1.2) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify()
        if self._thread:
            self._thread.join(timeout=max(0.0, timeout))
