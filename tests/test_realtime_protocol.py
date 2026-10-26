"""WebSocket framing and canonical-command contracts; no I/O or actuators."""
from __future__ import annotations

import json
import unittest

from shared.health import InputHealth
from shared.protocol import Action, CommandResponse, RobotStatus
from shared.realtime import (
    COMMAND_MODELS, MAX_MESSAGE_BYTES, PROTOCOL_VERSION, WS_PATH,
    CommandRequest, ErrorMessage, EventMessage, HelloMessage, InputEvent,
    LeaseMessage, LeaseRequest, PingRequest, PongMessage, ProtocolError,
    ResultMessage, StatusMessage, encode_message, parse_client_message,
    parse_server_message,
)

BASE = {"boot_id": "pi-boot", "session_id": "connection_1"}
REQUEST = {**BASE, "request_id": "request-1"}


def wire(message, **changes):
    return json.dumps({**message.model_dump(mode="json"), **changes}, separators=(",", ":"))


class ClientProtocolTests(unittest.TestCase):
    def test_requests_round_trip_and_every_action_reuses_existing_model(self):
        self.assertEqual(WS_PATH, "/companion/ws")
        self.assertEqual(PROTOCOL_VERSION, 1)
        self.assertEqual(set(COMMAND_MODELS), set(Action))
        special = {Action.TURN: {"degrees": 0.0}, Action.TURN_STEPS: {"direction": "left"},
                   Action.TEST_LEG: {"leg": 2}}
        for action, model in COMMAND_MODELS.items():
            with self.subTest(action=action):
                payload = {"command_id": "intent-1", "lease_id": "pi-lease", **special.get(action, {})}
                message = CommandRequest(**REQUEST, action=action, payload=payload)
                parsed = parse_client_message(encode_message(message))
                self.assertIsInstance(parsed, CommandRequest)
                self.assertIs(type(parsed.command), model)
                self.assertEqual(parsed.command.model_dump(), model.model_validate(payload).model_dump())
                self.assertEqual(parsed.action, action)
                self.assertEqual(parsed.request_id, REQUEST["request_id"])
        for kind in (LeaseRequest, PingRequest):
            message = kind(**REQUEST, **({"control_epoch": 0} if kind is LeaseRequest else {}))
            self.assertEqual(parse_client_message(encode_message(message)), message)

    def test_stop_requires_correlation_but_not_a_motion_lease(self):
        message = CommandRequest(**REQUEST, action=Action.STOP)
        self.assertIsNone(parse_client_message(encode_message(message)).command.lease_id)
        raw = message.model_dump(mode="json")
        del raw["request_id"]
        with self.assertRaises(ProtocolError):
            parse_client_message(json.dumps(raw))
        # Missing motion authority still reaches the existing canonical handler,
        # which rejects execution; the transport never fabricates a lease.
        self.assertIsNone(CommandRequest(**REQUEST, action=Action.STAND).command.lease_id)

    def test_unknown_actions_envelope_fields_and_servo_payload_are_rejected(self):
        message = CommandRequest(**REQUEST, action=Action.WALK, payload={"steps": 1})
        for changes in ({"action": "set_servo"}, {"type": "servo_trajectory"},
                        {"angles": [0]*12}, {"expires_at": 999999999999.0},
                        {"payload": {"steps": 1, "servo_angles": [0]*12}},
                        {"payload": {"steps": 1, "trajectory": []}},
                        {"payload": {"steps": 1, "mode": "custom"}},
                        {"payload": {"steps": 1, "expires_at": 99999999999.0}}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                parse_client_message(wire(message, **changes))

    def test_command_bounds_are_not_loosened_by_websocket_transport(self):
        message = CommandRequest(**REQUEST, action=Action.WALK)
        for payload in ({"steps": 5}, {"steps": -1}, {"speed": 101}, {"speed": 0},
                        {"steps": True}, {"steps": "1"}, {"steps": 1.0},
                        {"min_clearance_cm": -1.0}, {"min_clearance_cm": 401.0}):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                parse_client_message(wire(message, payload=payload))
        turn = CommandRequest(**REQUEST, action="turn_steps", payload={"direction": "right"})
        for payload in ({"direction": "reverse"}, {"direction": "left", "cycles": 5},
                        {"direction": "left", "cycles": 0}):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                parse_client_message(wire(turn, payload=payload))

    def test_explicit_supported_version_and_session_identity_are_required(self):
        message = PingRequest(**REQUEST)
        for name in ("version", "type", "boot_id", "session_id", "request_id"):
            raw = message.model_dump(mode="json")
            del raw[name]
            with self.subTest(missing=name), self.assertRaises(ProtocolError):
                parse_client_message(json.dumps(raw))
        for version in (0, 2, True, 1.0, "1", None):
            with self.subTest(version=version), self.assertRaises(ProtocolError):
                parse_client_message(wire(message, version=version))

    def test_identifiers_are_bounded_nonempty_tokens(self):
        message = PingRequest(**REQUEST)
        for name in ("boot_id", "session_id", "request_id"):
            self.assertIsInstance(parse_client_message(wire(message, **{name: "x"*64})), PingRequest)
            for value in ("", "x"*65, "space here", "../path", "é", 1, None):
                with self.subTest(field=name, value=value), self.assertRaises(ProtocolError):
                    parse_client_message(wire(message, **{name: value}))
        command = CommandRequest(**REQUEST, action=Action.STAND)
        for name in ("lease_id", "command_id"):
            for value in ("", "bad token", "x"*65):
                with self.subTest(field=name, value=value), self.assertRaises(ProtocolError):
                    parse_client_message(wire(command, payload={name: value}))


class ServerProtocolTests(unittest.TestCase):
    def test_all_server_messages_round_trip_with_correlation(self):
        status = RobotStatus(boot_id=BASE["boot_id"], observed_at=123.0, battery_v=None)
        response = CommandResponse(ok=False, action=Action.STAND, command_id="intent-1",
                                   detail="battery unavailable", status=status)
        messages = [HelloMessage(**BASE, control_epoch=0), StatusMessage(**BASE, control_epoch=0, sequence=1, status=status),
                    EventMessage(**BASE, control_epoch=0, sequence=1, event=InputEvent(text="stop", timestamp=123.0)),
                    ResultMessage(**REQUEST, response=response),
                    LeaseMessage(**REQUEST, control_epoch=0, ok=True, lease_id="pi-minted", ttl_s=2.0),
                    LeaseMessage(**REQUEST, control_epoch=0, ok=False, reason="lease capacity reached"),
                    ErrorMessage(**BASE, code="invalid_message", reason="invalid JSON"),
                    ErrorMessage(**REQUEST, code="session_mismatch", reason="old connection"),
                    PongMessage(**REQUEST)]
        for message in messages:
            with self.subTest(kind=message.type):
                self.assertEqual(parse_server_message(encode_message(message)), message)
        result = parse_server_message(encode_message(messages[3]))
        self.assertEqual(result.request_id, "request-1")
        self.assertEqual(result.response.command_id, "intent-1")
        self.assertFalse(result.response.ok)

    def test_capture_timestamps_and_unknown_values_survive_transport_unchanged(self):
        health = InputHealth(available=False, provenance="unknown", captured_at=100.25,
                             received_at=101.25, failure_reason="capture disconnected")
        status = RobotStatus(boot_id=BASE["boot_id"], observed_at=200.5, distance_cm=None,
                             inputs={"camera": health}, motion_state={"position_feedback": False})
        before = status.model_dump(mode="json")
        parsed = parse_server_message(encode_message(StatusMessage(**BASE, control_epoch=0, sequence=7, status=status)))
        self.assertEqual(parsed.status.model_dump(mode="json"), before)
        self.assertEqual(parsed.status.inputs["camera"].captured_at, 100.25)
        self.assertEqual(parsed.status.inputs["camera"].received_at, 101.25)
        self.assertIsNone(parsed.status.distance_cm)
        self.assertFalse(parsed.status.inputs["camera"].available)

    def test_sequence_is_bounded_integer_and_receive_order_is_not_a_timestamp(self):
        message = StatusMessage(**BASE, control_epoch=0, sequence=2, status=RobotStatus())
        for sequence in (0, -1, True, 2.0, "2", 2**53):
            with self.subTest(sequence=sequence), self.assertRaises(ProtocolError):
                parse_server_message(wire(message, sequence=sequence))
        self.assertEqual(parse_server_message(wire(message, sequence=2**53-1)).sequence, 2**53-1)

    def test_lease_duration_is_relative_and_cannot_be_chosen_by_requester(self):
        request = LeaseRequest(**REQUEST, control_epoch=0)
        for field in ("ttl_s", "expires_at", "lease_id"):
            with self.subTest(field=field), self.assertRaises(ProtocolError):
                parse_client_message(wire(request, **{field: 9999999}))
        reply = LeaseMessage(**REQUEST, control_epoch=0, ok=True, lease_id="pi-minted", ttl_s=2.0)
        for changes in ({"ok": True, "lease_id": None}, {"ok": True, "ttl_s": None},
                        {"ok": False}, {"ttl_s": -1.0}, {"ttl_s": 0.0},
                        {"expires_at": 99999999999.0}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                parse_server_message(wire(reply, **changes))

    def test_hello_heartbeat_window_and_protocol_limits(self):
        hello = HelloMessage(**BASE, control_epoch=0)
        self.assertEqual(hello.max_message_bytes, MAX_MESSAGE_BYTES)
        self.assertGreater(hello.heartbeat_timeout_s, hello.heartbeat_s)
        for changes in ({"heartbeat_s": 0.0}, {"heartbeat_timeout_s": 1.0},
                        {"status_interval_s": 0.0}, {"max_message_bytes": MAX_MESSAGE_BYTES+1}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                parse_server_message(wire(hello, **changes))

    def test_events_cannot_invent_microphone_capture_or_unbounded_text(self):
        event = EventMessage(**BASE, control_epoch=0, sequence=1, event=InputEvent(text="hello", timestamp=123.0))
        for payload in ({"text": "x"*501, "timestamp": 123.0, "source": "dashboard"},
                        {"text": "hello", "timestamp": 123.0, "source": "microphone"},
                        {"text": "hello", "timestamp": -1.0},
                        {"text": "hello", "timestamp": 123.0, "recognized_person": "owner"}):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                parse_server_message(wire(event, event=payload))


class ControlEpochTests(unittest.TestCase):
    def messages(self):
        return [
            (LeaseRequest(**REQUEST, control_epoch=0), parse_client_message),
            (HelloMessage(**BASE, control_epoch=0), parse_server_message),
            (StatusMessage(**BASE, control_epoch=0, sequence=1, status=RobotStatus()), parse_server_message),
            (EventMessage(**BASE, control_epoch=0, sequence=1,
                          event=InputEvent(text="resume", timestamp=123.0)), parse_server_message),
            (LeaseMessage(**REQUEST, control_epoch=0, ok=True, lease_id="pi-minted", ttl_s=2.0),
             parse_server_message),
            (LeaseMessage(**REQUEST, control_epoch=0, ok=False, reason="control epoch changed"),
             parse_server_message),
        ]

    def test_control_epoch_is_required_strict_and_bounded(self):
        for message, parse in self.messages():
            with self.subTest(kind=type(message).__name__, valid=0):
                self.assertEqual(parse(encode_message(message)).control_epoch, 0)
            with self.subTest(kind=type(message).__name__, valid=2**53-1):
                self.assertEqual(parse(wire(message, control_epoch=2**53-1)).control_epoch, 2**53-1)
            missing = message.model_dump(mode="json")
            del missing["control_epoch"]
            with self.subTest(kind=type(message).__name__, missing=True), self.assertRaises(ProtocolError):
                parse(json.dumps(missing))
            for invalid in (-1, 2**53, True, False, 0.0, "0", None):
                with self.subTest(kind=type(message).__name__, value=invalid), self.assertRaises(ProtocolError):
                    parse(wire(message, control_epoch=invalid))

    def test_event_retains_original_epoch_across_newer_status_and_lease_messages(self):
        event = EventMessage(**BASE, control_epoch=4, sequence=1,
                             event=InputEvent(text="resume", timestamp=123.0))
        status = StatusMessage(**BASE, control_epoch=5, sequence=2, status=RobotStatus())
        request = LeaseRequest(**REQUEST, control_epoch=event.control_epoch)
        rejection = LeaseMessage(**REQUEST, control_epoch=5, ok=False, reason="control epoch changed")
        parsed = [parse_server_message(encode_message(message)) for message in (event, status, rejection)]
        self.assertEqual([message.control_epoch for message in parsed], [4, 5, 5])
        self.assertEqual(parse_client_message(encode_message(request)).control_epoch, 4)

    def test_commands_cannot_supply_epoch_outside_pi_minted_lease(self):
        command = CommandRequest(**REQUEST, action=Action.RESUME,
                                 payload={"lease_id": "pi-minted", "command_id": "intent-1"})
        for changes in ({"control_epoch": 5}, {"payload": {**command.payload, "control_epoch": 5}}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                parse_client_message(wire(command, **changes))


class FramingTests(unittest.TestCase):
    def test_size_limit_is_utf8_bytes_and_applies_before_decoding(self):
        ping = encode_message(PingRequest(**REQUEST))
        exact = ping + " " * (MAX_MESSAGE_BYTES-len(ping.encode("utf-8")))
        self.assertEqual(len(exact.encode("utf-8")), MAX_MESSAGE_BYTES)
        self.assertIsInstance(parse_client_message(exact), PingRequest)
        with self.assertRaises(ProtocolError):
            parse_client_message(exact + " ")
        status = StatusMessage(**BASE, control_epoch=0, sequence=1, status=RobotStatus(motion_state={"text": "🐾"*17000}))
        with self.assertRaises(ProtocolError):
            encode_message(status)
        payload = json.dumps(status.model_dump(mode="json"), ensure_ascii=False)
        self.assertLess(len(payload), MAX_MESSAGE_BYTES)
        with self.assertRaises(ProtocolError):
            parse_server_message(payload)

    def test_malformed_duplicate_and_nonfinite_json_are_rejected(self):
        invalid = ["", "[]", "null", '"string"', "{broken}", b"\xff",
                   '{"version":1,"version":1}', "["*1100+"0"+"]"*1100]
        ping = encode_message(PingRequest(**REQUEST))
        invalid.append(ping[:-1] + ',"request_id":"different"}')
        command = CommandRequest(**REQUEST, action=Action.WALK)
        for value in ("NaN", "Infinity", "-Infinity", "1e9999"):
            raw = wire(command)
            invalid.append(raw.replace('"payload":{}', '"payload":{"min_clearance_cm":'+value+'}'))
        for raw in invalid:
            with self.subTest(raw=str(raw)[:80]), self.assertRaises(ProtocolError):
                parse_client_message(raw)

    def test_outgoing_revalidates_nested_mutation_and_finite_status_metadata(self):
        message = CommandRequest(**REQUEST, action=Action.WALK, payload={"steps": 1})
        message.payload["steps"] = True
        with self.assertRaises(ProtocolError):
            encode_message(message)
        status = StatusMessage(**BASE, control_epoch=0, sequence=1, status=RobotStatus())
        status.status.motion_state["nested"] = {"not_finite": float("nan")}
        with self.assertRaises(ProtocolError):
            encode_message(status)
        with self.assertRaises(ProtocolError):
            encode_message({"type": "ping"})

    def test_message_types_are_directional(self):
        with self.assertRaises(ProtocolError):
            parse_client_message(encode_message(PongMessage(**REQUEST)))
        with self.assertRaises(ProtocolError):
            parse_server_message(encode_message(PingRequest(**REQUEST)))
        with self.assertRaises(ProtocolError):
            parse_server_message(wire(HelloMessage(**BASE, control_epoch=0), servo_commands=[]))


if __name__ == "__main__":
    unittest.main()
