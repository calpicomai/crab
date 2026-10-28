"""Versioned local WebSocket envelopes over the existing command protocol.

This module is transport-free. Leases and interruption remain Pi-owned; message
wall timestamps are observations, never command-expiry authority. Socket receipt
must not replace input capture timestamps. Media continues over existing paths.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .protocol import (
    Action, Command, CommandResponse, GetStatusCommand, ResumeCommand, RobotStatus,
    SitCommand, StandCommand, StopCommand, TestLegCommand, TurnCommand,
    TurnStepsCommand, WalkCommand,
)

WS_PATH = "/companion/ws"
# Optional observation metadata, negotiated in the HTTP upgrade so old strict
# protocol-v1 peers keep their original Hello and event JSON shapes.
INPUT_RECEIPTS_HEADER = "X-Crab-Input-Receipts"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
HEARTBEAT_INTERVAL_S = 1.0
HEARTBEAT_TIMEOUT_S = 3.0
STATUS_INTERVAL_S = 0.2

Identifier = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Sequence = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]
ControlEpoch = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
_ID = TypeAdapter(Identifier)

# The models and bounds are defined once, in protocol.py. This map only selects
# the canonical model for a wire action; no motor parameter schema is duplicated.
COMMAND_MODELS: dict[Action, type[Command]] = {
    Action.STOP: StopCommand,
    Action.RESUME: ResumeCommand,
    Action.TURN_STEPS: TurnStepsCommand,
    Action.WALK: WalkCommand,
    Action.TURN: TurnCommand,
    Action.STAND: StandCommand,
    Action.SIT: SitCommand,
    Action.GET_STATUS: GetStatusCommand,
    Action.TEST_LEG: TestLegCommand,
}


class ProtocolError(ValueError):
    """Invalid version, framing, size, or envelope; never an executable request."""


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    version: Literal[1] = PROTOCOL_VERSION
    boot_id: Identifier
    session_id: Identifier

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != PROTOCOL_VERSION:
            raise ValueError("unsupported WebSocket protocol version")
        return value


class RequestEnvelope(Envelope):
    request_id: Identifier


class LeaseRequest(RequestEnvelope):
    type: Literal["lease"] = "lease"
    # The epoch of the action that caused this request, never a newer status.
    # The Pi grants authority only while that control epoch remains current.
    control_epoch: ControlEpoch


class CommandRequest(RequestEnvelope):
    type: Literal["command"] = "command"
    action: Action
    payload: dict = Field(default_factory=dict)

    @field_validator("action", mode="before")
    @classmethod
    def canonical_action(cls, value):
        # Python JSON decoding produces a string; strict envelope validation
        # should not require callers to construct an enum before wire parsing.
        if isinstance(value, str):
            return Action(value)
        return value

    @model_validator(mode="after")
    def canonical_payload(self):
        command = self.command
        for value in (command.command_id, command.lease_id):
            if value is not None:
                _ID.validate_python(value, strict=True)
        return self

    @property
    def command(self) -> Command:
        """Validated existing command model, ready for the shared Pi handler.

        Missing leases/command IDs are rejected by that handler exactly as over
        HTTP. STOP and GET_STATUS do not acquire authority through a lease.
        """
        return COMMAND_MODELS[self.action].model_validate(self.payload, strict=True)


class PingRequest(RequestEnvelope):
    type: Literal["ping"] = "ping"


class HelloMessage(Envelope):
    type: Literal["hello"] = "hello"
    control_epoch: ControlEpoch
    heartbeat_s: float = Field(default=HEARTBEAT_INTERVAL_S, gt=0, le=10)
    heartbeat_timeout_s: float = Field(default=HEARTBEAT_TIMEOUT_S, gt=0, le=30)
    status_interval_s: float = Field(default=STATUS_INTERVAL_S, gt=0, le=10)
    max_message_bytes: Literal[65536] = MAX_MESSAGE_BYTES

    @model_validator(mode="after")
    def heartbeat_window(self):
        if self.heartbeat_timeout_s <= self.heartbeat_s:
            raise ValueError("heartbeat timeout must exceed heartbeat interval")
        return self


class StatusMessage(Envelope):
    type: Literal["status"] = "status"
    control_epoch: ControlEpoch
    sequence: Sequence
    status: RobotStatus


class InputEvent(BaseModel):
    """The Pi's existing dashboard text input; no synthetic microphone claim."""
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=500)
    timestamp: float = Field(ge=0)
    source: Literal["dashboard"] = "dashboard"
    # Observation/correlation only: never a lease, epoch or freshness proof.
    # Optional so a current companion can still receive events from an older Pi.
    event_id: Identifier | None = None


