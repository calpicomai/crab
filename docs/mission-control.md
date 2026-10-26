# Local operations dashboard — 2026-10-26

Open the Pi's existing `/sim` page. The name is retained for compatibility; the
physical robot shows camera, companion state and vitals. A world map appears only
when the explicit simulator actually supplies one. No frontend server, framework,
external fonts, model, extra capture consumer or observability service is added.

The camera is displayed without automatic enlargement. Native 1:1 permits
scrolling larger frames; Expand keeps STOP above the view. Typed interaction,
current intent and the bounded result timeline explain what was requested and
what the backend reported. “Handled” does not establish movement or audible sound.
Availability, current use and verification are separate. Missing sonar echoes
remain unavailable; saved character text is labelled generated/saved narrative.

STOP uses the existing independent Pi stop endpoint, followed by the existing
companion stop input. It remains visible during failures. Ordinary input cannot
overwrite a pending stop result. Resume requires a deliberate second click and
fresh connected companion status; it does not enable actuators or start movement.
Backend stop, freshness, lease and session protections are unchanged.

## Vitals and freshness

Eight rolling charts show battery volts, sonar centimetres, capture/encoded/preview
FPS, encoded-frame age/request RTT, Pi–Jetson RTT/clock interval width, and Pi/Jetson
CPU, memory and CPU temperature. The browser retains at most 121 one-second points
over two minutes. Unknown/stale samples are gaps, never invented zeros. New boot
IDs break lines. Server-sourced camera FPS becomes unknown if state delivery is
stale even when independent preview requests still work.

Host sampling is cached at one second and reads bounded Linux `/proc` files and
up to 16 CPU-labelled thermal zones without root or settings changes. Whole-host
CPU needs two counter samples. Memory is total minus `MemAvailable`, in MiB;
process RSS and one-core process CPU are in diagnostics. No readable CPU zone
means unknown temperature. Sources are explicitly Pi and Jetson, not GPU or motor
telemetry. Collection stays off the coordinator and ASGI event loop.

## Camera timing and clarity

Default capture remains 640×480, requested 15 FPS, JPEG quality 80. An owner's
temporary 960×720 quality-90 test was clearer but measured slower on their devices;
this dashboard does not silently change capture settings. The OV5647 has no
verified autofocus integration here. Resolution cannot repair an unverified lens
or optical problem.

The current launch path is Python/FastAPI and Picamera2 `JpegEncoder`, which uses
native `simplejpeg` compression. There is no Rust camera/server path in this
repository. JPEG was already compressed. Increasing pixel count and quality can
increase both encoder work and payload size; the physical dominant cost is still
unmeasured. Picamera2 also has a hardware MJPEG encoder on supported platforms,
but its public name can alias a software encoder elsewhere. Importing that name
alone would not establish acceleration. This PR does not claim hardware encoding.

The preview now overlaps one bounded HTTP fetch with one offscreen decode, keeping
only one replaceable pending JPEG. It caps received JPEGs at 4 MiB and 2,048 chunks,
requests at approximately 10 Hz, request time at 1.5 seconds and decode at one
second. Sequence and timestamp ordering reject replayed/older preview samples.
Hidden tabs, boot changes and reconnects fence old work. No historical frame queue
is added and the server still supplies its atomic newest encoded frame.

The display distinguishes:

- **Capture cadence:** existing request metadata `SensorTimestamp` intervals,
  when available, observed without requesting another frame.
- **Encoded cadence:** arrivals at the existing JPEG output; this can lag capture.
- **Received / preview decoded:** browser arrivals and decoded image commits,
  rather than guaranteed monitor presentation FPS.
- **Encoded age:** a conservative clock-bounded upper age plus transfer/queue/decode
  delay. The original timestamp is encoded-output arrival, so this excludes sensor
  exposure and encoding time and is not sensor-to-screen latency.
- **Headers wait, body transfer, JPEG decode and decode-slot wait:** separate
  browser durations, with payload KiB and replaced-before-decode count.

The controlled scheduler benchmark with synthetic 50 ms fetch and 120 ms decode
measured 23 decoded frames / 5.75 FPS before and 32 / 8.00 FPS after over four
seconds per path. Peak HTTP concurrency was one in both; six pending frames were
replaced in the new path. This demonstrates scheduling overlap only, not actual
JPEG decode, Pi encoding, LAN throughput or physical camera improvement.

For a **user-controlled read-only measurement** after reviewing this code, from
the repo root on Pi and Jetson with the same existing server/capture profile:

```bash
python3 -m brain.preview_probe --base-url http://10.1.20.170:8000 --samples 15
```

It GETs the existing endpoint, discards image bytes, uses one persistent HTTP
connection, and reports each matching sequence/stamp/byte count and header/body
timing. It starts no capture, actuator, microphone or playback and changes no
settings. Pi versus Jetson isolates additional transport/client cost, not pure
encoder latency. First-request connection setup and server wait are included in
headers time. Comparing quality 90 versus 80 at the **same 960×720** requires a
separate owner-controlled restart; resolution, FPS and client load must stay the
same to interpret that comparison. No such device comparison ran in cloud.

Primary encoder references: [JpegEncoder](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/jpeg_encoder.py),
[platform aliases](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/__init__.py),
[hardware MJPEGEncoder](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/mjpeg_encoder.py).

## Reproducible development checks

```bash
bash test_sim.sh --quick
brain/.venv/bin/python tests/browser_live_feed.py --output-dir /tmp/crab-live
brain/.venv/bin/python tests/browser_mission_control.py --output-dir /tmp/crab-mission
PYTHONPATH=. brain/.venv/bin/python tests/benchmark_preview.py
brain/.venv/bin/python -m compileall -q brain robot shared tests
git diff --check
```

The new Chromium fixture covers synthetic real/sim labels, disabled audio/motion,
desktop and 390px mobile, native scaling, expanded STOP, deliberate resume, slow
decode/newest-frame replacement, stop transport failure, stale/disconnected state
and JavaScript errors. Screenshots explicitly label synthetic data; their camera
pattern is generated, not a physical camera photograph. The readiness wait was
fixed after one test-harness navigation race. A later run passed all assertions.

Final focused suite: 287 tests and off-body/nine scenarios passed. The preceding
quick run passed 285 tests plus HTTP/virtual body. A full run at the earlier
283-test revision passed those portions and failed the already reproduced baseline
explicit-resume/status-generation race. The dashboard does not weaken the stop
latch to make that pass. Exact-head CI is reported with the draft PR.

[Desktop](evidence/mission-control/real-connected-desktop.png),
[vitals](evidence/mission-control/vitals-desktop.png),
[mobile](evidence/mission-control/real-connected-mobile.png),
[failure](evidence/mission-control/failure-mobile.png),
[disconnected](evidence/mission-control/disconnected-desktop.png),
[simulation](evidence/mission-control/simulation-desktop.png).

[Browser state report](evidence/mission-control-browser.json),
[live-feed report](evidence/mission-control-live-feed.json),
[scheduler benchmark](evidence/preview-scheduling.json).
No physical camera, host vitals, network performance, audio, motion or model
acceptance is claimed.
