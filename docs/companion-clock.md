# Companion capture timing — 2026-10-26

The deployed `b505086` client compared Pi capture timestamps directly with the
Jetson wall clock and required a nonnegative age, with no clock-offset allowance.
A Pi about 30 ms ahead therefore disconnected every fresh status sample. The
reported request/response measurements bounded Pi-minus-Jetson offset at roughly
+23.5 to +36.3 ms with about 12 ms RTT; they did not demonstrate a four-second
clock error. NTP synchronization alone does not imply identical wall clocks.

## Bounded measurement

The Pi includes `server_at` in a request-correlated application pong. If a request
leaves at local time L0 and its reply arrives at L1 carrying Pi time R, the
Pi-minus-local offset is bounded by **[R − L1, R − L0]**. Processing, sender
queueing and both network directions remain inside the measured round trip.
Unsolicited status timestamps cannot calibrate the clock. The client waits for
this evidence before publishing status or enabling command dispatch.

Policy lives in `shared/clock.py` and also supplies the dashboard constants:

| Bound | Policy |
| --- | --- |
| Entire measured offset interval | within ±250 ms |
| RTT / interval uncertainty | at most 200 ms |
| Calibration lifetime | 5 seconds; ordinary probes use the existing 1-second heartbeat |
| Wall versus monotonic discrepancy | over 20 ms invalidates the anchor |
| Inter-probe drift allowance | 1000 ppm, conservatively widening both interval ends |
| Status capture freshness | existing 1-second limit, including elapsed age before receipt |
| Typed-event freshness | existing 5-second limit |

The maximum possible current Pi time minus the original capture timestamp is the
conservative upper capture age. A timestamp beyond that interval is unknown;
receipt never substitutes for capture. Original `observed_at`, `captured_at` and
event `timestamp` values remain unchanged. Locally attached clock anchors follow
status health and typed events through the coordinator. Freshness and deadlines
advance using local monotonic elapsed time. These are bounded timing assumptions,
not authenticated time or a claim to detect every sub-uncertainty clock change.

Invalid/missing probes, excessive offset/RTT, inconsistent successive intervals,
clock steps and expired calibrations fail closed. Disconnect discards the anchor,
pending commands and old input. Boot/session/sequence/control-epoch checks, Pi
leases, independent HTTP stop, explicit fresh resume and actuator defaults remain
in force. A mapped stop also fences local events through its receipt time, so
clock uncertainty cannot admit a buffered pre-stop microphone resume. Calibration
itself never sends a motion, resume or sound request.

The browser separately brackets a no-cache `GET /sim/state` about once a second.
It uses `performance.now()` after measurement, rejects duplicate/older SSE server
times, checks Pi boot identity and reacquires timing after feed pause/reconnect.
The Jetson publisher supplies the earliest possible Pi time of telemetry
production; this preserves an upper source age across both machine boundaries.
Input rows use the coordinator's capture ages plus subsequent transport/source
age. Camera display uses its original Pi capture timestamp and encoded-frame age.
Unknown timing remains visible. Telemetry is observability, never motion authority.

## Development validation and limits

`bash test_sim.sh --quick` passed **272 tests**, off-body checks, nine scenarios,
and the isolated HTTP/virtual-body smoke. The final Chromium run passed with no
JavaScript errors, one camera request at a time and stale/reconnect assertions;
its [report](evidence/companion-clock-browser.json) is development-host evidence.
Compileall, shell syntax and diff whitespace checks also passed. No lint or static
type-check configuration exists in this repository.

Virtual-clock/fake-socket regressions cover ±30 ms offsets, excessive skew and
uncertainty, malformed/future timestamps, delayed/replayed/out-of-order captures,
calibration expiry, clock steps, reboot/session reset, typed stop/resume ordering,
and unchanged failed-sensor/provenance gates. Node executes the real dashboard
timing functions, including buffered state, replay, future samples, reboot and
probe-timer lifecycle. Chromium runs against explicit loopback simulation with
a +30 ms browser clock, stale data, offline/reconnect and one camera request at a
time. These are development-host checks, not physical acceptance.

The full `bash test_sim.sh` run passed its unit, off-body, scenario, HTTP and
simulated movement portions, then failed the existing live explicit-resume
assertion. An unchanged `b505086` baseline reproduced the same failure: a resume
from a newer control epoch completes before the next status; that status relatches
the coordinator with `Pi stop generation changed; explicit fresh resume required`.
The clock patch does not change this contract or conceal the failing full check.
Do not treat the clock fix as evidence that every interaction path is accepted.

Ultrasonic no-echo/concurrent-read diagnosis, capture-driver changes, detector
clock handling, audio, and physical safety acceptance remain separate work. A
valid clock does not make unavailable sensors healthy. No device was contacted,
no clock/security settings changed, and no physical motion or sound was tested.

## Operator-controlled review/update procedure

These are instructions for the owner after reviewing the draft and selecting a
specific commit. They were not executed against either device. Keep actuators
disabled and homing off throughout. Do not discard local changes, rewrite
configuration/calibration or reset memory.

1. Stop the current foreground Jetson companion with Ctrl-C and let its software
   stop finish; retain access to the physical cutoff. Stop the foreground Pi
   server with Ctrl-C. If a process is service-managed, use the owner's existing
   service procedure; do not start a second server or companion.
2. In the existing Crab checkout on **each** device, run `git status --short`.
   Resolve/preserve any local edits first. Fetch the proposed branch, inspect the
   diff and select the **same full reviewed commit SHA** on both nodes:

   ```bash
   git fetch origin fix/companion-clock-bounds
   git show --stat FETCH_HEAD
   git switch --detach <full-reviewed-commit-sha>
   git rev-parse HEAD
   ```

   This patch adds no dependency or configuration requirement. Do not run setup,
   driver, clock, security, power, calibration or model installation scripts.
3. On the **Pi**, start the existing server with hardware disabled:

   ```bash
   PICRAWLER_ACTUATORS_ENABLED=0 PICRAWLER_HOME_ON_START=none bash robot/run.sh
   ```

4. On the **Jetson**, run read-only readiness against the owner's Pi address:

   ```bash
   bash brain/run.sh check -- --base-url http://<pi-address>:8000
   ```

   Then use the same typed-only companion mode:

   ```bash
   bash brain/run.sh pet -- --base-url http://<pi-address>:8000 --dashboard --no-voice --no-stt --no-camera --no-llm
   ```

5. Reload `/sim` so the browser gets the new script. Check WebSocket readiness,
   clock-offset interval/RTT and advancing capture ages. Failed sonar/audio must
   remain unavailable. A disconnect still latches stop; reconnect alone must
   never clear it. Given the separately recorded resume-ordering failure, do not
   repeatedly resume or enable motion as a workaround. Stop and report the exact
   failure if timing remains unknown. No hardware acceptance is implied.
