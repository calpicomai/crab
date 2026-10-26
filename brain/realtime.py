"""One local WebSocket session, latest status, and non-replayed command RPCs.

The reader never runs behavior or motor control. Callbacks must enqueue work and
return promptly. A bounded sender keeps socket backpressure off the stop path;
HTTP stop remains independent. Disconnect and timeout never replay a command.
"""
from __future__ import annotations

import math
import queue
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import InvalidStatus
from websockets.sync.client import ClientConnection, connect

from shared import Action, CommandResponse
from shared.clock import CaptureClock, capture_age
from shared.local import require_local_url
from shared.realtime import (MAX_MESSAGE_BYTES, WS_PATH, CommandRequest, ErrorMessage,
                             EventMessage, HelloMessage, LeaseMessage, LeaseRequest,
                             PingRequest, PongMessage, ResultMessage, StatusMessage,
                             encode_message, parse_server_message)


class RealtimeUnavailable(RuntimeError):
    """No fresh session/lease; the requested command wasn't dispatched."""


class RealtimeBusy(RealtimeUnavailable):
    """A command is already outstanding; requests are never queued behind it."""


class CommandOutcomeUnknown(RuntimeError):
    """Dispatch may have executed. Do not retry or substitute another transport."""


class _NoRedirectConnection(ClientConnection):
    def handshake(self, *args, **kwargs):
        try:
            return super().handshake(*args, **kwargs)
        except InvalidStatus as exc:
            if 300 <= exc.response.status_code < 400:
                # Sync connect follows InvalidStatus redirects in newer versions.
                # Transform this failure before that redirect handler sees it.
                raise RealtimeUnavailable("WebSocket redirects are disabled") from exc
            raise


@dataclass
class _Pending:
    request_id: str
    connection: object
    generation: int
    kind: str
    deadline: float
    action: Action | None = None
    command_id: str | None = None
    control_epoch: int | None = None
    event: threading.Event = field(default_factory=threading.Event)
    sent: bool = False
    response: object = None
    error: Exception | None = None


@dataclass
class _Dispatch:
    connection: object
    session_id: str
    message: object
    send_before: float
    generation: int | None = None
    pending: _Pending | None = None


