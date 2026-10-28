"""Bounded software ownership at the ASGI boundary; no hardware interruption claim."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import threading
import time

from fastapi import HTTPException
from fastapi.routing import APIRoute
import uvicorn

logger = logging.getLogger("picrawler.lifecycle")
SHUTDOWN_TIMEOUT_S = 3.0
CLEANUP_TIMEOUT_S = 1.0


class WorkerCapacityError(RuntimeError):
    pass


class DaemonWorkers:
    """Submit immediately or reject; abandoned native calls cannot hold up exit.

    Cancelling the future does not interrupt its thread. Callers must fence
    authority before abandoning work. Capacity stays occupied until it returns.
    """
    def __init__(self, capacity=32):
        self._slots = threading.BoundedSemaphore(capacity)

    def submit(self, function, *args, **kwargs):
        loop = asyncio.get_running_loop()
        if not self._slots.acquire(blocking=False):
            raise WorkerCapacityError("blocking worker capacity reached; work not queued")
        future = loop.create_future()
        context = contextvars.copy_context()

        def finish(result=None, error=None):
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(result)

        def run():
            try:
                result = context.run(function, *args, **kwargs)
            except BaseException as exc:
                callback = functools.partial(finish, error=exc)
            else:
                callback = functools.partial(finish, result=result)
            finally:
                self._slots.release()
            try:
                loop.call_soon_threadsafe(callback)
            except RuntimeError:
                pass  # The owner has closed; a late result grants no authority.

        try:
            threading.Thread(target=run, name="crab-io", daemon=True).start()
        except BaseException:
            self._slots.release()
            future.cancel()
            raise
        return future


request_workers = DaemonWorkers()
stop_workers = DaemonWorkers(capacity=8)
audio_stop_workers = DaemonWorkers(capacity=8)


class OwnedRoute(APIRoute):
    """Keep sync request work out of ASGI's non-daemon default worker pool."""
    def __init__(self, path, endpoint, **kwargs):
        if not inspect.iscoroutinefunction(endpoint):
            original = endpoint
            workers = (audio_stop_workers if original.__name__ == "audio_stop" else
                       stop_workers if original.__name__ == "stop" else request_workers)

            @functools.wraps(original)
            async def endpoint(*args, **kwargs):
                try:
                    return await workers.submit(original, *args, **kwargs)
                except WorkerCapacityError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc

            endpoint.__signature__ = inspect.signature(original, eval_str=True)
        super().__init__(path, endpoint, **kwargs)


class Shutdown:
    """One terminal fence, one cleanup task and one monotonic shutdown budget."""
    def __init__(self, latch, cleanup, boot_id):
        self.closing = threading.Event()
        self.started_at = self.finished_at = None
        self._loop = asyncio.get_running_loop()
        self._latch, self._cleanup = latch, cleanup
        self.boot_id = boot_id
        self._task = None
        self.ready = asyncio.Event()

    def request(self):
        # Safe in Python's signal handler: never acquire a control/device lock.
        if not self.closing.is_set():
            self.started_at = time.monotonic()
            self.closing.set()
            try:
                self._loop.call_soon_threadsafe(self._start)
            except RuntimeError:
                pass  # Library lifespans may close from a later owned loop.

    def _start(self):
        if self._task is None:
            self.ready.set()
            try:
                self._latch()
            except Exception:
                logger.exception("software STOP/fence failed; still attempting every cleanup owner")
            self._task = self._loop.create_task(self._finish())

    async def _finish(self):
        try:
            await self._cleanup()
        finally:
            self.finished_at = time.monotonic()

    async def close(self):
        if self._loop.is_closed() and self._task is None:
            self._loop = asyncio.get_running_loop()
        self.request()
        self._start()
        await asyncio.shield(self._task)

    def remaining(self):
        return max(0.0, SHUTDOWN_TIMEOUT_S-(time.monotonic()-self.started_at))


async def cleanup_owners(operations):
    """Start every independent cleanup even if another raises or never returns."""
    workers = DaemonWorkers(capacity=len(operations))

    async def clean(name, operation, asynchronous):
        try:
            result = await operation() if asynchronous else await workers.submit(operation)
            if isinstance(result, dict) and not result.get("ok", True):
                logger.error("%s shutdown incomplete: %s", name, result.get("reason"))
        except Exception:
            logger.exception("%s shutdown failed", name)

    tasks = {asyncio.create_task(clean(*item)): item[0] for item in operations}
    _, pending = await asyncio.wait(tasks, timeout=CLEANUP_TIMEOUT_S)
    for task in pending:
        logger.error("%s cleanup still pending at software shutdown deadline", tasks[task])
        task.cancel()
    # These wrappers only await cancellable futures / our bounded async owners.
    await asyncio.gather(*tasks, return_exceptions=True)


class OwnedServer(uvicorn.Server):
    """Begin application shutdown before Uvicorn waits for open connections."""
    def __init__(self, config, shutdown_owner):
        super().__init__(config)
        self._shutdown_owner = shutdown_owner

    def handle_exit(self, sig, frame):
        owner = self._shutdown_owner()
        if owner is not None:
            owner.request()
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets=None):
        owner = self._shutdown_owner()
        if owner is not None:
            await owner.close()
            self.config.timeout_graceful_shutdown = owner.remaining()
        await super().shutdown(sockets=sockets)
