# Read-only commissioning diagnostics

Software implementation and host checks: 2026-10-28. This is configuration and
compatibility evidence, not owner acceptance of the Pi, Jetson, storage, motion,
capture, ROS, model performance, or audible sound. The commissioning acceptance
plan remains [the separate gate ledger](plans/software-commissioning-readiness.md).

## Local inventory without contacting devices

From the checkout root, any existing Python 3.10+ interpreter can run the passive
report. Offline reporting needs only the Python standard library:

```bash
python3 -m brain.pet.readiness --help
python3 -m brain.pet.readiness --offline --ros-humble
# With an already-provisioned brain environment and existing crab.env:
bash brain/run.sh check --offline --ros-humble
```

The Python entry point reads the process environment; it does not source or
rewrite `crab.env`. The existing launcher sources that file as before. Neither
entry point installs anything, changes settings/calibration, creates credentials,
reads identity/memory contents, starts services, or downloads models. Do not run
setup scripts merely to obtain an offline inventory; select an existing Python.

`--help` exits before configuration validation or probing. `--offline` performs
zero HTTP, DNS, WebSocket, control, capture, model construction, or playback calls.
It imports no runtime HTTP dependencies, ROS, vendor/GPIO libraries, camera/mic
workers, active `Voice` service, or inference libraries. It reports:

- OS/distribution, kernel release and architecture, **process userspace bitness**,
  userspace ABI, and Python implementation/version, without a hostname or
  interpreter path.
- Git HEAD from one fixed `git rev-parse --verify HEAD` query with a one-second
  timeout, optional locks disabled and sanitized Git environment. A SHA identifies
  the committed base; working-tree changes are explicitly **not inspected**.
  Missing Git/non-checkout yields `unknown`; timeout yields `error`. No stderr,
  repository path, remotes, branch names or user Git configuration is reported.
- Allowlisted environment flags, whether each was explicitly specified, and the
  requested/default boolean value. `--profile interaction|quiet` labels the
  launcher preview; it does not start or inspect an existing companion process.
  Body expressions remain disabled in the default preview. Pi-related flags
  describe this process environment, not the configured or accepted remote Pi.
- Allowlisted module specs and executable lookup results. `available` proves
  only that a module spec or binary was found. No import, ABI load, executable,
  GPU/CUDA initialization, ROS node, or runtime operation is tested.
- Local Piper file metadata only when speech output is requested and
  `PET_VOICE_MODEL` is explicitly configured. Only `.onnx` and `.onnx.json` regular
  file existence is checked; paths and file contents are withheld. Whisper model
  cache contents are not inspected or populated. Camera/perception and model
  readiness remain unknown until separately measured.

The fixed profile distinguishes `disabled`, `unconfigured`, `missing`, `error`,
and `unknown`. Requested flags are separate from usable capabilities. A configured
LLM endpoint is `unknown`, because it has not been contacted. Finding all optional
speech prerequisites still leaves speech output `unknown`, because no synthesis
or playback occurs. A report never acquires a control owner or marks hardware
accepted. Unknown input or hardware state grants no motion authority.

Microphone recording is now **off by default** as a commissioning privacy
default. An absent, empty, whitespace-only, or explicit false `PET_STT` disables
speech input in both companion configuration and the passive report. The
launcher adds `--no-stt` when `PET_STT` is absent or empty. Existing explicit
`PET_STT=1` preferences in `crab.env` or the environment remain an opt-in, subject
to separate owner authorization for real recording. Explicit `--no-stt` still
overrides that preference. No existing preference file is rewritten. Accepted
true/false spellings match runtime configuration and diagnostics; malformed
values produce a named error without exposing the supplied value.

Configuration errors return exit status **1** and name the invalid key without
echoing its value. Invalid active link ports/timeouts, malformed boolean flags,
invalid audio sink and malformed player quoting are errors. No invalid value is
replaced by a weaker limit. Active `brain.config` imports likewise reject invalid
`ROBOT_PORT`/`ROBOT_TIMEOUT_S` with named errors. Legacy navigation settings are
parsed only when their consumer requests the setting; those consumers still
receive an error for an invalid value. Stationary `pet.config` import also leaves
the legacy `PET_REFLEX_CM` → `AGENT_REFLEX_CM` → `WANDER_REFLEX_CM` fallback chain
unevaluated. Explicit legacy consumers still evaluate their requested value and
reject invalid settings; explicit overrides do not evaluate unused fallbacks.
For example:

