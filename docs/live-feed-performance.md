# Live dashboard latency

The previous feed combined three avoidable delays: status, perception and user
input were polled serially; every telemetry update created its own HTTP thread;
and the page launched overlapping state polls while consuming a streaming camera.
A slow detector delayed already captured status and typed input. Delayed telemetry
requests could arrive out of order and replace newer state with older state.

The new path gives status, user input and perception independent bounded pollers
with persistent connections. Only status completion publishes a behavior sample;
a detector update cannot refresh old status. Telemetry has one request in flight
and one replaceable pending sample, with publisher/sequence checks on the Pi.
The page receives the latest state through a bounded SSE stream and renders at a
bounded rate. Camera display requests only one latest JPEG at a time, with capture
age shown separately from transport and rendering.

The Pi camera timestamp is set when the encoded JPEG reaches the frame buffer;
the displayed camera age estimates **encoded-frame age**, not exposure-to-screen
latency. Sensor exposure and JPEG encoding time precede that timestamp. Absolute
source-age checks require synchronized Pi, Jetson and browser host wall clocks;
receipt/performance clocks only track additional age after delivery and cannot
prove time spent before receipt. Future or missing timestamps stay unknown.

This changes delivery and visibility, not sensor freshness requirements or safety
limits. Old captures keep their original timestamps. Slow inference can still be
slow; its results no longer hold up unrelated status and engagement input.

Every dashboard publisher carries a fixed `time.time_ns()` start epoch and an
increasing sequence. The Pi rejects older publisher epochs, including a delayed
first request from a process replaced during a restart. Epochs are compared only
with other Jetson publisher epochs; Pi and Jetson clocks need not match for this
ordering check. The Jetson wall clock must remain ordered across brain restarts.
If that clock moves backwards, the Pi reports an older-publisher rejection and
retains the last accepted state until the telemetry store is explicitly reset
or the Pi server is restarted. This is an observability limitation; telemetry
never authorizes robot behavior. Restarting a live Pi service remains a separate
operator action, not an automatic recovery step.

## Reproducible measurements

Baseline: commit `5053600`. Development VM, Python 3.12; these are injected delay
fixtures, not Pi, Wi-Fi, camera or Jetson measurements. The polling fixture uses
5 ms status/event calls and a 500 ms perception call over a 2.2 second window.
Status age below is measured from the fake capture to consumer receipt, excluding
the preceding 5 ms transport fixture. Sample counts are small and demonstrate
blocking behavior rather than a hardware latency distribution.

| Measurement | Baseline | New path |
| --- | ---: | ---: |
| First status delivered | 513 ms | 5.4 ms |
| First user event delivered | 513 ms | 10.5 ms |
| Status age at consumer, p95 | 507 ms | 0.09 ms |
| Status samples within 2.2 seconds | 3 | 11 |
| Concurrent telemetry requests in a 30-update burst | 11 | 1 |
| HTTP requests for that burst | 30 | 2 |
| Last delivered fixture tick after delayed first request | 0 (rolled backward) | 29 (latest) |

For the telemetry burst, the first request takes 600 ms, later requests take 50 ms,
and updates are submitted 5 ms apart. The new publisher replaces 28 pending
samples. Local experience logs retain their full history; the live display needs
the latest visible state. Replacing pending samples cannot accelerate an already
in-flight request, but prevents a stale backlog and rollback.

Run from the repository root:

```bash
brain/.venv/bin/python -m brain.live_feed_benchmark \
  --output docs/evidence/live-feed-benchmark.json
brain/.venv/bin/python tests/browser_live_feed.py --output-dir /tmp/crab-browser
```

The benchmark loads baseline source from local git and replaces HTTP with delay
fixtures. The browser check uses installed Chromium and `websockets`, starts only
an explicit simulation server on an ephemeral loopback port, and installs nothing.
Its JSON report and screenshot separate visible-page evidence from physical-camera
claims. In restricted execution environments these loopback/async checks need
network permission even though they do not contact an external service.

The installed headless Chromium check measured 197 ms from a local telemetry POST
to visible text. During the check it issued 58 camera requests with at most one
request in flight and reported no JavaScript exceptions or horizontal overflow
at 390 px width. It showed stale capture data and stale companion telemetry while
the connection remained live. After an emulated browser outage and feed reopen,
EventSource's retry recovered the newest state in 3.0 seconds. Repeated paints of
one telemetry revision produced exactly one graph sample. This is one local
browser acceptance run, not a LAN percentile. Results and screenshot are in
`docs/evidence/live-dashboard-browser.json` and `live-dashboard-browser.png`.

## Remaining physical acceptance

Measure the installed Pi camera capture cadence and encode time, Pi CPU use with
the dashboard open, LAN delivery/reconnect timing, and Jetson detector latency on
the real devices before selecting a higher frame rate. Use a visible timed target
or matched exposure metadata to measure actual exposure-to-screen delay, and
verify host clock agreement independently. Verify that delayed or
missing data remains visibly stale, typed stop remains prompt during inference,
and camera/telemetry reconnects recover without replaying old commands. No
actuator, driver, OS, power-mode or model-residency changes are part of this work.

## Picamera2 API evidence

Inspected official Raspberry Pi Picamera2 0.3.38, commit
[`5e2a35d3`](https://github.com/raspberrypi/picamera2/blob/5e2a35d3bb230825c98c56d98e01b41ddeb43e86/pyproject.toml#L7),
on 2026-10-22. Its [controls implementation](https://github.com/raspberrypi/picamera2/blob/5e2a35d3bb230825c98c56d98e01b41ddeb43e86/picamera2/controls.py#L8-L18)
converts frame rate to equal `FrameDurationLimits` in microseconds.
[`create_video_configuration`](https://github.com/raspberrypi/picamera2/blob/5e2a35d3bb230825c98c56d98e01b41ddeb43e86/picamera2/picamera2.py#L902-L954)
accepts controls and `queue`; its [configuration path](https://github.com/raspberrypi/picamera2/blob/5e2a35d3bb230825c98c56d98e01b41ddeb43e86/picamera2/picamera2.py#L1267-L1273)
sets retained completed-request queue length to zero for `queue=False`.

Crab requests frame-duration limits only when advertised, retains vendor
buffer-count defaults, and reports requested control separately from unverified
achieved FPS in `/health`. This does not remove encoder, socket or browser queues.
Picamera2 is absent in this development workspace; the deployed library version,
achieved rate and hardware latency remain pending. The encoded-JPEG timestamp
boundary described above is unchanged by these configuration requests.
