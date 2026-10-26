# Local companion WebSocket transport

The companion uses one local `/companion/ws` connection for Pi status, dashboard
input events, command leases and typed command results. Pi-owned motion guards,
calibration, interruption boundaries and power limits are unchanged. Media
continues over the existing camera/audio HTTP endpoints, and HTTP stop remains
an independent path when the WebSocket is busy or unavailable.

Status is still sampled at 5 Hz. The sampling delay can therefore remain up to
200 ms even with a fast connection. The transport removes repeated status/event
HTTP requests; pushed dashboard input removes the former 10 Hz event polling
wait. Neither change establishes camera exposure-to-display latency or physical
sensor responsiveness.

## Session and failure contract

`shared/realtime.py` defines the versioned, bounded JSON envelopes. The Pi sends
`hello` with a server boot identity, new session identity and heartbeat limits,
then the current status. Status and event messages carry increasing sequence
numbers; request/result messages carry request IDs. Hello, status, events and
lease requests/results also carry the Pi's `control_epoch`. Stop advances this
generation. An event retains the generation in which it was created, so a resume
already in transit before stop cannot acquire a fresh lease afterward. New status
must not upgrade the authority of an old event. Input capture timestamps
remain producer timestamps. Socket receipt never makes an old observation fresh.
Request-correlated pongs now carry Pi time for bounded clock calibration; new
clients fail closed if an older Pi omits it. Update both nodes together. See
[capture timing policy and validation](companion-clock.md).

Only one companion owns a session. A second connection is rejected without
evicting the existing owner. Application pings have a correlated pong; a missing
heartbeat closes the session and latches the existing software stop. A fresh
connection obtains a new session and current status, discards disconnected
dashboard input and never resumes movement automatically.

Mutating requests use a session-bound, one-use Pi lease and the existing
canonical command model. Bounds, clearance, power, stop and command-ID checks
still execute on the Pi. Old-session messages, expired/replayed leases and
unsupported payload fields cannot authorize motion. No servo trajectory crosses
the connection. The HTTP stop route can latch stop while a simulated vendor
boundary is blocked; actual interruption remains limited by vendor/leg boundaries.

The client allows one outstanding command. A sent command without a confirmed
result has an **unknown outcome**. Reconnection does not retry it, clear a stop
or infer that the motion did not occur. Explicit operator recovery remains
necessary. Status uses a replaceable latest slot, events and acknowledgements
have bounded queues, and command contention is rejected instead of queued.
WebSocket frames are limited to 64 KiB, the server transport queue to four frames,
and compression is disabled; the integration server uses the same bounds.

The channel is for a trusted local network. Browser `Origin` headers are rejected
before creating a control session; the dashboard retains its separate read-only
SSE feed. Session/boot IDs prevent stale execution; they are not authentication
or encryption. The existing physical
power cutoff and separate hardware acceptance process remain necessary.

## Reproducible validation

Run from the repository root with existing development dependencies:

```bash
brain/.venv/bin/python -m unittest tests.test_realtime_integration -v
brain/.venv/bin/python -m brain.realtime_benchmark \
  --output docs/evidence/realtime-benchmark.json
```

The integration fixture starts actual uvicorn HTTP/WebSocket listeners on an
ephemeral `127.0.0.1` port. It explicitly enables simulation, disables physical
actuators and startup homing, substitutes labelled battery/ultrasonic samples,
and disables camera/audio. It never reads a configured robot address or personal
memory. Tests restore patched globals and close sockets/threads after each case.
Sandboxed environments may require network permission for loopback sockets.

Checks cover hello/current status and correlated heartbeat, direct event
delivery, rejected browser origins and second owner, session-exclusive event delivery, missing and
replayed leases, duplicate command IDs, expired leases, unknown power, invalid
command fields, HTTP stop during a blocked WebSocket command, saved resume events
that predate stop, explicit fresh resume without movement, heartbeat expiry,
old-session rejection, reconnect without old input replay, and loss of a sent
command result without automatic retry. A blocked vendor call is an explicit
simulation fixture; these checks do not exercise physical servos.

## Measurement method and evidence

The benchmark loads the previous `InputWorker` directly from local git commit
`e5ea52c`, retaining its independent 10 Hz event poller. Both that worker and the
new production `RealtimeClient` connect to the same isolated simulated server,
one at a time. Twenty-four HTTP input events follow an identical deterministic
schedule: first offset 23 ms, then every 137 ms, sweeping across polling phases.
The metric is monotonic time from HTTP event submission start to the companion
input callback. It includes local event ingress and delivery, and excludes
behavior reaction, inference, sound and physical motion.

On 2026-10-22, all **13 actual-socket integration tests passed**. The recorded
Linux x86_64/Python 3.12.14 benchmark produced:

| Event submission → input callback | Previous HTTP polling | WebSocket |
| --- | ---: | ---: |
| Median | 52.965 ms | 2.273 ms |
| p95, 24 samples | 101.256 ms | 3.294 ms |
| Maximum | 101.974 ms | 14.513 ms |
| Delivered, in order | 24/24 | 24/24 |

See [`evidence/realtime-benchmark.json`](evidence/realtime-benchmark.json) for
the complete recorded run and transport health. Combined process RSS and Linux
peak RSS were **50.96 MiB**, with **0.575 CPU seconds over 6.884 elapsed seconds**.
Thread-count snapshots were 6 during polling and 8 during WebSocket delivery;
these include uvicorn, HTTP workers, WebSocket receiver threads and fixture/client
threads in one process. No models were loaded. These figures are development-host
observations, not Jetson 8 GB residency measurements or Pi server capacity
estimates, and do not establish a reduction in memory or thread usage.

Physical acceptance remains pending: measure Pi status capture duration,
heartbeat behavior during actual Wi-Fi loss, Jetson callback/coordination latency,
local CPU/RAM under approved model workloads, and vendor stop boundaries. Camera
encoded-frame age has its own limits in
[live-feed measurements](live-feed-performance.md); it does not measure sensor
exposure or encoder delay. Do not raise capture rates or loosen safety limits
based on a loopback benchmark.
