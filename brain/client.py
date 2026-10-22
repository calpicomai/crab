"""RobotClient — the Jetson's typed handle on the robot's abilities.

Mirrors the shared protocol exactly: it builds the same command models the
server parses and returns the same CommandResponse the server produces. Because
both sides import shared/protocol.py, the client and server cannot drift.

Commands can share the local realtime session. HTTP remains available for
diagnostics and independent STOP; an uncertain command never changes transport.
"""

from __future__ import annotations

import httpx
import uuid
from shared.local import require_local_url

from shared import (
    ACTION_PATHS,
    CONTROL_LEASE_PATH, StopCommand, ResumeCommand, TurnStepsCommand,
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
    """Typed command client with an optional shared WebSocket transport."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None, *, realtime=None) -> None:
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        require_local_url(self.base_url)
        self.timeout = timeout if timeout is not None else config.REQUEST_TIMEOUT_S
        if realtime is not None and getattr(realtime, "base_url", self.base_url).rstrip("/") != self.base_url:
            raise ValueError("HTTP stop and WebSocket commands must address the same robot base URL")
        self.realtime = realtime  # shared lifetime belongs to the companion input service
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout,
                                    trust_env=False, follow_redirects=False)

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
    def _post(self, action: Action, payload: dict, *, expected_control_epoch=None) -> CommandResponse:
        if self.realtime is not None and action not in (Action.GET_STATUS, Action.STOP):
            # Never fall back to HTTP after an uncertain WebSocket dispatch.
            metadata = {} if expected_control_epoch is None else {"expected_control_epoch": expected_control_epoch}
            return self.realtime.command(action, payload, timeout=self.timeout, **metadata)
        if expected_control_epoch is not None:
            raise ValueError("control epoch provenance requires the realtime transport")
        if action not in (Action.GET_STATUS, Action.STOP):
            lease = self._client.post(CONTROL_LEASE_PATH)
            lease.raise_for_status()
            lease_data = lease.json()
            if not lease_data.get("ok"):
                raise RuntimeError(lease_data.get("reason", "command lease unavailable"))
            payload = {**payload, "lease_id": lease_data["lease_id"], "command_id": uuid.uuid4().hex}
        resp = self._client.post(ACTION_PATHS[action], json=payload)
        resp.raise_for_status()
        result = CommandResponse.model_validate(resp.json())
        if result.action != action:
            raise ValueError("robot response action does not match command; outcome unknown")
        if payload.get("command_id") and result.command_id != payload["command_id"]:
            raise ValueError("robot response command ID does not match; outcome unknown")
        return result

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

    def turn_steps(self, direction: str, cycles: int = 1, speed: int | None = None) -> CommandResponse:
        cmd = TurnStepsCommand(direction=direction, cycles=cycles, **({} if speed is None else {"speed": speed}))
        return self._post(Action.TURN_STEPS, cmd.model_dump())

    def stop(self) -> CommandResponse:
        self.cancel_commands("HTTP stop invalidated pending commands")
        return self._post(Action.STOP, StopCommand().model_dump())

    def cancel_commands(self, reason="stop invalidated pending commands") -> None:
        """Fence shared transport work before scheduling an independent STOP."""
        if self.realtime is not None:
            self.realtime.cancel_commands(reason)

    def emergency_stop(self) -> CommandResponse:
        """Independent, bounded stop usable after the ordinary client is closed."""
        self.cancel_commands("emergency HTTP stop invalidated pending commands")
        with httpx.Client(base_url=self.base_url, timeout=min(self.timeout, 1.0),
                          trust_env=False, follow_redirects=False) as client:
            response = client.post(ACTION_PATHS[Action.STOP], json=StopCommand().model_dump())
            response.raise_for_status()
            result = CommandResponse.model_validate(response.json())
            if result.action != Action.STOP:
                raise ValueError("emergency stop response action mismatch; state unknown")
            return result

    def resume(self, *, expected_control_epoch=None) -> CommandResponse:
        return self._post(Action.RESUME, ResumeCommand().model_dump(),
                          expected_control_epoch=expected_control_epoch)

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
