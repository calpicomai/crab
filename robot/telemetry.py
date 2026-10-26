"""Atomic dashboard snapshots. This is observability data, never motion authority."""
from __future__ import annotations

import copy
import json
import threading
import time
from collections import deque


class TelemetryStore:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._state = {}
        self._revision = 0
        self._publisher = None
        self._publisher_started_at_ns = 0
        self._sequence = -1
        self._retired = deque(maxlen=32)

    @property
    def revision(self):
        with self._lock:
            return self._revision

    def publish(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"ok": False, "reason": "telemetry object required"}
        publisher = payload.get("_publisher_id")
        epoch = payload.get("_publisher_started_at_ns")
        sequence = payload.get("_sequence")
        if not isinstance(publisher, str) or not 1 <= len(publisher) <= 64:
            return {"ok": False, "reason": "publisher ID required"}
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            return {"ok": False, "reason": "positive sequence required"}
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            return {"ok": False, "reason": "positive publisher start epoch required"}
        try:
            json.dumps(payload, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            return {"ok": False, "reason": "telemetry must be finite JSON"}
        with self._lock:
            if publisher in self._retired or (publisher == self._publisher and sequence <= self._sequence):
                return {"ok": False, "reason": "outdated telemetry ignored"}
            if publisher != self._publisher:
                # An old process's first request may arrive after its replacement
                # even if we never saw its publisher ID. Compare Jetson start
                # epochs, not Pi receive time; a delayed request cannot retire a
                # newer process. A backwards Jetson wall-clock jump needs an
                # explicit telemetry reset/server restart.
                if epoch <= self._publisher_started_at_ns:
                    return {"ok": False, "reason": "older or equal publisher epoch ignored"}
                if self._publisher is not None:
                    self._retired.append(self._publisher)
                self._publisher = publisher
                self._publisher_started_at_ns = epoch
            elif epoch != self._publisher_started_at_ns:
                return {"ok": False, "reason": "publisher start epoch must remain constant"}
            self._sequence = sequence
            self._state = copy.deepcopy(payload)
            self._state["_received_at"] = self._clock()
            self._revision += 1
            return {"ok": True, "revision": self._revision}

    def snapshot(self):
        with self._lock:
            # Nested mutation must not change retained state without a revision.
            return copy.deepcopy(self._state), self._revision
