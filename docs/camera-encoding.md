# Explicit camera encoder profile — 2026-10-26

The existing path already compresses individual frames as JPEG. Its Picamera2
`JpegEncoder` calls native `simplejpeg` through software worker threads. Crab's
launcher/server is Python/FastAPI; there is no Rust camera path in this repo.
The owner's OV5647 test at 960×720 / quality 90 looked clearer but produced lower
preview FPS and more delay. That observation does not identify whether encoding,
payload transfer, request scheduling or browser decoding dominates on the devices.

This optional profile selects Picamera2's direct **V4L2 hardware MJPEG**
implementation rather than its public `MJPEGEncoder` name, which can alias a
software encoder on unsupported platforms. It requires the vendor's affirmative
hardware-capability signal, the direct module and V4L2 base class. Missing APIs,
incompatible versions, invalid configuration or recording errors report capture
unavailable. There is no automatic software fallback. Installed Picamera2 version
and the physical encoder remain unverified in this cloud environment.

`PICRAWLER_CAMERA_ENCODER=jpeg` remains the default and retains the existing
quality API and pixel-format selection. Explicit `mjpeg_hw` selects YUV420, even
dimensions up to 1920×1080, requested 1–30 FPS, and bitrate 1–12 Mbps (default
4 Mbps). Its bitrate is **not equivalent** to JPEG quality 80/90. Quality is not
applied in that mode and this is reported in capture settings. Bitrate is a target,
not a measured network cap. Simulation uses its explicit Pillow path and never
imports or starts hardware through this profile. Disabled camera starts neither.

The existing single capture, newest encoded-frame buffer, endpoints, timestamps,
stop/freshness controls, motion defaults and sensor behavior remain unchanged.
Frames retain encoded-output timestamps, which exclude exposure/encoding delay.
Hardware encoding is an alternative to benchmark, not a demonstrated speedup or
an optical fix. There is no autofocus, microphone, global mixer, driver install,
host setting or actuator change.

## Owner-controlled evaluation after review

These commands are documentation only; none ran against a device here. Keep the
robot stationary with actuators disabled, no startup homing and audio disabled.
Use the same lighting, scene and preview clients for every comparison. Stop the
existing Pi server with Ctrl-C before a temporary profile launch; preserve saved
configuration. Do not run two camera servers or an extra capture program.

First inspect installed APIs without constructing a camera or encoder:

```bash
robot/.venv/bin/python - <<'PY'
import importlib, importlib.metadata, inspect, json
result = {}
try:
    result['picamera2_version'] = importlib.metadata.version('picamera2')
    package = importlib.import_module('picamera2.encoders')
    result['hardware_advertised'] = getattr(package, '_hw_encoder_available', None) is True
    module = importlib.import_module('picamera2.encoders.mjpeg_encoder')
    result['direct_class'] = module.MJPEGEncoder.__module__
    result['constructor'] = str(inspect.signature(module.MJPEGEncoder))
except Exception as exc:
    result['unavailable_reason'] = str(exc)
print(json.dumps(result, indent=2))
PY
```

For a **quality-only comparison**, hold 960×720 and requested 15 FPS fixed:

```bash
PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_HOME_ON_START=none \
PICRAWLER_AUDIO_ENABLED=0 PICRAWLER_CAMERA_ENCODER=jpeg \
PICRAWLER_CAMERA_WIDTH=960 PICRAWLER_CAMERA_HEIGHT=720 \
PICRAWLER_CAMERA_FPS=15 PICRAWLER_CAMERA_QUALITY=80 bash robot/run.sh
```

Compare with the prior quality-90 evidence and the same workload before changing
transport. The new dashboard branch separately instruments capture/encoded versus
received/decoded FPS and header/body/decode/queue timings; its read-only preview
probe can compare Pi-local and Jetson requests to the same existing endpoint.
Do not infer encoder time from the encoded-output frame-age value alone.

For a **separate explicit hardware-profile comparison**, if the capability check
supports it, stop the software-profile server and launch:

```bash
PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_HOME_ON_START=none \
PICRAWLER_AUDIO_ENABLED=0 PICRAWLER_CAMERA_ENCODER=mjpeg_hw \
PICRAWLER_CAMERA_MJPEG_BITRATE=4000000 \
PICRAWLER_CAMERA_WIDTH=960 PICRAWLER_CAMERA_HEIGHT=720 \
PICRAWLER_CAMERA_FPS=15 bash robot/run.sh
```

Keep the Jetson typed-only and model/capture workload identical:

```bash
bash brain/run.sh pet -- --base-url http://10.1.20.170:8000 \
  --dashboard --no-voice --no-stt --no-camera --no-llm
```

Measure actual clarity, encoded bytes/cadence, request/body/decode delays and CPU
before accepting a profile. If unavailable or worse, stop it and return to the
software-profile command. All overrides are per-process environment values;
no saved configuration or global camera setting is rewritten.

## Development evidence and limits

Focused tests use fake vendor modules/drivers, including explicit capability,
software-alias rejection, bitrate/dimension bounds, no default probe, unavailable
APIs, recording failure cleanup, no-frame health and explicit simulation. Existing
latest-frame/timestamp tests are retained. The final quick suite passed 284 tests, off-body checks, nine scenarios and
HTTP/virtual-body checks. The preceding full run passed 283 tests and live
WebSocket greeting/priority stop/explicit resume before the last reporting-only
regression test/fix. An initial full run caught a default-start error-order
regression; it was fixed and that run was not treated as acceptance. Compileall,
shell syntax and diff checks passed. No physical performance or encoder
acceptance was tested in cloud.

Primary references: [software JpegEncoder](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/jpeg_encoder.py),
[platform-dependent aliases](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/__init__.py),
[direct MJPEGEncoder](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/mjpeg_encoder.py),
[V4L2 implementation](https://github.com/raspberrypi/picamera2/blob/main/picamera2/encoders/v4l2_encoder.py).
