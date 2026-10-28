"""Bounded runtime ownership and a progress fence independent of WebSocket health.

Python cannot interrupt arbitrary native code or an in-flight physical vendor
boundary. Owners use finite transport deadlines; unresolved results stay unknown.
"""
from __future__ import annotations

from concurrent.futures import Future
import signal
import threading
import time


class CleanupStack:
    """Register every successful construction immediately; isolate all cleanup."""
    def __init__(self):
        self._owners = []
        self.errors = []

    def own(self, owner, *, close=None, priority=0):
        self._owners.append((priority, len(self._owners), close or owner.close))
        return owner

    def close(self):
        owners, self._owners = self._owners, []
        for _, _, close in sorted(owners, key=lambda item: item[:2], reverse=True):
            try:
                close()
            except BaseException as exc:
                self.errors.append(f"{getattr(close, '__qualname__', 'cleanup')}: {type(exc).__name__}: {str(exc)[:200]}")


class ShutdownRequested(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum


class ShutdownSignals:
    """SIGTERM unwinds normal ownership; restore handlers for embedded callers."""
    def __init__(self):
        self.previous = {}

    def install(self):
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                self.previous[signum] = signal.signal(signum, self._handle)
        return self

    @staticmethod
    def _handle(signum, frame):
        raise ShutdownRequested(signum)

    def close(self):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)
        self.previous.clear()


class SingleFlightOwner:
    """One bounded task, no queue and no interpreter-global executor exit join.

    The caller MUST fence transport before abandonment. Daemon lifetime is a last
    resort for broken native/fake transports, never evidence of command completion.
"""
    def __init__(self, name):
        self.name = name
        self._thread = None
        self._future = None
        self._closed = False

    def submit(self, function, *args):
        if self._closed or (self._future is not None and not self._future.done()):
            raise RuntimeError(f"{self.name} closed or already outstanding")
        future = Future()
        self._future = future

        def run():
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(function(*args))
                except BaseException as exc:
                    future.set_exception(exc)

        self._thread = threading.Thread(target=run, name=self.name, daemon=True)
        try:
            self._thread.start()
        except BaseException as exc:
            if not future.running() and not future.done():
                future.set_exception(exc)
            raise
        return future

    def shutdown(self, wait=False, cancel_futures=False):
        self._closed = True
        if wait:
            raise ValueError("owners must use an explicit finite join deadline")

    def join(self, timeout):
        if self._thread and self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, timeout))


class FaultLatch:
    """Terminal per-process fault; no implicit or explicit in-process rearm."""
    def __init__(self):
        self.reason = None
        self._lock = threading.Lock()

    def trip(self, reason):
        with self._lock:
            if self.reason is not None:
                return False
            self.reason = str(reason)[:240]
            return True


class ProgressGuard:
    """Main-loop progress, not WS heartbeats, authorizes a healthy runtime.

    Default: 1s stale threshold, 50ms inspection, 1.25s STOP acknowledgement budget.
    One STOP attempt uses the independent fresh-HTTP route. Timeouts remain unknown;
    this watchdog cannot replace Pi stop boundaries or a physical power cutoff.
"""
    def __init__(self, fence, emergency_stop, *, fault=None, timeout_s=1.0,
                 check_s=.05, stop_timeout_s=1.25, clock=time.monotonic):
        for value in (timeout_s, check_s, stop_timeout_s):
            if not isinstance(value, (int, float)) or not 0 < value < float("inf"):
                raise ValueError("progress guard deadlines must be positive and finite")
        self.fault = fault or FaultLatch()
        self.fence, self.emergency_stop = fence, emergency_stop
        self.timeout_s, self.check_s, self.stop_timeout_s = timeout_s, check_s, stop_timeout_s
        self.clock = clock
        self._progress = clock()
        self._closed = threading.Event()
        self._thread = None
        self._stop_done = threading.Event()
        self._stop_thread = None
        self._outcome = {"state": "not_requested"}
        self._fence_error = None

    def start(self):
        if self._thread is None:
            self._progress = self.clock()
            self._thread = threading.Thread(target=self._run, name="companion-progress", daemon=True)
            self._thread.start()

    def checkpoint(self):
        if self.fault.reason is None:
            self._progress = self.clock()

    def _send_stop(self):
        try:
            result = self.emergency_stop()
            result = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
            valid = result.get("ok") is True and result.get("action") == "stop"
            self._outcome = {"state": "acknowledged" if valid else "unknown", "response": result}
        except Exception as exc:
            self._outcome = {"state": "unknown", "reason": str(exc)[:240]}
        finally:
            self._stop_done.set()

    def _run(self):
        while not self._closed.wait(self.check_s):
            if self.clock()-self._progress <= self.timeout_s:
                continue
            if not self.fault.trip("companion main progress expired; restart required"):
                return
            # Independent failures cannot suppress the other safety action.
            try:
                self.fence(self.fault.reason)
            except Exception as exc:
                self._fence_error = str(exc)[:240]
            self._outcome = {"state": "pending"}
            try:
                self._stop_thread = threading.Thread(target=self._send_stop,
                    name="companion-progress-stop", daemon=True)
                self._stop_thread.start()
            except Exception as exc:
                self._outcome = {"state": "unknown", "reason": str(exc)[:240]}
                return
            if not self._stop_done.wait(self.stop_timeout_s):
                self._outcome = {"state": "unknown", "reason": "STOP acknowledgement deadline exceeded; physical state unknown"}
            return

    def health(self):
        return {"fault": self.fault.reason, "requires_restart": self.fault.reason is not None,
                "progress_age_s": max(0.0, self.clock()-self._progress),
                "timeout_s": self.timeout_s, "fence_error": self._fence_error,
                "stop": dict(self._outcome)}

    def close(self):
        self._closed.set()
        if self._thread and self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join(self.check_s+self.stop_timeout_s+.1)
