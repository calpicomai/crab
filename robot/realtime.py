"""Bounded local companion transport; the existing Pi handlers own authority.

One connection owns event delivery. Status is sampled by one worker thread,
never by viewers or the asyncio receiver. A lost session latches the same STOP
as HTTP and cannot replay queued commands or dashboard events on reconnection.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import logging
import threading
import time
import uuid

from shared import Action, CommandResponse
from shared.realtime import (
    CommandRequest, ErrorMessage, EventMessage, HelloMessage, InputEvent,
    LeaseMessage, LeaseRequest, PingRequest, PongMessage, ProtocolError,
    ResultMessage, StatusMessage, HEARTBEAT_TIMEOUT_S, STATUS_INTERVAL_S,
    WS_PATH, PROTOCOL_VERSION, INPUT_RECEIPTS_HEADER, encode_message, parse_client_message,
)

logger = logging.getLogger("picrawler.realtime")


@dataclass
class _Session:
    boot_id: str
    session_id: str
    loop: asyncio.AbstractEventLoop
    input_receipts: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    generation: int = 0
    live: bool = True
    wake_pending: bool = False
    fatal: str | None = None
    leases: dict[str, float] = field(default_factory=dict)
    acknowledgements: deque = field(default_factory=deque)
    events: deque = field(default_factory=deque)
    urgent: object | None = None
    status: object | None = None

    @property
    def envelope(self):
        return {"boot_id": self.boot_id, "session_id": self.session_id}


class RealtimeHub:
    """One owner, one status sampler and at most one motion dispatch worker.

    ``dispatch_fn(action, command, authorize, status_provider)`` invokes the
    canonical handler. It MUST check authorize within the same short transaction
    that orders lease consumption / RESUME against STOP. ``stop_fn`` must call
    invalidate_commands within that transaction, and must never wait for a gait.
    ``lease_fn(expected_epoch, register)`` checks the original event's control
    epoch, then issues and registers a lease in that transaction too.
    Callbacks and samples execute off the asyncio event loop.
    """

    ACK_LIMIT = 16
    EVENT_LIMIT = 32

    def __init__(self, *, boot_id_fn, epoch_fn, status_fn, dispatch_fn, lease_fn, stop_fn,
                 clear_events_fn=lambda: None,
                 heartbeat_timeout_s=HEARTBEAT_TIMEOUT_S,
                 status_interval_s=STATUS_INTERVAL_S, send_timeout_s=1.0):
        self.boot_id_fn, self.status_fn = boot_id_fn, status_fn
        self.epoch_fn = epoch_fn
        self.dispatch_fn, self.lease_fn, self.stop_fn = dispatch_fn, lease_fn, stop_fn
        self.clear_events_fn = clear_events_fn
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.status_interval_s = status_interval_s
        self.send_timeout_s = send_timeout_s
        self._lock = threading.Lock()
        self._owner: _Session | None = None
        self._command_running = False
        self._sequence = 0
        self._last_status = None
        self._sampler: threading.Thread | None = None
        self._sampler_stop = threading.Event()
        self._sample_wake = threading.Event()
        self._connections = self._rejected_connections = self._status_samples = 0
        self._last_error = None

    def health(self):
        """Read-only advertisement; connecting just to probe would latch STOP."""
        with self._lock:
            return {"available": True, "path": WS_PATH, "protocol_version": PROTOCOL_VERSION,
                    "session_active": self._owner is not None and self._owner.live,
                    "command_in_flight": self._command_running,
                    "control_epoch": self.epoch_fn(),
                    "status_interval_s": self.status_interval_s,
                    "heartbeat_timeout_s": self.heartbeat_timeout_s,
                    "connections": self._connections, "rejected_connections": self._rejected_connections,
                    "status_samples": self._status_samples,
                    "last_status_observed_at": getattr(self._last_status, "observed_at", None),
                    "last_error": self._last_error,
                    "disconnect_policy": "latch stop; explicit fresh resume required"}

    @property
    def has_owner(self):
        with self._lock:
            return self._owner is not None

    def latest_status(self):
        with self._lock:
            return self._last_status.model_copy(deep=True) if self._last_status is not None else None

    def _notify_locked(self, session):
        # One pending callback, even if an HTTP producer floods the connection.
        if session.wake_pending or session.loop.is_closed():
            return
        session.wake_pending = True
        def wake():
            with self._lock:
                session.wake_pending = False
            session.ready.set()
        try:
            session.loop.call_soon_threadsafe(wake)
        except RuntimeError:
            session.wake_pending = False

    def _ack(self, session, message):
        with self._lock:
            if self._owner is not session or not session.live:
                return
            if len(session.acknowledgements) >= self.ACK_LIMIT:
                session.fatal = "acknowledgement queue full"
            else:
                session.acknowledgements.append(message)
            self._notify_locked(session)

    def _error(self, session, request_id, code, reason):
        self._ack(session, ErrorMessage(**session.envelope, request_id=request_id,
                                        code=code, reason=reason[:300]))

    def invalidate_commands(self):
        """Synchronous, nonblocking hook called inside the Pi STOP transaction."""
        with self._lock:
            if self._owner is not None:
                self._owner.generation += 1
                self._owner.leases.clear()
                self._owner.events.clear()
                self._owner.urgent = None

    def publish_event(self, item: dict, *, control_epoch: int, urgent=False,
                      receipt: dict | None = None) -> bool | None:
        """Nonblocking HTTP hook: None means no WS owner; False means full.

        An active session exclusively consumes input. Nothing is copied to an
        HTTP replay queue. STOP has its own replaceable slot and clears social
        events; actuator stopping itself remains the HTTP endpoint's job.
        An optional diagnostic receipt gets the accepted session atomically;
        reading health afterwards could instead report a replacement owner.
        """
        event = InputEvent.model_validate(item)
        with self._lock:
            session = self._owner
            if session is None:
                return None
            if not session.live:
                return False
            if not urgent and len(session.events) >= self.EVENT_LIMIT:
                return False
            if not session.input_receipts:
                event = event.model_copy(update={"event_id": None})
            self._sequence += 1
            message = EventMessage(**session.envelope, sequence=self._sequence,
                                   control_epoch=control_epoch, event=event)
            if urgent:
                session.events.clear()
                session.urgent = message
            else:
                session.events.append(message)
            if receipt is not None:
                receipt.update(session.envelope)
                receipt["correlation_available"] = session.input_receipts
            self._notify_locked(session)
            return True

    def _start_sampler(self):
        with self._lock:
            if self._sampler is not None and self._sampler.is_alive():
                self._sample_wake.set()
                return
            self._sampler_stop.clear()
            self._sampler = threading.Thread(target=self._sample, name="crab-status", daemon=True)
            self._sampler.start()

    def _sample(self):
        while not self._sampler_stop.is_set():
            with self._lock:
                session = self._owner
                active = session is not None and session.live
            if not active:
                self._sample_wake.wait(self.status_interval_s)
                self._sample_wake.clear()
                continue
            started = time.monotonic()
            try:
                status = self.status_fn()
                with self._lock:
                    if self._owner is session and session.live:
                        self._status_samples += 1
                        self._last_status = status.model_copy(deep=True)
                        self._sequence += 1
                        session.status = StatusMessage(**session.envelope, sequence=self._sequence,
                                                       control_epoch=self.epoch_fn(), status=status)
                        self._notify_locked(session)
            except Exception as exc:
                # Retain the original capture timestamp. A failed sample must
                # never republish an old observation with fresh receipt evidence.
                logger.warning("companion status sample failed", exc_info=True)
                with self._lock:
                    self._last_error = "status sample failed: " + str(exc)[:180]
            self._sampler_stop.wait(max(0, self.status_interval_s - (time.monotonic()-started)))

    def _register_lease(self, session, generation, lease):
        # Called by lease_fn while the server control-transition lock is held.
        with self._lock:
            if self._owner is not session or not session.live or session.generation != generation:
                return False
            now = time.monotonic()
            session.leases = {k: v for k, v in session.leases.items() if v > now}
            if len(session.leases) >= 128:
                return False
            if lease.get("ok"):
                session.leases[lease["lease_id"]] = now + lease["ttl_s"]
            return True

    def _authorize(self, session, generation, command):
        with self._lock:
            if self._owner is not session or not session.live or generation != session.generation:
                return "companion session cancelled or stopped; fresh command required"
            if session.boot_id != self.boot_id_fn():
                return "server restarted; reconnect before commanding"
            if command.lease_id not in session.leases:
                return "lease was not issued to this companion session"
            session.leases.pop(command.lease_id)
            return None

    def _dispatch_worker(self, session, generation, request):
        try:
            return self.dispatch_fn(request.action, request.command,
                lambda: self._authorize(session, generation, request.command), self.latest_status)
        finally:
            with self._lock:
                self._command_running = False

    async def _command(self, session, request, future):
        try:
            response = await asyncio.shield(future)
            self._ack(session, ResultMessage(**session.envelope, request_id=request.request_id, response=response))
        except asyncio.CancelledError:
            # Cancelling an await does not stop a vendor worker. Its admission
            # remains occupied until _dispatch_worker actually returns.
            raise
        except Exception:
            logger.exception("companion command failed")
            self._error(session, request.request_id, "command_failed", "command outcome unknown; do not retry automatically")

    async def _stop_request(self, session, request):
        try:
            response = await asyncio.to_thread(self.stop_fn)
            response.command_id = request.command.command_id
            self._ack(session, ResultMessage(**session.envelope, request_id=request.request_id, response=response))
        except Exception:
            logger.exception("companion stop failed")
            self._error(session, request.request_id, "stop_failed", "software stop failed; physical cutoff may be required")

    async def _sender(self, websocket, session):
        while True:
            session.ready.clear()
            with self._lock:
                if session.fatal:
                    raise RuntimeError(session.fatal)
                if session.urgent is not None:
                    message, session.urgent = session.urgent, None
                elif session.acknowledgements:
                    message = session.acknowledgements.popleft()
                elif session.events:
                    message = session.events.popleft()
                else:
                    message, session.status = session.status, None
            if message is None:
                await session.ready.wait()
                continue
            await asyncio.wait_for(websocket.send_text(encode_message(message)), self.send_timeout_s)

    async def _receiver(self, websocket, session):
        deadline = time.monotonic() + self.heartbeat_timeout_s
        command_task = stop_task = None
        try:
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("companion heartbeat expired")
                frame = await asyncio.wait_for(websocket.receive(), remaining)
                if frame["type"] == "websocket.disconnect":
                    return
                try:
                    request = parse_client_message(frame.get("text", frame.get("bytes")))
                except ProtocolError as exc:
                    self._error(session, None, "invalid_message", str(exc))
                    continue
                if request.boot_id != session.boot_id or request.session_id != session.session_id:
                    self._error(session, request.request_id, "wrong_session", "boot or session identity does not match")
                    continue
                if isinstance(request, PingRequest):
                    deadline = time.monotonic()+self.heartbeat_timeout_s
                    self._ack(session, PongMessage(**session.envelope, request_id=request.request_id,
                                                   server_at=time.time()))
                elif isinstance(request, LeaseRequest):
                    with self._lock:
                        generation = session.generation
                    lease = await asyncio.to_thread(self.lease_fn, request.control_epoch,
                        lambda issued: self._register_lease(session, generation, issued))
                    lease.pop("boot_id", None)
                    self._ack(session, LeaseMessage(**session.envelope, request_id=request.request_id, **lease))
                elif isinstance(request, CommandRequest):
                    if request.action == Action.STOP:
                        if stop_task is not None and not stop_task.done():
                            self._error(session, request.request_id, "stop_pending", "stop is already being latched")
                        else:
                            # Invalidate admission before scheduling any worker;
                            # an older queued RESUME can no longer clear STOP.
                            self.invalidate_commands()
                            stop_task = asyncio.create_task(self._stop_request(session, request))
                        continue
                    if request.action == Action.GET_STATUS:
                        status = self.latest_status()
                        response = CommandResponse(ok=status is not None, action=Action.GET_STATUS,
                            command_id=request.command.command_id,
                            detail="latest sampled status" if status is not None else "status sample unavailable", status=status)
                        self._ack(session, ResultMessage(**session.envelope, request_id=request.request_id, response=response))
                        continue
                    with self._lock:
                        busy = self._command_running
                        if not busy:
                            self._command_running = True
                        generation = session.generation
                    if busy:
                        self._error(session, request.request_id, "busy", "motion busy; command not queued")
                    else:
                        # Submit synchronously before creating the async waiter.
                        # Even an immediate disconnect cannot cancel admission
                        # before its worker finally releases the busy flag.
                        try:
                            future = asyncio.get_running_loop().run_in_executor(
                                None, self._dispatch_worker, session, generation, request)
                        except Exception:
                            with self._lock:
                                self._command_running = False
                            raise
                        command_task = asyncio.create_task(self._command(session, request, future))
        finally:
            with self._lock:
                session.live = False
                session.generation += 1
                session.leases.clear()
            # Retain STOP until completion, but never await a vendor gait here.
            if command_task is not None and not command_task.done():
                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
            if stop_task is not None:
                await asyncio.shield(stop_task)

    async def handle(self, websocket):
        session = _Session(self.boot_id_fn(), uuid.uuid4().hex, asyncio.get_running_loop(),
            input_receipts=getattr(websocket, "headers", {}).get(INPUT_RECEIPTS_HEADER) == "1")
        with self._lock:
            owned = self._owner is not None
            if not owned:
                self._owner = session
                self._last_status = None
                self._connections += 1
            else:
                self._rejected_connections += 1
        if owned:
            await websocket.accept()
            try:
                await asyncio.wait_for(websocket.send_text(encode_message(ErrorMessage(**session.envelope,
                    code="owner_exists", reason="another companion session owns control"))), self.send_timeout_s)
            finally:
                await websocket.close(code=1008)
            return
        workers = []
        try:
            self.clear_events_fn()
            await websocket.accept()
            hello = HelloMessage(**session.envelope,
                control_epoch=self.epoch_fn(),
                heartbeat_s=min(1.0, self.heartbeat_timeout_s/3),
                heartbeat_timeout_s=self.heartbeat_timeout_s,
                status_interval_s=self.status_interval_s)
            await asyncio.wait_for(websocket.send_text(encode_message(hello)), self.send_timeout_s)
            self._start_sampler()
            workers = [asyncio.create_task(self._sender(websocket, session)),
                       asyncio.create_task(self._receiver(websocket, session))]
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("companion session ended: %s", str(exc)[:160])
            with self._lock:
                self._last_error = str(exc)[:200] or type(exc).__name__
        finally:
            with self._lock:
                session.live = False
                session.generation += 1
                session.leases.clear()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            # Ownership stays reserved through STOP. An old disconnect cannot
            # stop a replacement owner or let its delayed resume cross STOP.
            try:
                await asyncio.to_thread(self.stop_fn)
            finally:
                with self._lock:
                    if self._owner is session:
                        self._owner = None
                try:
                    await asyncio.wait_for(websocket.close(code=1001), self.send_timeout_s)
                except Exception:
                    pass

    async def close(self):
        self._sampler_stop.set()
        self._sample_wake.set()
        with self._lock:
            session = self._owner
            if session is not None:
                session.fatal = "server shutting down"
                self._notify_locked(session)
        # Sensor drivers are expected to be bounded. Do not block shutdown on a
        # failed external driver or claim the worker has stopped if it has not.
        sampler = self._sampler
        if sampler is not None:
            await asyncio.to_thread(sampler.join, 1.0)