class RealtimeClient:
    def __init__(self, base_url, *, on_status=None, on_event=None,
                 reconnect_min_s=0.2, reconnect_max_s=2.0, status_timeout_s=1.0):
        parsed = urlsplit(require_local_url(base_url))
        if parsed.query or parsed.fragment:
            raise ValueError("WebSocket base URL cannot contain a query or fragment")
        self.base_url = base_url.rstrip("/")
        self.url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                              parsed.path.rstrip("/") + WS_PATH, "", ""))
        for value in (reconnect_min_s, reconnect_max_s, status_timeout_s):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("WebSocket timeouts must be positive finite values")
        if reconnect_min_s > reconnect_max_s:
            raise ValueError("reconnect minimum must not exceed maximum")
        self.on_status, self.on_event = on_status, on_event
        self.reconnect_min_s, self.reconnect_max_s = reconnect_min_s, reconnect_max_s
        self.status_timeout_s = status_timeout_s
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._started = False
        self._connection = None
        self._boot_id = self._session_id = None
        self._control_epoch = None
        self._hello = None
        self._generation = 0
        self._pending = {}
        self._outgoing = queue.Queue(maxsize=2)  # one RPC plus one heartbeat
        self._status = None
        self._last_status_mono = None
        self._last_status_received_at = None
        self._status_sequence = self._event_sequence = 0
        self._last_message_received_at = None
        self._ping = None
        self._next_ping = 0.0
        self._last_rtt_ms = None
        self._clock = None
        self._waiting_status = None
        self._reconnects = 0
        self._failure_reason = None
        self._callback_error = None
        self._reader = threading.Thread(target=self._run, name="crab-realtime-reader", daemon=True)
        self._sender = threading.Thread(target=self._send_loop, name="crab-realtime-sender", daemon=True)

    def start(self):
        with self._lock:
            if self._started or self._closed.is_set():
                return
            self._started = True
            self._sender.start()
            self._reader.start()

    def _notify_status(self, status, reason=None):
        if self.on_status:
            try:
                self.on_status(status, reason)
            except Exception as exc:
                with self._lock:
                    self._callback_error = str(exc)[:200]

    def _notify_event(self, event):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as exc:
                with self._lock:
                    self._callback_error = str(exc)[:200]

    @staticmethod
    def _abort(connection):
        if connection is None:
            return
        # Shutdown interrupts a potentially blocked sendall without waiting for
        # the WebSocket protocol lock, keeping cancellation independent of it.
        sock = getattr(connection, "socket", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            connection.close()
        except Exception:
            pass

    @staticmethod
    def _pending_error(pending, reason):
        if pending.kind == "command" and pending.sent:
            return CommandOutcomeUnknown(f"{reason}; command outcome unknown, never retried")
        return RealtimeUnavailable(f"{reason}; command not dispatched")

    def _invalidate(self, connection, reason):
        with self._lock:
            if self._connection is not connection:
                return
            self._connection = None
            self._ready.clear()
            self._generation += 1
            self._status = None
            self._clock = None
            self._waiting_status = None
            self._failure_reason = str(reason)[:240]
            pending = list(self._pending.values())
            self._pending.clear()
            for request in pending:
                request.error = self._pending_error(request, self._failure_reason)
                request.event.set()
        self._notify_status(None, self._failure_reason)
        self._abort(connection)

    def cancel_commands(self, reason="stop invalidated pending commands"):
        """Synchronously fence old lease/RPC generations before HTTP STOP."""
        with self._lock:
            self._cancel_commands_locked(reason)

    def _cancel_commands_locked(self, reason):
        self._generation += 1
        for pending in self._pending.values():
            pending.error = self._pending_error(pending, reason)
            pending.event.set()
        self._pending.clear()

    def _accept_epoch_locked(self, epoch):
        """Fence older authority before publishing new Pi control provenance."""
        if self._control_epoch is not None:
            if epoch < self._control_epoch:
                return False
            if epoch > self._control_epoch:
                self._cancel_commands_locked("Pi control epoch changed")
        self._control_epoch = epoch
        return True

    def _run(self):
        delay = self.reconnect_min_s
        attempts = 0
        while not self._closed.is_set():
            with self._lock:
                self._reconnects = attempts
            connection = None
            try:
                with connect(self.url, proxy=None, create_connection=_NoRedirectConnection,
                             open_timeout=1.0, close_timeout=0.2, ping_interval=None,
                             compression=None, max_size=MAX_MESSAGE_BYTES, max_queue=4) as connection:
                    if self._closed.is_set():
                        break
                    with self._lock:
                        self._connection = connection
                        self._hello = None
                        self._boot_id = self._session_id = None
                        self._control_epoch = None
                        self._status_sequence = self._event_sequence = 0
                        self._last_status_mono = None
                        self._clock = None
                        self._waiting_status = None
                        self._ping = None
                        self._reconnects = attempts
                    opened = time.monotonic()
                    while not self._closed.is_set():
                        try:
                            raw = connection.recv(timeout=0.1)
                        except TimeoutError:
                            raw = None
                        if raw is not None:
                            self._receive(connection, raw)
                        self._heartbeat(connection, opened)
                        with self._lock:
                            if self._connection is not connection:
                                break
                        if self._ready.is_set():
                            delay = self.reconnect_min_s
            except Exception as exc:
                reason = str(exc)[:240] or type(exc).__name__
                if connection is not None:
                    self._invalidate(connection, reason)
                elif not self._closed.is_set():
                    with self._lock:
                        self._failure_reason = reason
                    self._notify_status(None, reason)
            finally:
                if connection is not None:
                    self._invalidate(connection, "WebSocket disconnected")
            attempts += 1
            if self._closed.wait(delay):
                break
            delay = min(self.reconnect_max_s, delay * 2)

    def _receive(self, connection, raw):
        message = parse_server_message(raw)
        callback = None
        waiting = None
        with self._lock:
            if self._connection is not connection:
                return
            now = time.monotonic()
            self._last_message_received_at = time.time()
            if self._hello is None:
                if isinstance(message, ErrorMessage):
                    raise RealtimeUnavailable(f"{message.code}: {message.reason}")
                if not isinstance(message, HelloMessage):
                    raise RealtimeUnavailable("WebSocket hello must be first")
                self._hello = message
                self._boot_id, self._session_id = message.boot_id, message.session_id
                self._control_epoch = message.control_epoch
                self._next_ping = now
                return
            if (message.boot_id, message.session_id) != (self._boot_id, self._session_id):
                raise RealtimeUnavailable("WebSocket boot/session mismatch")
            if isinstance(message, HelloMessage):
                raise RealtimeUnavailable("unexpected repeated WebSocket hello")
            if isinstance(message, StatusMessage):
                if message.status.boot_id != self._boot_id:
                    raise RealtimeUnavailable("status boot identity mismatch")
                if message.sequence <= self._status_sequence:
                    return
                if not self._accept_epoch_locked(message.control_epoch):
                    return
                if self._clock is None:
                    if self._waiting_status is None or message.sequence > self._waiting_status.sequence:
                        self._waiting_status = message  # one latest sample, never fresh by receipt
                    return
                status = message.status.model_dump(mode="json")
                status["control_epoch"] = message.control_epoch
                stamp = status.get("observed_at")
                age = capture_age(stamp, self._clock, mono=now)
                if age is None or age > self.status_timeout_s:
                    raise RealtimeUnavailable("WebSocket robot status capture is stale or clock is inconsistent")
                if self._status is not None and stamp <= self._status.get("observed_at", 0):
                    return
                self._status_sequence = message.sequence
                status["_clock"] = self._clock.model_dump()
                for health in status.get("inputs", {}).values():
                    health["clock"] = status["_clock"]
                self._last_status_mono = now-age
                self._last_status_received_at = time.time()
                self._status = status
                self._failure_reason = None
                self._ready.set()
                callback = ("status", status)
            elif isinstance(message, EventMessage):
                if message.sequence <= self._event_sequence:
                    return
                self._event_sequence = message.sequence
                if not self._accept_epoch_locked(message.control_epoch):
                    return
                age = capture_age(message.event.timestamp, self._clock) if self._clock else None
                if age is not None and age <= 5:
                    callback = ("event", {**message.event.model_dump(),
                                          "_clock": self._clock.model_dump(),
                                          "control_epoch": message.control_epoch})
            elif isinstance(message, PongMessage):
                if self._ping and message.request_id == self._ping[0]:
                    try:
                        self._clock = CaptureClock.measure(message.server_at, self._ping[2], self._ping[1],
                                                           time.time(), now, self._clock)
                    except ValueError as exc:
                        raise RealtimeUnavailable(f"WebSocket clock calibration failed: {exc}") from exc
                    self._last_rtt_ms = round((now - self._ping[1]) * 1000, 2)
                    self._ping = None
                    waiting, self._waiting_status = self._waiting_status, None
            elif isinstance(message, (ResultMessage, LeaseMessage, ErrorMessage)):
                pending = self._pending.get(message.request_id)
                if pending is None:
                    return  # cancelled/expired replies never become new work
                if now >= pending.deadline:
                    return  # caller times out; a late reply cannot revive an RPC
                if isinstance(message, ErrorMessage):
                    pending.error = self._pending_error(pending, f"Pi rejected request: {message.reason}")
                elif pending.kind == "lease" and isinstance(message, LeaseMessage):
                    if message.control_epoch != pending.control_epoch:
                        pending.error = RealtimeUnavailable("Pi lease control epoch mismatch; command not dispatched")
                    else:
                        pending.response = message
                elif pending.kind == "command" and isinstance(message, ResultMessage):
                    response = message.response
                    if response.action != pending.action or response.command_id != pending.command_id:
                        raise RealtimeUnavailable("command response correlation mismatch")
                    if response.status and response.status.boot_id != self._boot_id:
                        raise RealtimeUnavailable("command result boot identity mismatch")
                    pending.response = response
                else:
                    raise RealtimeUnavailable("RPC response type mismatch")
                pending.event.set()
        if callback:
            if callback[0] == "status":
                self._notify_status(callback[1])
            else:
                self._notify_event(callback[1])
        if waiting is not None:
            self._receive(connection, encode_message(waiting))

    def _heartbeat(self, connection, opened):
        with self._lock:
            if self._connection is not connection:
                return
            now = time.monotonic()
            if self._hello is None:
                if now - opened > 1.0:
                    raise RealtimeUnavailable("WebSocket hello timed out")
                return
            if self._clock is not None:
                try:
                    self._clock.remote_bounds(mono=now)
                except ValueError as exc:
                    raise RealtimeUnavailable(f"WebSocket clock invalid: {exc}") from exc
            if now - (self._last_status_mono or opened) > self.status_timeout_s:
                raise RealtimeUnavailable("WebSocket status stream expired")
            if self._ping and now - self._ping[1] > self._hello.heartbeat_timeout_s:
                raise RealtimeUnavailable("WebSocket peer heartbeat timed out")
            if self._ping is not None or now < self._next_ping:
                return
            request_id = uuid.uuid4().hex
            ping = PingRequest(boot_id=self._boot_id, session_id=self._session_id, request_id=request_id)
            self._ping = (request_id, now, time.time())
            self._next_ping = now + self._hello.heartbeat_s
            dispatch = _Dispatch(connection, self._session_id, ping,
                                 now + self._hello.heartbeat_timeout_s)
        try:
            self._outgoing.put_nowait(dispatch)
        except queue.Full as exc:
            raise RealtimeUnavailable("WebSocket sender backpressure") from exc

    def _send_loop(self):
        while not self._closed.is_set():
            try:
                dispatch = self._outgoing.get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                valid = (self._connection is dispatch.connection and self._session_id == dispatch.session_id
                         and (dispatch.generation is None or dispatch.generation == self._generation))
                if dispatch.pending is not None:
                    valid = valid and self._pending.get(dispatch.pending.request_id) is dispatch.pending
                    age = capture_age(self._status.get("observed_at") if self._status else None, self._clock)
                    valid = valid and self._clock is not None and age is not None and age <= self.status_timeout_s
                expired = time.monotonic() >= dispatch.send_before
                if valid and not expired and dispatch.pending:
                    dispatch.pending.sent = True  # send can partially complete before raising
            if not valid:
                continue
            if expired:
                if dispatch.pending:
                    with self._lock:
                        dispatch.pending.error = self._pending_error(dispatch.pending, "dispatch expired before send")
                        dispatch.pending.event.set()
                continue
            try:
                dispatch.connection.send(encode_message(dispatch.message))
            except Exception as exc:
                self._invalidate(dispatch.connection, f"WebSocket send failed: {str(exc)[:180]}")

    def _rpc(self, message, *, generation, connection, deadline, kind, send_before=None,
             action=None, command_id=None, control_epoch=None):
        pending = _Pending(message.request_id, connection, generation, kind, deadline,
                           action=action, command_id=command_id, control_epoch=control_epoch)
        with self._lock:
            age = capture_age(self._status.get("observed_at") if self._status else None, self._clock)
            if (self._closed.is_set() or self._connection is not connection or not self._ready.is_set()
                    or generation != self._generation or self._last_status_mono is None
                    or self._clock is None or age is None or age > self.status_timeout_s
                    or time.monotonic() - self._last_status_mono > self.status_timeout_s):
                raise RealtimeUnavailable("WebSocket command generation or session changed before dispatch")
            self._pending[pending.request_id] = pending
        try:
            self._outgoing.put_nowait(_Dispatch(connection, message.session_id, message,
                min(deadline, send_before if send_before is not None else deadline), generation, pending))
            remaining = max(0.0, deadline - time.monotonic())
            if not pending.event.wait(remaining):
                self._invalidate(connection, "WebSocket RPC timed out")
                raise self._pending_error(pending, "WebSocket RPC timed out")
            if pending.error:
                raise pending.error
            return pending.response
        except queue.Full as exc:
            raise RealtimeBusy("WebSocket sender busy; command not queued") from exc
        finally:
            with self._lock:
                self._pending.pop(pending.request_id, None)

    def command(self, action, payload, timeout=5.0, *, expected_control_epoch=None) -> CommandResponse:
        action = Action(action)
        if action in {Action.STOP, Action.GET_STATUS}:
            raise RealtimeUnavailable("stop and diagnostic status use independent HTTP")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("command timeout must be positive and finite")
        if expected_control_epoch is not None and (type(expected_control_epoch) is not int
                or not 0 <= expected_control_epoch <= 2**53 - 1):
            raise ValueError("expected control epoch must be a nonnegative bounded integer")
        if not self._command_lock.acquire(blocking=False):
            raise RealtimeBusy("WebSocket command already outstanding; new command not queued")
        try:
            started = time.monotonic()
            deadline = started + min(timeout, 30.0)
            with self._lock:
                if (self._connection is None or not self._ready.is_set() or self._closed.is_set()
                        or self._last_status_mono is None
                        or started - self._last_status_mono > self.status_timeout_s):
                    raise RealtimeUnavailable("WebSocket disconnected or robot status stale; command not dispatched")
                connection, generation = self._connection, self._generation
                boot_id, session_id = self._boot_id, self._session_id
                control_epoch = self._control_epoch if expected_control_epoch is None else expected_control_epoch
                if control_epoch != self._control_epoch:
                    raise RealtimeUnavailable("action control epoch is stale; command not dispatched")
            # Every command obtains its own Pi-owned lease. Neither this RPC nor
            # its result survives cancellation, disconnect, or session change.
            lease_started = time.monotonic()
            lease = self._rpc(LeaseRequest(boot_id=boot_id, session_id=session_id,
                                          request_id=uuid.uuid4().hex, control_epoch=control_epoch),
                              generation=generation, connection=connection,
                              deadline=deadline, kind="lease", control_epoch=control_epoch)
            if not lease.ok:
                raise RealtimeUnavailable(lease.reason or "Pi refused command lease")
            lease_deadline = lease_started + lease.ttl_s
            if time.monotonic() >= lease_deadline:
                raise RealtimeUnavailable("Pi lease expired in transit; command not dispatched")
            command_id = uuid.uuid4().hex
            request = CommandRequest(boot_id=boot_id, session_id=session_id,
                                     request_id=uuid.uuid4().hex, action=action,
                                     payload={**payload, "lease_id": lease.lease_id, "command_id": command_id})
            return self._rpc(request, generation=generation, connection=connection,
                             deadline=deadline, send_before=lease_deadline, kind="command",
                             action=action, command_id=command_id)
        finally:
            self._command_lock.release()

    def health(self):
        with self._lock:
            stamp = self._status.get("observed_at") if self._status else None
            return {"transport": "websocket", "connected": self._connection is not None and self._hello is not None,
                    "ready": self._ready.is_set(), "closed": self._closed.is_set(),
                    "boot_id": self._boot_id, "session_id": self._session_id,
                    "control_epoch": self._control_epoch,
                    "reconnects": self._reconnects, "failure_reason": self._failure_reason,
                    "last_rtt_ms": self._last_rtt_ms,
                    "last_status_received_at": self._last_status_received_at,
                    "last_message_received_at": self._last_message_received_at,
                    "status_capture_age_s": capture_age(stamp, self._clock) if self._clock else None,
                    "clock": self._clock.model_dump() if self._clock else None,
                    "pending_requests": len(self._pending), "send_queue_depth": self._outgoing.qsize(),
                    "callback_error": self._callback_error}

    def close(self):
        self._closed.set()
        self.cancel_commands("WebSocket client closed")
        with self._lock:
            connection, started = self._connection, self._started
        if connection is not None:
            self._invalidate(connection, "WebSocket client closed")
        if started:
            deadline = time.monotonic() + 2.0
            for thread in (self._reader, self._sender):
                if thread is not threading.current_thread():
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
