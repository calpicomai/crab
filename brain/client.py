"""RobotClient — the Jetson's typed handle on the robot's abilities.

Mirrors the shared protocol exactly: it builds the same command models the
server parses and returns the same CommandResponse the server produces. Because
both sides import shared/protocol.py, the client and server cannot drift.

Stage 1 uses HTTP (httpx). The public method surface here is what the future
Ollama tool-calling agent will expose as tools — so a later WebSocket transport
can be swapped in behind these methods without changing callers.
"""

from __future__ import annotations

import httpx

from shared import (
    ACTION_PATHS,
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


class RobotClient:
    """Synchronous HTTP client for the robot command server."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else config.REQUEST_TIMEOUT_S
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    # ----------------------------------------------------------------- #
    # Context-manager support so callers can `with RobotClient() as r:`
    # ----------------------------------------------------------------- #
    def __enter__(self) -> "RobotClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ----------------------------------------------------------------- #
    # Internal helper
    # ----------------------------------------------------------------- #
    def _post(self, action: Action, payload: dict) -> CommandResponse:
        resp = self._client.post(ACTION_PATHS[action], json=payload)
        resp.raise_for_status()
        return CommandResponse.model_validate(resp.json())

    # ----------------------------------------------------------------- #
    # Abilities (one per shared.Action)
    # ----------------------------------------------------------------- #
    def health(self) -> dict:
        resp = self._client.get(HEALTH_PATH)
        resp.raise_for_status()
        return resp.json()

    def walk(
        self, steps: int = 1, speed: int | None = None, min_clearance_cm: float | None = None
    ) -> CommandResponse:
        # min_clearance_cm: optional per-walk reflex threshold (the Pi aborts the
        # walk early if forward clearance drops below it). None -> Pi default.
        extra: dict = {}
        if speed is not None:
            extra["speed"] = speed
        if min_clearance_cm is not None:
            extra["min_clearance_cm"] = min_clearance_cm
        cmd = WalkCommand(steps=steps, **extra)
        return self._post(Action.WALK, cmd.model_dump())

    def turn(self, degrees: float, speed: int | None = None) -> CommandResponse:
        cmd = TurnCommand(degrees=degrees, **({} if speed is None else {"speed": speed}))
        return self._post(Action.TURN, cmd.model_dump())

    def stand(self) -> CommandResponse:
        return self._post(Action.STAND, StandCommand().model_dump())

    def sit(self) -> CommandResponse:
        return self._post(Action.SIT, SitCommand().model_dump())

    def get_status(self) -> CommandResponse:
        return self._post(Action.GET_STATUS, GetStatusCommand().model_dump())

    def test_leg(self, leg: int, speed: int | None = None) -> CommandResponse:
        """Diagnostic: move one leg (0-3) to the standing pose. See robot/diagnose.py
        for the preferred Pi-local version that needs no network."""
        cmd = TestLegCommand(leg=leg, **({} if speed is None else {"speed": speed}))
        return self._post(Action.TEST_LEG, cmd.model_dump())