class EventMessage(Envelope):
    type: Literal["event"] = "event"
    # Provenance of this original explicit action. Receivers must not replace
    # this with an epoch learned from a status received after the action.
    control_epoch: ControlEpoch
    sequence: Sequence
    event: InputEvent


class ResultMessage(RequestEnvelope):
    type: Literal["result"] = "result"
    response: CommandResponse


class LeaseMessage(RequestEnvelope):
    type: Literal["lease"] = "lease"
    control_epoch: ControlEpoch
    ok: bool
    lease_id: Identifier | None = None
    # Advisory relative duration only. The Pi checks its own monotonic expiry
    # immediately before executing the canonical handler; do not add expires_at.
    ttl_s: float | None = Field(default=None, gt=0, le=30)
    reason: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def usable_lease(self):
        if self.ok and (self.lease_id is None or self.ttl_s is None):
            raise ValueError("successful lease response requires lease_id and ttl_s")
        if not self.ok and (self.lease_id is not None or self.ttl_s is not None):
            raise ValueError("rejected lease response cannot carry an executable lease")
        return self


class ErrorMessage(Envelope):
    type: Literal["error"] = "error"
    request_id: Identifier | None = None
    code: Identifier
    reason: str = Field(min_length=1, max_length=300)


class PongMessage(RequestEnvelope):
    type: Literal["pong"] = "pong"
    # Optional only for decoding an older peer: the new client fails closed
    # without a measured clock. No control authority comes from this value.
    server_at: float | None = Field(default=None, gt=0)


ClientMessage = Annotated[Union[LeaseRequest, CommandRequest, PingRequest], Field(discriminator="type")]
ServerMessage = Annotated[
    Union[HelloMessage, StatusMessage, EventMessage, ResultMessage, LeaseMessage, ErrorMessage, PongMessage],
    Field(discriminator="type"),
]
_CLIENT = TypeAdapter(ClientMessage)
_SERVER = TypeAdapter(ServerMessage)


def _pairs_unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ProtocolError("duplicate JSON field")
        result[name] = value
    return result


def _reject_constant(value):
    raise ProtocolError("WebSocket messages must contain finite JSON numbers")


def _decode(raw: str | bytes) -> dict:
    if not isinstance(raw, (str, bytes)):
        raise ProtocolError("WebSocket message must be JSON text")
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ProtocolError("WebSocket message exceeds 64 KiB")
        data = json.loads(encoded.decode("utf-8"), object_pairs_hook=_pairs_unique,
                          parse_constant=_reject_constant)
        # Float overflow (1e9999) does not call parse_constant. Check recursively,
        # including opaque canonical status/motion metadata and nested arrays.
        json.dumps(data, allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid or nonfinite WebSocket JSON") from exc
    if not isinstance(data, dict):
        raise ProtocolError("WebSocket message must be a JSON object")
    if not {"version", "type", "boot_id", "session_id"}.issubset(data):
        raise ProtocolError("WebSocket version, type, boot_id and session_id are required")
    return data


def parse_client_message(raw: str | bytes):
    """Parse a bounded request. Session/boot equality is checked by the adapter."""
    try:
        return _CLIENT.validate_python(_decode(raw))
    except ValueError as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid client WebSocket envelope") from exc


def parse_server_message(raw: str | bytes):
    """Parse a bounded response without changing any captured timestamps."""
    try:
        return _SERVER.validate_python(_decode(raw))
    except ValueError as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid server WebSocket envelope") from exc


def encode_message(message: Envelope) -> str:
    """Serialize one validated envelope, with the same finite JSON/size limits."""
    if not isinstance(message, Envelope):
        raise ProtocolError("expected a validated WebSocket envelope")
    try:
        # Revalidation also catches nested mutation after a model was constructed.
        # Check Python values before JSON-mode serialization: Pydantic can turn
        # a nested NaN into null, which would silently erase a failed input.
        payload = message.model_dump(mode="python")
        json.dumps(payload, allow_nan=False)
        adapter = _CLIENT if isinstance(message, (LeaseRequest, CommandRequest, PingRequest)) else _SERVER
        validated = adapter.validate_python(payload)
        wire = validated.model_dump(mode="json")
        if isinstance(validated, EventMessage) and validated.event.event_id is None:
            wire["event"].pop("event_id", None)
        encoded = json.dumps(wire, allow_nan=False,
                             ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError("WebSocket message exceeds 64 KiB")
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("cannot encode invalid WebSocket envelope") from exc
