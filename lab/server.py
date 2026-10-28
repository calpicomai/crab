"""Loopback-only virtual world owner. This module has no robot/ROS clients."""
from __future__ import annotations

from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import threading
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .physics import LabWorld


class Control(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    action: Literal['play','pause','step','reset','mode']
    mode: Literal['stand','crawl','stop'] | None = None
    seed: int = Field(default=1,ge=0,le=2**31-1)
    scenario: Literal['flat','low_friction','sonar_dropout'] = 'flat'
    steps: int = Field(default=10,ge=1,le=100)


class LabRunner:
    """One physics writer, no catch-up burst and one latest display snapshot."""
    def __init__(self):
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._world = LabWorld()
        self._paused = True
        self._error = ''
        self._revision = 0
        self._skipped_wall_s = 0.
        self._factor = 0.
        self._tokens, self._token_at = 10., time.monotonic()
        self._cached = self._world.snapshot()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run,name='crab-lab-physics',daemon=True)
        self._thread.start()

    def _advance(self,steps):
        try:
            self._world.step(steps)
            self._cached = self._world.snapshot()
        except Exception as exc:
            self._error = 'Physics stopped: '+type(exc).__name__+'; showing last valid snapshot'
            self._paused = True
            self._revision += 1
            return False
        self._revision += 1
        return True

    def _run(self):
        period = .01
        while not self._closed.is_set():
            started = time.monotonic()
            with self._lock:
                if not self._paused and not self._error:
                    self._advance(5)
            elapsed = time.monotonic()-started
            with self._lock:
                self._factor = min(1.,period/max(period,elapsed))
                self._skipped_wall_s += max(0.,elapsed-period)
            self._closed.wait(max(0.,period-elapsed))

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def _snapshot(self):
        result = copy.deepcopy(self._cached)
        result['runtime'] = {'running':not self._paused,'error':self._error or None,
            'worker_alive':bool(self._thread and self._thread.is_alive()),
            'revision':self._revision,'snapshot_valid':not bool(self._error),'realtime_factor':self._factor,
            'skipped_wall_s':self._skipped_wall_s,'hardware_route':False}
        return result

    def control(self,value):
        with self._lock:
            now = time.monotonic()
            self._tokens = min(10.,self._tokens+max(0.,now-self._token_at)*10.)
            self._token_at = now
            if self._tokens<1:
                raise HTTPException(429,'Control rate exceeded; wait briefly')
            self._tokens -= 1
            if self._closed.is_set():
                raise HTTPException(503,'Virtual world is closed')
            if value.action=='pause':self._paused=True
            elif value.action=='play':
                if self._error:raise HTTPException(409,'Reset the failed virtual world first')
                self._paused=False
            elif value.action=='step':
                if not self._paused:raise HTTPException(409,'Pause before single stepping')
                if self._error:raise HTTPException(409,'Reset the failed virtual world first')
                if not self._advance(value.steps):raise HTTPException(409,self._error)
            elif value.action=='reset':
                # Build first: a failed model reset cannot partially replace the
                # currently retained world or its last valid display snapshot.
                try:
                    replacement=LabWorld(seed=value.seed,scenario=value.scenario,mode='stand')
                    snapshot=replacement.snapshot()
                except Exception as exc:
                    self._paused=True
                    self._error='Reset failed: '+type(exc).__name__+'; showing last valid snapshot'
                    self._revision+=1
                    raise HTTPException(409,self._error) from None
                self._world=replacement
                self._paused,self._error=True,''
                self._cached=snapshot;self._revision+=1
            elif value.action=='mode':
                if self._error:raise HTTPException(409,'Reset the failed virtual world first')
                if value.mode is None:raise HTTPException(422,'Controller mode is required')
                self._world.set_mode(value.mode)
                self._cached=self._world.snapshot();self._revision+=1
            return self._snapshot()

    def close(self):
        self._closed.set()
        if self._thread is not None:self._thread.join(timeout=1.)


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        runner = LabRunner()
        app.state.runner=runner;runner.start()
        try:yield
        finally:runner.close()

    app=FastAPI(title='Crab Lab — simulation only',lifespan=lifespan,docs_url=None,redoc_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])
    static=Path(__file__).with_name('static')
    app.mount('/static',StaticFiles(directory=static),name='static')

    @app.get('/')
    def index():return FileResponse(static/'index.html',headers={'Cache-Control':'no-store'})

    @app.get('/api/state')
    def state():return app.state.runner.snapshot()

    @app.get('/api/specs')
    def specs():return json.loads(Path(__file__).with_name('specs.json').read_text())

    @app.post('/api/control')
    async def control(request:Request):
        origin=request.headers.get('origin')
        if origin and urlsplit(origin).netloc!=request.headers.get('host'):
            raise HTTPException(403,'Cross-origin virtual-world controls are disabled')
        body=bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>1024:raise HTTPException(413,'Control body exceeds1024bytes')
            body.extend(chunk)
        try:
            value=Control.model_validate(json.loads(body))
        except (ValueError,ValidationError,UnicodeError):
            raise HTTPException(422,'Invalid bounded virtual-world control') from None
        return app.state.runner.control(value)

    return app
