"""Pi-owned one-use command leases. Expiry uses only the Pi monotonic clock."""
from __future__ import annotations

import threading
import time
import uuid


class CommandLeases:
    def __init__(self, clock=time.monotonic, ttl_s: float = 2.0):
        self.boot_id = uuid.uuid4().hex
        self._clock, self.ttl_s = clock, ttl_s
        self._tokens: dict[str, float] = {}
        self._commands: set[str] = set()
        self._lock = threading.Lock()

    def issue(self) -> dict:
        with self._lock:
            now = self._clock()
            self._tokens = {k: v for k, v in self._tokens.items() if v > now}
            if len(self._tokens) >= 128:
                return {"ok": False, "reason": "too many outstanding command leases"}
            token = uuid.uuid4().hex
            self._tokens[token] = now + self.ttl_s
            return {"ok": True, "lease_id": token, "boot_id": self.boot_id, "ttl_s": self.ttl_s}

    def consume(self, lease_id: str | None, command_id: str | None) -> str | None:
        with self._lock:
            expiry = self._tokens.pop(lease_id, 0)
            if not lease_id or expiry <= self._clock():
                return "command lease missing, expired, already used or from an earlier boot"
            if not command_id or command_id in self._commands:
                return "command ID missing or already executed; never automatically retry motion"
            # Keep bounded per-boot deduplication; exhaustion requires deliberate restart.
            if len(self._commands) >= 4096:
                return "command history full; restart readiness before issuing more commands"
            self._commands.add(command_id)
            return None

    def invalidate(self) -> None:
        with self._lock:
            self._tokens.clear()
