"""Tiny telemetry publisher for the sim dashboard.

The brain loops (pet/wander) push their inner state — mood, gesture, costmap,
narration, chosen heading — to the robot server's ``POST /sim/brain`` so the
dashboard at ``/sim`` can show it alongside the physical sim. Fire-and-forget on a
daemon thread and error-swallowing, so telemetry never slows or breaks the loop;
a no-op when disabled or the endpoint is unreachable.
"""

from __future__ import annotations

import threading

import httpx


class Dashboard:
    def __init__(self, base_url: str | None, enabled: bool) -> None:
        self.url = (base_url.rstrip("/") + "/sim/brain") if (enabled and base_url) else None

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def push(self, state: dict) -> None:
        if not self.url:
            return
        threading.Thread(target=self._send, args=(dict(state),), daemon=True).start()

    def _send(self, state: dict) -> None:
        try:
            httpx.post(self.url, json=state, timeout=1.0)
        except Exception:  # noqa: BLE001 - telemetry is best-effort
            pass
