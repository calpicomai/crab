"""Single disk owner. Behavior submits bounded work and reads immutable caches.

Disk stalls can lose queued data at process exit; they cannot stall a tick.
Overflow is explicit telemetry, never an unbounded backlog or synchronous retry.
Defaults: 64 records, one latest log snapshot, 2s startup and .5s close budgets.
"""
from __future__ import annotations

import copy
import json
import queue
import threading
import time

from .memory import MemoryStore


class PersistenceWorker:
    def __init__(self, memory_db, log_path=None, *, capacity=64, store_factory=MemoryStore,
                 open_log=open):
        if type(capacity) is not int or not 1 <= capacity <= 1024:
            raise ValueError("persistence capacity must be between 1 and 1024")
        self.memory_db, self.log_path = memory_db, log_path
        self._store_factory, self._open_log = store_factory, open_log
        self._records = queue.Queue(maxsize=capacity)
        self._logs = queue.Queue(maxsize=1)
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._summary = "(memory loading)"
        self._continuity = {}
        self._failure = None
        self._startup_error = None
        self._dropped = self._logs_replaced = self._written = 0
        self._operation = None

    def start(self, timeout=2.0):
        self._thread = threading.Thread(target=self._run, name="companion-persistence", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("persistence startup exceeded its budget; no companion transport started")
        if self._startup_error is not None:
            raise RuntimeError("persistence startup failed: " + self._startup_error)

    def remember(self, **record):
        if self._closed.is_set():
            return False
        try:
            self._records.put_nowait(copy.deepcopy(record))
        except queue.Full:
            self._dropped += 1
            return False
        self._wake.set()
        return True

    def log(self, state):
        if self.log_path is None or self._closed.is_set():
            return
        payload = copy.deepcopy(state)
        try:
            self._logs.put_nowait(payload)
        except queue.Full:
            try:
                self._logs.get_nowait()
                self._logs_replaced += 1
            except queue.Empty:
                pass
            self._logs.put_nowait(payload)
        self._wake.set()

    def summary(self):
        return self._summary

    def interaction_history(self):
        return dict(self._continuity)

    def health(self):
        operation = self._operation
        return {"closed": self._closed.is_set(), "pending_records": self._records.qsize(),
                "pending_log": not self._logs.empty(), "records_written": self._written,
                "records_dropped": self._dropped, "logs_replaced": self._logs_replaced,
                "failure_reason": self._failure, "worker_alive": bool(self._thread and self._thread.is_alive()),
                "active_operation": operation[0] if operation else None,
                "operation_age_s": max(0.0, time.monotonic()-operation[1]) if operation else None}

    def _run(self):
        memory = log = None
        try:
            self._operation = ("initializing persistence", time.monotonic())
            memory = self._store_factory(self.memory_db)
            if self.log_path:
                log = self._open_log(self.log_path, "a", encoding="utf-8")
            self._continuity = memory.interaction_history()
            self._summary = memory.summary()
            self._operation = None
            self._ready.set()
            while not self._closed.is_set() or not self._records.empty() or not self._logs.empty():
                self._wake.clear()
                try:
                    record = self._records.get_nowait()
                except queue.Empty:
                    record = None
                if record is not None:
                    self._operation = ("memory write/cache", time.monotonic())
                    try:
                        memory.remember(**record)
                        self._written += 1
                        self._summary = memory.summary()
                    except Exception as exc:
                        self._failure = f"memory write/cache failed: {str(exc)[:200]}"
                    finally:
                        self._operation = None
                try:
                    state = self._logs.get_nowait()
                except queue.Empty:
                    state = None
                if state is not None and log is not None:
                    self._operation = ("log write/flush", time.monotonic())
                    try:
                        log.write(json.dumps(state)+"\n")
                        log.flush()
                    except Exception as exc:
                        self._failure = f"log write/flush failed: {str(exc)[:200]}"
                    finally:
                        self._operation = None
                if record is None and state is None:
                    self._wake.wait(.1)
        except BaseException as exc:
            self._failure = self._startup_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._ready.set()
        finally:
            # Each cleanup runs even if another fails. Only this thread ever
            # touches the store/file, including after a bounded close returns.
            for owner in (log, memory):
                if owner is not None:
                    self._operation = ("closing persistence", time.monotonic())
                    try:
                        owner.close()
                    except BaseException as exc:
                        self._failure = f"persistence cleanup failed: {str(exc)[:200]}"
            self._operation = None

    def close(self, timeout=.5):
        self._closed.set()
        self._wake.set()
        if self._thread and self._thread.ident is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
