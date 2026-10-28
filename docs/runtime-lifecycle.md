# Runtime ownership and shutdown

The companion and Pi server keep transport, optional features and persistence
under explicit owners. Shutdown is terminal for that process: it invalidates
command authority before abandoning work. Restart never automatically resumes
movement. Native driver interruption and physical STOP latency still require
separate supervised acceptance.

## Companion

Construction registers cleanup immediately. Partial startup, SIGINT and SIGTERM
unwind remaining owners even when one cleanup fails. Optional voice cancellation
cannot suppress STOP. Memory reads, writes and JSON logs use one disk worker;
the coordinator reads caches and submits bounded work. The record queue holds
64 items and the log slot keeps only the newest snapshot. Overflow, stalled
operations and failed persistence are visible in telemetry. Pending data may be
lost if a blocked worker cannot finish at exit.

A monotonic progress guard checks every 50 ms. After one second without main
loop progress it permanently fences the realtime client, stops heartbeats and
reconnection, and independently attempts fresh HTTP STOP. Its acknowledgement
budget is 1.25 seconds; unresolved results remain unknown. A failed fence cannot
suppress that STOP attempt. This fault requires a process restart, followed by
fresh health and an explicit Resume. A heartbeat alone cannot keep a stalled
coordinator authorized.

Control work is single-flight with no queued replay. Terminal fencing precedes
bounded abandonment. A late cancelled Resume still attempts STOP cleanup. Python
cannot interrupt an arbitrary native call; abandoning its daemon owner is not
evidence of physical completion or cleanup. Persistence startup has a two-second
budget and close waits up to half a second.

## Pi server

The owned Uvicorn server begins application shutdown before draining SSE and
WebSocket connections. It fences admissions, latches software STOP, invalidates
leases and stop generations, and closes observation streams. An older Resume
cannot clear the new fence. A delayed audio self-test rechecks its original
owner at playback admission, so a replacement lifespan cannot authorize it.

Blocking requests use 32 bounded worker slots. Audio cancellation and motion
STOP completion have separate eight-slot lanes. HTTP/WebSocket STOP and owner
disconnect latch the software transition before any fallible worker admission;
exhausted cleanup capacity returns an explicit latch-only result without status
or an audio-completion claim. Saturation rejects other work instead of queueing
it; WebSocket rejection does not consume its lease or leave the hub busy.
Shutdown cleanup attempts every
independent owner concurrently with a one-second cleanup wait and a three-second
software shutdown budget. Pending driver, camera or amplifier cleanup is logged
as incomplete. These budgets bound software waits, not hardware power removal.

## Reproducible checks

```bash
bash test_sim.sh
brain/.venv/bin/python -m unittest discover -s tests -p 'test_companion_lifecycle.py'
robot/.venv/bin/python -m unittest discover -s tests -p 'test_pi_shutdown.py'
brain/.venv/bin/python tests/browser_dashboard_outcomes.py --output-dir /tmp/crab-outcomes
```

The process tests use fakes and owned loopback simulation, including blocked
commands, disk stalls, open streams, capacity rejection and late shutdown races.
They do not operate a physical robot. Cloud validation on 2026-10-28 passed the
unit suite, full simulator smoke and the actual Chromium outcome/reconnect test.
Additional saturation regressions cover blocked audio cancellation, full STOP
completion capacity, intervening Resume, WebSocket disconnect and failed
worker-thread startup.
Vendor interruption, physical STOP distance, amplifier cleanup, storage health
and device service signal delivery remain commissioning gates.