```bash
WANDER_SPEED=unused-invalid python3 -m brain.pet.readiness --help
WANDER_SPEED=unused-invalid python3 -m brain.pet.readiness --offline
ROBOT_TIMEOUT_S=invalid python3 -m brain.pet.readiness --offline
```

The first two commands succeed; the last exits 1 with a named configuration
error. Exit **0** means the requested metadata/check operations completed without
configuration, transport, or protocol errors. It does not mean optional features
are installed or usable, or that commissioning gates are closed.

## Existing HTTP checks

The existing connected check remains available after the owner has selected the
intended device and authorized read-only endpoint access:

```bash
bash brain/run.sh check --base-url http://picrawler.local:8000 --ros-humble
```

Without `--offline`, readiness independently requests Pi health, status, and
`POST /audio/self-test` with **`{"play": false}`**. Health/status use three-second
HTTP timeouts; the audio diagnostic uses eight seconds. These are transport
timeouts, not claims about hard real-time DNS or server-driver bounds. Automatic
redirects and environment proxies are disabled. This client never requests a
camera or microphone feed, inference, movement, a control lease, STOP/resume,
playback, or a WebSocket owner. Existing server-side health/status collection
remains the Pi's responsibility; offline mode is the way to avoid all device
contact. No connected check was run against owner hardware in this cloud task.

Realtime compatibility uses the exact version/type/path advertisement in HTTP
health. Missing, false, wrong-type, wrong-version, or wrong-path advertisements
fail independently of status/audio results and explain that Pi and brain must be
updated together. Opening a control WebSocket is deliberately excluded: its
disconnect can latch Pi STOP. A successful advertisement check records
`live_connection_tested: false`.

Responses are projected onto fixed diagnostic field names and safe scalar
metadata; arbitrary server messages, device descriptions, endpoint URLs,
identifiers, paths and extra fields are withheld. Exceptions report their class,
not raw text. Local audio diagnostics now use passive dependency/file metadata,
without constructing `Voice`. Audio diagnostics reporting actual playback in
response to `play:false` are an error. A successful process or endpoint response
cannot establish audibility or movement.

The complete health response must pass finite-JSON validation before its
realtime advertisement can enter the report. Nonfinite values fail the health
check with a redacted error, leave compatibility untested, and preserve
independent status/audio results instead of breaking final JSON serialization.

## Platform and owner gates

The optional `--ros-humble` result is a static **Jammy arm64/Python 3.10 binary ABI
comparison**, not ROS installation/runtime acceptance. It requires Ubuntu 22.04,
a 64-bit process with an AArch64/arm64 userspace ABI, an ARM64 kernel, and Python
3.10. Missing inventory yields `unknown`; incompatible facts yield `incompatible`.
No `rclpy` import, ROS graph discovery, node, provisioning, or repair occurs.

The owner's recorded platform facts are Pi Bookworm **ARMHF userspace/Python
3.11** despite its ARM64 kernel, and Jetson Jammy **arm64/Python 3.10**. The Pi
does not meet that Humble binary profile, and no Pi ROS install is needed. The
Jetson's matching ABI does not close its unresolved storage/journal gate. See
[the observation bridge inventory and storage gate](ros-observer.md). This report
always leaves Jetson storage acceptance and physical acceptance open; neither
Git/dependency availability nor reboot success establishes healthy storage. No
installation, storage repair, OS/driver/power change, real recording, calibration,
model training, or deployment is authorized by running these diagnostics.

## Reproduce the software evidence

Run with an existing development interpreter; no owner endpoints or models are
required:

```bash
python3 -m unittest tests.test_commissioning_diagnostics tests.test_realtime_readiness
python3 -m unittest tests.test_mind_memory tests.test_local_perception
```

Fresh-process proofs prohibit sockets/DNS, runtime imports and non-Git child
processes during help/offline execution; inject unused invalid legacy settings
and active configuration errors; and verify secret/path redaction. Other tests
cover the bounded Git query, explicit missing/error/unknown states, metadata-only
asset checks, ARMHF/ARM64/Python ABI distinctions, no active `Voice` construction,
no WebSocket owner, exact HTTP protocol mismatches, and the `play:false` request.
Regressions also cover absent/empty STT settings, preserved explicit saved opt-in
and `--no-stt` override using an isolated launcher argument recorder, lazy legacy
reflex fallbacks in stationary configuration, and nonfinite health advertisements
using a fake client. No launcher test starts the companion or records audio.
These are host software tests. Pi/Jetson physical commissioning remains unrun.
