"""Pure typed diagnostics and an explicitly started, read-only HTTP observer.

No ROS import, command endpoint, hardware consumer or implicit configuration.
All numeric placeholders have validity flags. Clock anchors reported by another
host are metadata: only the bridge's own correlated Pi anchor can age delivery.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import hashlib
import json
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from shared.clock import CaptureClock, capture_age, finite_number, MAX_ANCHOR_AGE_S, DRIFT_S_PER_S
from shared.local import require_local_url

MAX_RESPONSE_BYTES = 256 * 1024
MAX_RESPONSE_CHUNKS = 4096
MAX_INPUTS = 32
MAX_UINT64 = 2**64 - 1


@dataclass(frozen=True)
class ObservationContext:
    schema_version: int = 1
    producer: str = ""
    robot_boot_id: str = ""
    session_id_valid: bool = False
    session_id: str = ""
    publisher_id_valid: bool = False
    publisher_id: str = ""
    publisher_started_at_ns_valid: bool = False
    publisher_started_at_ns: int = 0
    sequence_valid: bool = False
    sequence: int = 0
    captured_at_valid: bool = False
    captured_at: float = 0.
    captured_clock_domain: str = ""
    producer_received_at_valid: bool = False
    producer_received_at: float = 0.
    producer_received_clock_domain: str = ""
    bridge_received_at: float = 0.
    bridge_received_mono: float = 0.
    age_evaluated_mono: float = 0.
    calibration_valid_until_mono: float = 0.
    offset_bounds_valid: bool = False
    offset_lower_s: float = 0.
    offset_upper_s: float = 0.
    age_upper_valid: bool = False
    age_upper_s: float = 0.
    timing_verified: bool = False
    fresh: bool = False
    reported_clock_valid: bool = False
    reported_offset_lower_s: float = 0.
    reported_offset_upper_s: float = 0.
    reported_wall_at: float = 0.
    reported_mono_at: float = 0.
    reported_produced_at_pi_lower_valid: bool = False
    reported_produced_at_pi_lower: float = 0.


@dataclass(frozen=True)
class InputObservation:
    context: ObservationContext
    name: str = ""
    source_valid: bool = False
    source: str = ""
    available_valid: bool = False
    available: bool = False
    provenance: str = "unknown"
    reported_fresh_valid: bool = False
    reported_fresh: bool = False
    used_valid: bool = False
    used: bool = False
    physically_verified_valid: bool = False
    physically_verified: bool = False
    max_age_s_valid: bool = False
    max_age_s: float = 0.
    confidence_valid: bool = False
    confidence: float = 0.
    failure_reason: str = ""
    ignored_reason: str = ""


@dataclass(frozen=True)
class HostVitals:
    context: ObservationContext
    host_valid: bool = False
    host: str = ""
    source: str = ""
    age_valid: bool = False
    age_s: float = 0.
    cpu_percent_valid: bool = False
    cpu_percent: float = 0.
    process_cpu_percent_valid: bool = False
    process_cpu_percent: float = 0.
    rss_mib_valid: bool = False
    rss_mib: float = 0.
    memory_used_mib_valid: bool = False
    memory_used_mib: float = 0.
    memory_total_mib_valid: bool = False
    memory_total_mib: float = 0.
    temperature_c_valid: bool = False
    temperature_c: float = 0.
    temperature_source: str = ""
    sampling_state: str = ""
    sampling_reason: str = ""
    cpu_reason: str = ""
    process_cpu_reason: str = ""
    rss_reason: str = ""
    memory_used_reason: str = ""
    memory_total_reason: str = ""
    temperature_reason: str = ""


@dataclass(frozen=True)
class ObservationSnapshot:
    context: ObservationContext
    companion_context: ObservationContext
    inputs: tuple[InputObservation, ...]
    pi: HostVitals
    jetson: HostVitals
    revision: int = 0
    real_valid: bool = False
    real: bool = False
    simulated_valid: bool = False
    simulated: bool = False
    motion_enabled_valid: bool = False
    motion_enabled: bool = False
    stop_latched_valid: bool = False
    stop_latched: bool = False
    companion_motion_enabled_valid: bool = False
    companion_motion_enabled: bool = False
    companion_stopped_valid: bool = False
    companion_stopped: bool = False
    battery_v_valid: bool = False
    battery_v: float = 0.
    sonar_cm_valid: bool = False
    sonar_cm: float = 0.
    companion_fresh: bool = False


@dataclass(frozen=True)
class ObservationResult:
    """One consume-once worker result; errors never carry an earlier snapshot."""
    snapshot: ObservationSnapshot | None
    error: str
    received_at: float


def _clip(value, limit=300):
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore") if isinstance(value, str) else ""


def _failure(exc):
    # Validation/transport exceptions may include the rejected body or URL.
    # Only our own plain ValueErrors contain controlled diagnostic text.
    return _clip(str(exc)) if type(exc) is ValueError else "observation failed (" + type(exc).__name__ + ")"


def _map(value):
    return value if isinstance(value, dict) else {}


def _number(value, minimum=0., maximum=1e15, *, positive=False):
    try:
        finite = finite_number(value)
    except OverflowError:
        finite = False
    if not finite or value < minimum or value > maximum or (positive and value == 0):
        return None
    return float(value)


def _uint(value, *, positive=False):
    return value if type(value) is int and (1 if positive else 0) <= value <= MAX_UINT64 else None


def _identity(value):
    return value if isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 64 else ""


def _finite_json(value):
    """Bound nested work, including unused/private fields that are never emitted."""
    remaining = 8192

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 20:
            raise ValueError("observation JSON nesting or item limit exceeded")
        if isinstance(item, dict):
            if len(item) > 1024:
                raise ValueError("observation object too large")
            for key, child in item.items():
                if not isinstance(key, str) or len(key.encode("utf-8")) > 65536:
                    raise ValueError("invalid observation key")
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 1024:
                raise ValueError("observation array too large")
            for child in item:
                visit(child, depth + 1)
        elif type(item) in (int, float):
            if _number(item, minimum=-1e308, maximum=1e308) is None:
                raise ValueError("observation must contain finite numbers")
        elif isinstance(item, str):
            if len(item.encode("utf-8")) > 65536:
                raise ValueError("observation string too large")
        elif item is not None and type(item) is not bool:
            raise ValueError("observation must be JSON")

    visit(value, 0)
    try:
        if len(json.dumps(value, allow_nan=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("observation response too large")
    except (OverflowError, RecursionError, TypeError) as exc:
        raise ValueError("invalid observation JSON") from exc


def _reported_clock(value):
    try:
        return CaptureClock.model_validate(value)
    except (TypeError, ValueError):
        return None


def _with_capture(context, stamp, domain, receipt, receipt_domain, reported, age, fresh):
    stamp, receipt = _number(stamp, positive=True), _number(receipt, positive=True)
    reported = _reported_clock(reported)
    return replace(context, captured_at_valid=stamp is not None, captured_at=stamp or 0.,
                   captured_clock_domain=domain if stamp is not None else "",
                   producer_received_at_valid=receipt is not None, producer_received_at=receipt or 0.,
                   producer_received_clock_domain=receipt_domain if receipt is not None else "",
                   age_upper_valid=age is not None, age_upper_s=age or 0.,
                   timing_verified=context.offset_bounds_valid and age is not None and stamp is not None,
                   fresh=bool(fresh and stamp is not None),
                   reported_clock_valid=reported is not None,
                   reported_offset_lower_s=reported.offset_lower_s if reported else 0.,
                   reported_offset_upper_s=reported.offset_upper_s if reported else 0.,
                   reported_wall_at=reported.wall_at if reported else 0.,
                   reported_mono_at=reported.mono_at if reported else 0.)


class ObservationConverter:
    """Stateful replay fences plus pure conversion; safe to construct anywhere.

    Call from one worker. The only accepted local monotonic epoch is received_mono
    paired with clock measured by this bridge. InputHealth.age_s was computed by
    its producer already; add Pi upper-now minus _produced_at_pi_lower rather than
    evaluating that producer's InputHealth.clock.remote_bounds on this machine.
    """

    def __init__(self):
        self._boot = ""
        self._server_at = self._revision = None
        self._publisher = ""
        self._epoch = self._sequence = None
        self._source_fingerprint = None
        self._retired_boots = deque(maxlen=32)
        self._retired_publishers = deque(maxlen=32)

    def convert(self, state, *, received_wall, received_mono, clock=None, clock_boot_id=None):
        _finite_json(state)
        if not isinstance(state, dict):
            raise ValueError("state envelope must be an object")
        server_at = _number(state.get("server_at"), positive=True)
        boot, revision = _identity(state.get("server_boot_id")), _uint(state.get("revision"))
        if server_at is None or not boot or revision is None or not isinstance(state.get("brain"), dict):
            raise ValueError("state requires server_at, server_boot_id, revision and brain object")
        if _number(received_wall, positive=True) is None or _number(received_mono) is None:
            raise ValueError("invalid bridge receipt clock")
        brain = state["brain"]
        # Canonical telemetry retains the entire brain value unchanged for one
        # publisher/sequence. Keep only a fixed-size digest; private values never
        # leave conversion or appear in emitted diagnostics.
        fingerprint = hashlib.sha256(json.dumps(brain, sort_keys=True, separators=(",", ":"),
                                               allow_nan=False).encode("utf-8")).digest()
        publisher = _identity(brain.get("_publisher_id"))
        sequence = _uint(brain.get("_sequence"), positive=True)
        epoch = _uint(brain.get("_publisher_started_at_ns"), positive=True)
        changed_boot = boot != self._boot
        if changed_boot and boot in self._retired_boots:
            raise ValueError("retired robot boot replay")
        if not changed_boot:
            if server_at <= self._server_at or revision < self._revision:
                raise ValueError("state timestamp or revision replay")
            if publisher and publisher == self._publisher:
                if sequence is not None and self._sequence is not None and sequence < self._sequence:
                    raise ValueError("companion sequence replay")
                if epoch is not None and self._epoch is not None and epoch != self._epoch:
                    raise ValueError("publisher epoch changed within identity")
                if (sequence is not None and sequence == self._sequence
                        and self._source_fingerprint != fingerprint):
                    raise ValueError("unchanged sequence cannot change companion payload")
            elif publisher:
                if publisher in self._retired_publishers:
                    raise ValueError("retired companion publisher replay")
                if self._publisher and (epoch is None or self._epoch is None or epoch <= self._epoch):
                    raise ValueError("companion publisher order unknown or outdated")

        own_clock = None
        if clock_boot_id == boot:
            try:
                own_clock = clock if isinstance(clock, CaptureClock) else CaptureClock.model_validate(clock)
                own_clock.remote_bounds(received_wall, received_mono)
            except (TypeError, ValueError):
                own_clock = None
        state_age = capture_age(server_at, own_clock, wall=received_wall, mono=received_mono) if own_clock else None
        produced_lower = _number(brain.get("_produced_at_pi_lower"), positive=True)
        transport = _map(_map(brain.get("pipeline")).get("transport"))
        reported_boot = _identity(transport.get("boot_id"))
        source_boot_matches = reported_boot == boot
        source_age = (capture_age(produced_lower, own_clock, wall=received_wall, mono=received_mono)
                      if own_clock and source_boot_matches else None)
        # Identity validity is needed to describe a source as fresh; unknown
        # identity still yields numeric diagnostics and reported metadata.
        source_fresh = (source_age is not None and source_age <= 2. and bool(publisher) and sequence is not None
                        and _number(brain.get("_produced_at"), positive=True) is not None)
        session = _identity(transport.get("session_id"))
        base = ObservationContext(
            producer="pi.dashboard", robot_boot_id=boot,
            session_id_valid=bool(session), session_id=session,
            publisher_id_valid=bool(publisher), publisher_id=publisher,
            publisher_started_at_ns_valid=epoch is not None, publisher_started_at_ns=epoch or 0,
            sequence_valid=sequence is not None, sequence=sequence or 0,
            bridge_received_at=float(received_wall), bridge_received_mono=float(received_mono),
            age_evaluated_mono=float(received_mono),
            calibration_valid_until_mono=own_clock.mono_at + MAX_ANCHOR_AGE_S if own_clock else 0.,
            offset_bounds_valid=own_clock is not None,
            offset_lower_s=own_clock.offset_lower_s if own_clock else 0.,
            offset_upper_s=own_clock.offset_upper_s if own_clock else 0.,
            reported_produced_at_pi_lower_valid=produced_lower is not None,
            reported_produced_at_pi_lower=produced_lower or 0.)
        context = _with_capture(base, server_at, "pi.unix", brain.get("_received_at"), "pi.unix",
                                brain.get("pi_clock"), state_age, state_age is not None and state_age <= 2.)
        companion = _with_capture(replace(base, producer="jetson.companion", robot_boot_id=reported_boot), brain.get("_produced_at"),
                                  "jetson.unix", brain.get("_received_at"), "pi.unix",
                                  brain.get("pi_clock"), source_age, source_fresh)
        inputs = brain.get("inputs", {})
        if not isinstance(inputs, dict) or len(inputs) > MAX_INPUTS:
            raise ValueError("input observation object exceeds bound or is malformed")
        observations = tuple(self._input(name, _map(raw), companion, source_age, source_fresh)
                             for name, raw in sorted(inputs.items()))
        pi = self._host(_map(state.get("host_vitals")), "pi", context, state_age)
        jetson = self._host(_map(brain.get("host_vitals")), "jetson", companion, source_age)
        runtime = _map(state.get("robot_runtime"))
        motion = _map(runtime.get("motion"))
        simulate = runtime.get("simulate")
        battery, sonar = _number(brain.get("battery_v"), maximum=100.), _number(brain.get("distance_cm"), maximum=100000.)
        result = ObservationSnapshot(
            context=context, companion_context=companion, inputs=observations, pi=pi, jetson=jetson,
            revision=revision, real_valid=type(simulate) is bool, real=simulate is False,
            simulated_valid=type(simulate) is bool, simulated=simulate is True,
            motion_enabled_valid=type(motion.get("enabled")) is bool, motion_enabled=motion.get("enabled") is True,
            stop_latched_valid=type(motion.get("stop_latched")) is bool, stop_latched=motion.get("stop_latched") is True,
            companion_motion_enabled_valid=type(brain.get("motion_enabled")) is bool,
            companion_motion_enabled=brain.get("motion_enabled") is True,
            companion_stopped_valid=type(brain.get("stopped")) is bool, companion_stopped=brain.get("stopped") is True,
            battery_v_valid=battery is not None, battery_v=battery or 0., sonar_cm_valid=sonar is not None,
            sonar_cm=sonar or 0., companion_fresh=bool(source_fresh))
        # Commit fences only after complete successful conversion.
        if changed_boot:
            if self._boot:
                self._retired_boots.append(self._boot)
            self._publisher, self._epoch, self._sequence = "", None, None
            self._retired_publishers.clear()
        if publisher:
            if publisher != self._publisher:
                if self._publisher:
                    self._retired_publishers.append(self._publisher)
                self._publisher, self._epoch, self._sequence = publisher, epoch, sequence
            else:
                if epoch is not None:
                    self._epoch = epoch
                if sequence is not None:
                    self._sequence = sequence
            self._source_fingerprint = fingerprint
        self._boot, self._server_at, self._revision = boot, server_at, revision
        return result

    @staticmethod
    def _input(name, raw, base, source_age, source_fresh):
        producer_age = _number(raw.get("age_s"))
        age = producer_age + source_age if producer_age is not None and source_age is not None else None
        limit = _number(raw.get("max_age_s"), maximum=300., positive=True)
        confidence = _number(raw.get("confidence"), maximum=1.)
        fresh = (source_fresh and raw.get("fresh") is True and raw.get("available") is True
                 and age is not None and limit is not None and age <= limit)
        # InputHealth currently carries no explicit domain tags. A relayed Pi
        # sensor and locally constructed Jetson transport health can both have
        # received_at; do not infer their domain from source names or a clock.
        capture_domain = _clip(raw.get("captured_clock_domain"), 32) or "unspecified.unix"
        receipt_domain = _clip(raw.get("received_clock_domain"), 32) or "unspecified.unix"
        context = _with_capture(base, raw.get("captured_at"), capture_domain,
                                raw.get("received_at"), receipt_domain, raw.get("clock"), age, fresh)
        context = replace(context, producer="jetson.input_health")
        source = _clip(raw.get("source"), 128)
        return InputObservation(
            context=context, name=_clip(name, 64), source_valid=bool(source), source=source,
            available_valid=type(raw.get("available")) is bool, available=raw.get("available") is True,
            provenance=raw.get("provenance") if raw.get("provenance") in ("real", "simulated", "unknown") else "unknown",
            reported_fresh_valid=type(raw.get("fresh")) is bool, reported_fresh=raw.get("fresh") is True,
            used_valid=type(raw.get("used")) is bool, used=raw.get("used") is True,
            physically_verified_valid=type(raw.get("physically_verified")) is bool,
            physically_verified=raw.get("physically_verified") is True,
            max_age_s_valid=limit is not None, max_age_s=limit or 0.,
            confidence_valid=confidence is not None, confidence=confidence or 0.,
            failure_reason=_clip(raw.get("failure_reason")), ignored_reason=_clip(raw.get("ignored_reason")))

    @staticmethod
    def _host(raw, host, base, delivery_age):
        host_valid = raw.get("host") == host
        age = _number(raw.get("age_s")) if host_valid else None
        age = age + delivery_age if age is not None and delivery_age is not None else None
        context = _with_capture(replace(base, producer=host + ".host_vitals"), raw.get("captured_at"),
                                host + ".unix", None, "", None, age, age is not None and age <= 3.)
        values = {}
        for key in ("cpu_percent", "process_cpu_percent", "rss_mib", "memory_used_mib", "memory_total_mib", "temperature_c"):
            value = _number(raw.get(key), minimum=-10. if key == "temperature_c" else 0.,
                            maximum=125. if key == "temperature_c" else (100. if key == "cpu_percent" else 1e12))
            values[key + "_valid"], values[key] = value is not None and host_valid, value if value is not None else 0.
        reasons, sampling = _map(raw.get("reasons")), _map(raw.get("sampling"))
        return HostVitals(context=context, host_valid=host_valid, host=_clip(raw.get("host"), 16), source=_clip(raw.get("source"), 160),
                          age_valid=age is not None, age_s=age or 0., **values,
                          temperature_source=_clip(raw.get("temperature_source"), 128),
                          sampling_state=_clip(sampling.get("state"), 32), sampling_reason=_clip(sampling.get("reason")),
                          cpu_reason=_clip(reasons.get("cpu_percent")), process_cpu_reason=_clip(reasons.get("process_cpu_percent")),
                          rss_reason=_clip(reasons.get("rss_mib")), memory_used_reason=_clip(reasons.get("memory_used_mib")),
                          memory_total_reason=_clip(reasons.get("memory_total_mib")), temperature_reason=_clip(reasons.get("temperature_c")))


def age_for_publish(snapshot: ObservationSnapshot, now_mono: float) -> ObservationSnapshot:
    """Age an immutable latest-slot value at publication; never refresh receipt.

    Safe to call repeatedly on either the original or an already-aged value.
    Monotonic reversal or expiration of the bridge's calibration makes timing
    unknown while preserving the original raw readings and reported metadata.
    """
    def aged(context, limit):
        valid = (finite_number(now_mono) and context.offset_bounds_valid
                 and context.age_evaluated_mono <= now_mono <= context.calibration_valid_until_mono)
        delay = (now_mono - context.age_evaluated_mono) * (1. + DRIFT_S_PER_S) if valid else 0.
        age_valid = valid and context.age_upper_valid
        age = context.age_upper_s + delay if age_valid else 0.
        return replace(context, offset_bounds_valid=bool(valid), age_upper_valid=bool(age_valid),
                       age_upper_s=age, timing_verified=bool(valid and context.timing_verified),
                       fresh=bool(age_valid and context.fresh and age <= limit),
                       age_evaluated_mono=float(now_mono) if finite_number(now_mono) else context.age_evaluated_mono)

    context, companion = aged(snapshot.context, 2.), aged(snapshot.companion_context, 2.)
    inputs = []
    for item in snapshot.inputs:
        changed = aged(item.context, item.max_age_s if item.max_age_s_valid else 0.)
        inputs.append(replace(item, context=replace(changed, fresh=bool(changed.fresh and companion.fresh))))

    def host(value):
        changed = aged(value.context, 3.)
        return replace(value, context=changed, age_valid=changed.age_upper_valid, age_s=changed.age_upper_s)

    return replace(snapshot, context=context, companion_context=companion, inputs=tuple(inputs),
                   pi=host(snapshot.pi), jetson=host(snapshot.jetson), companion_fresh=companion.fresh)


class HTTPObserver:
    """One explicit worker, two allowlisted GETs per poll, one latest result slot.

    timeout_s is a transport inactivity limit; each request also checks elapsed
    monotonic time between raw body chunks. DNS, header parsing and an in-progress
    read have no hard whole-request deadline here. close() bounds joining and
    fences late output.
    No constructor I/O. poll_once(client) is for deterministic fake-HTTP tests;
    it cannot overlap another poll or the worker.
    """

    def __init__(self, base_url: str, *, poll_hz=1., timeout_s=.5,
                 client_factory: Callable = httpx.Client, wall=time.time, monotonic=time.monotonic):
        require_local_url(base_url)
        parsed = urlsplit(base_url)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("observer requires a base URL without path, query or fragment")
        if not finite_number(poll_hz) or not 0 < poll_hz <= 5:
            raise ValueError("poll_hz must be positive and at most 5")
        if not finite_number(timeout_s) or not 0 < timeout_s <= 1.:
            raise ValueError("timeout_s must be positive and at most 1 second")
        self.base_url, self.poll_hz, self.timeout_s = base_url.rstrip("/"), float(poll_hz), float(timeout_s)
        self._client_factory, self._wall, self._mono = client_factory, wall, monotonic
        self._lock, self._poll_lock = threading.Lock(), threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._latest = None
        self._clock, self._clock_boot = None, ""
        self._converter = ObservationConverter()
        self._completed = self._failed = self._replaced = self._calibration_failed = 0
        self._last_error = self._calibration_error = ""

    def start(self):
        with self._lock:
            if self._closed:
                return False
            if self._thread is not None:
                return self._thread.is_alive()
            self._thread = threading.Thread(target=self._run, name="ros-observation-http", daemon=True)
            self._thread.start()
            return True

    def _read(self, client, path):
        if self._stop.is_set():
            raise ValueError("observer closed")
        started = self._mono()
        with client.stream("GET", self.base_url + path, headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError("observation HTTP status " + str(response.status_code))
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("compressed observation response rejected")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                raise ValueError("observation response size exceeds bound")
            data = bytearray()
            # iter_raw avoids accepting compressed decompression bombs; HTTP
            # decoding remains disabled and unsupported encodings are rejected.
            # Use the raw stream directly: iter_raw() hides empty chunks and a
            # chunk_size also coalesces tiny chunks before checks can run.
            # The socket timeout is inactivity, not a hard bound
            # on DNS, headers or an in-progress read; inspect between raw reads.
            chunks = (response.content,) if response.is_stream_consumed else response.stream
            for count, chunk in enumerate(chunks, 1):
                elapsed = self._mono() - started
                if self._stop.is_set() or elapsed < 0 or elapsed > self.timeout_s:
                    raise ValueError("observation request deadline exceeded or observer closed")
                if count > MAX_RESPONSE_CHUNKS:
                    raise ValueError("observation response chunk count exceeds bound")
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("observation response size exceeds bound")
                data.extend(chunk)
            elapsed = self._mono() - started
            if elapsed < 0 or elapsed > self.timeout_s or self._stop.is_set():
                raise ValueError("observation request deadline exceeded or observer closed")
        try:
            value = json.loads(data, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except (RecursionError, UnicodeError) as exc:
            raise ValueError("invalid observation JSON") from exc
        _finite_json(value)
        return value

    def poll_once(self, client):
        if not self._poll_lock.acquire(blocking=False):
            raise RuntimeError("observation poll already active")
        try:
            if self._stop.is_set():
                return
            calibration_error = ""
            sent_wall, sent_mono = self._wall(), self._mono()
            try:
                probe = self._read(client, "/sim/clock")
                boot = _identity(_map(probe).get("server_boot_id"))
                if not boot:
                    raise ValueError("clock probe boot identity missing")
                previous = self._clock if boot == self._clock_boot else None
                self._clock = CaptureClock.measure(_map(probe).get("server_at"), sent_wall, sent_mono,
                                                   self._wall(), self._mono(), previous)
                self._clock_boot = boot
            except Exception as exc:
                calibration_error = _failure(exc)
                self._clock, self._clock_boot = None, ""
            if self._stop.is_set():
                return
            try:
                payload = self._read(client, "/sim/state")
                receipt_wall, receipt_mono = self._wall(), self._mono()
                snapshot = self._converter.convert(payload, received_wall=receipt_wall, received_mono=receipt_mono,
                                                   clock=self._clock, clock_boot_id=self._clock_boot)
                # A state boot different from the probe cannot retain an anchor
                # for a later state/probe after the observed restart.
                if snapshot.context.robot_boot_id != self._clock_boot:
                    self._clock, self._clock_boot = None, ""
                result = ObservationResult(snapshot, "", receipt_wall)
            except Exception as exc:
                result = ObservationResult(None, _failure(exc) or "observation failed", self._wall())
            with self._lock:
                if self._closed:
                    return
                self._completed += 1
                self._failed += int(result.snapshot is None)
                self._calibration_failed += int(bool(calibration_error))
                self._last_error, self._calibration_error = result.error, calibration_error
                self._replace(result)
        finally:
            self._poll_lock.release()

    def _replace(self, result):
        self._replaced += int(self._latest is not None)
        self._latest = result

    def _run(self):
        try:
            with self._client_factory(timeout=httpx.Timeout(self.timeout_s), trust_env=False, follow_redirects=False,
                                      limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
                while not self._stop.is_set():
                    started = self._mono()
                    self.poll_once(client)
                    self._stop.wait(max(0., 1. / self.poll_hz - max(0., self._mono() - started)))
        except Exception as exc:
            with self._lock:
                if not self._closed:
                    self._failed += 1
                    self._last_error = _failure(exc) or "observation worker failed"
                    self._replace(ObservationResult(None, self._last_error, self._wall()))

    def take_latest(self):
        with self._lock:
            result, self._latest = self._latest, None
            return result

    def health(self):
        with self._lock:
            return dict(running=self._thread is not None and self._thread.is_alive(), closed=self._closed,
                        pending=self._latest is not None, completed=self._completed, failed=self._failed,
                        replaced=self._replaced, calibration_failed=self._calibration_failed,
                        failure_reason=self._last_error, calibration_error=self._calibration_error,
                        poll_hz=self.poll_hz)

    def close(self, timeout=.5):
        if not finite_number(timeout) or not 0 <= timeout <= 2.:
            raise ValueError("close timeout must be between zero and 2 seconds")
        with self._lock:
            if not self._closed:
                self._closed = True
                self._stop.set()
                self._replace(ObservationResult(None, "observer closed", self._wall()))
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
