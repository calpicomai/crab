"""Pure observation conversion and fake HTTP; no ROS, network or devices."""
from __future__ import annotations

import copy
from dataclasses import asdict, fields, FrozenInstanceError
import json
import math
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from brain.ros_observer.core import (
    HTTPObserver, ObservationConverter, ObservationContext, InputObservation,
    HostVitals, ObservationSnapshot, MAX_RESPONSE_BYTES, age_for_publish,
)
from shared.clock import CaptureClock


def anchor():
    return CaptureClock.measure(1000.006, 1000., 10., 1000.012, 10.012)


def state(at=1000.05, sequence=3, boot="pi-one", revision=3):
    health = dict(available=True, provenance="real", captured_at=999.8,
                  received_at=999.9, age_s=.2, max_age_s=2., fresh=True,
                  source="pi-sonar", used=False, ignored_reason="not consumed",
                  failure_reason=None, confidence=.9, clock=anchor().model_dump())
    metrics = dict(captured_at=999.9, age_s=.1, cpu_percent=12.,
                   process_cpu_percent=2., rss_mib=30., memory_used_mib=100.,
                   memory_total_mib=200., temperature_c=40., source="Linux /proc",
                   temperature_source="cpu", reasons={"cpu_percent": "warming"},
                   sampling={"state": "sampled", "reason": None})
    return dict(server_at=at, server_boot_id=boot, revision=revision, enabled=False,
                robot_runtime=dict(simulate=False, motion=dict(enabled=False, stop_latched=True)),
                host_vitals={**metrics, "host": "pi"},
                brain=dict(_publisher_id="jetson-one", _publisher_started_at_ns=100,
                           _sequence=sequence, _produced_at=999.99,
                           _produced_at_pi_lower=999.98, _received_at=1000.,
                           pi_clock=anchor().model_dump(),
                           pipeline={"transport": {"session_id": "ws-session", "boot_id": boot}},
                           inputs={"ultrasonic": health}, host_vitals={**metrics, "host": "jetson"},
                           battery_v=None, distance_cm=25., stopped=True, motion_enabled=False,
                           last_heard="PRIVATE UTTERANCE", last_say="PRIVATE NARRATIVE"))


class ConverterTests(unittest.TestCase):
    def convert(self, value=None, converter=None, **kwargs):
        options = dict(received_wall=1000.062, received_mono=10.062,
                       clock=anchor(), clock_boot_id="pi-one")
        options.update(kwargs)
        return (converter or ObservationConverter()).convert(state() if value is None else value, **options)

    def test_original_identities_timestamps_truth_and_conservative_ages(self):
        result = self.convert()
        self.assertIsInstance(result, ObservationSnapshot)
        context, item = result.context, result.inputs[0]
        self.assertEqual(context.robot_boot_id, "pi-one")
        self.assertEqual(context.session_id, "ws-session")
        self.assertEqual(context.publisher_id, "jetson-one")
        self.assertEqual(context.sequence, 3)
        self.assertEqual(item.source, "pi-sonar")
        self.assertEqual(item.context.captured_at, 999.8)
        self.assertEqual(item.context.producer_received_at, 999.9)
        self.assertEqual(item.context.producer_received_clock_domain, "unspecified.unix")
        self.assertEqual(item.context.bridge_received_at, 1000.062)
        self.assertGreater(item.context.age_upper_s, .28)
        self.assertGreater(result.pi.age_s, .11)
        self.assertGreater(result.jetson.age_s, .18)
        self.assertTrue(item.context.fresh)
        self.assertTrue(item.available)
        self.assertFalse(item.physically_verified)
        self.assertFalse(item.used)
        self.assertTrue(result.real_valid)
        self.assertTrue(result.real)
        self.assertTrue(result.stop_latched)
        self.assertFalse(result.motion_enabled)
        self.assertFalse(result.battery_v_valid)
        self.assertTrue(result.sonar_cm_valid)
        self.assertEqual(result.sonar_cm, 25.)
        self.assertNotIn("PRIVATE", json.dumps(asdict(result)))
        with self.assertRaises(FrozenInstanceError):
            result.sonar_cm = 0

    def test_repeated_receipt_adds_source_delivery_age(self):
        converter = ObservationConverter()
        first = self.convert(converter=converter)
        later = state(at=1001.05)
        second = self.convert(later, converter, received_wall=1001.062, received_mono=11.062)
        self.assertGreater(second.inputs[0].context.age_upper_s, first.inputs[0].context.age_upper_s + .99)
        self.assertEqual(second.inputs[0].context.captured_at, first.inputs[0].context.captured_at)

    def test_unknown_missing_null_disabled_remain_unknown(self):
        value = state()
        value["brain"] = {"inputs": {"camera": {"available": False, "failure_reason": "disabled"}}}
        value["host_vitals"] = {"cpu_percent": None, "age_s": None}
        value["robot_runtime"] = {}
        result = self.convert(value)
        item = result.inputs[0]
        self.assertFalse(item.context.sequence_valid)
        self.assertFalse(item.context.session_id_valid)
        self.assertFalse(item.context.captured_at_valid)
        self.assertFalse(item.context.age_upper_valid)
        self.assertFalse(item.context.fresh)
        self.assertFalse(item.source_valid)
        self.assertFalse(item.available)
        self.assertEqual(item.failure_reason, "disabled")
        self.assertFalse(result.real_valid)
        self.assertFalse(result.pi.cpu_percent_valid)
        self.assertFalse(result.pi.age_valid)
        self.assertFalse(result.companion_fresh)
        self.assertFalse(result.sonar_cm_valid)

    def test_untrusted_clock_keeps_raw_diagnostics_without_freshness(self):
        for options in (dict(clock=None), dict(clock_boot_id="other"),
                        dict(received_wall=1001.062), dict(received_mono=16.)):
            with self.subTest(options=options):
                result = self.convert(**options)
                self.assertEqual(result.sonar_cm, 25.)
                self.assertTrue(result.inputs[0].available)
                self.assertFalse(result.context.timing_verified)
                self.assertFalse(result.inputs[0].context.fresh)

    def test_upstream_monotonic_epoch_is_metadata_only(self):
        value = state()
        value["brain"]["pi_clock"]["mono_at"] = 1_000_000.
        value["brain"]["inputs"]["ultrasonic"]["clock"]["mono_at"] = 2_000_000.
        result = self.convert(value)
        self.assertTrue(result.inputs[0].context.fresh)
        self.assertEqual(result.context.reported_mono_at, 1_000_000.)
        self.assertEqual(result.inputs[0].context.reported_mono_at, 2_000_000.)

    def test_publish_queue_delay_ages_once_and_expires_or_invalidates(self):
        original = self.convert()
        delayed = age_for_publish(original, 11.062)
        self.assertGreater(delayed.inputs[0].context.age_upper_s, original.inputs[0].context.age_upper_s + 1.)
        self.assertFalse(age_for_publish(original, 12.062).inputs[0].context.fresh)
        twice = age_for_publish(delayed, 12.062)
        once = age_for_publish(original, 12.062)
        self.assertAlmostEqual(twice.inputs[0].context.age_upper_s, once.inputs[0].context.age_upper_s)
        self.assertEqual(delayed.context.bridge_received_mono, original.context.bridge_received_mono)
        for now in (9., 16., math.nan):
            invalid = age_for_publish(original, now)
            self.assertFalse(invalid.context.timing_verified)
            self.assertFalse(invalid.inputs[0].context.age_upper_valid)
            self.assertFalse(invalid.inputs[0].context.fresh)
            self.assertFalse(invalid.pi.age_valid)
            self.assertFalse(invalid.companion_fresh)

    def test_missing_capture_cannot_become_fresh_from_reported_age(self):
        payload = state()
        payload["brain"]["inputs"]["ultrasonic"]["captured_at"] = None
        result = self.convert(payload)
        self.assertFalse(result.inputs[0].context.fresh)
        self.assertFalse(result.inputs[0].context.timing_verified)

    def test_same_sequence_cannot_refresh_original_source_age(self):
        converter = ObservationConverter()
        self.convert(converter=converter)
        payload = state(at=1001.05)
        payload["brain"].update(_produced_at_pi_lower=1001.04, _received_at=1001.045)
        with self.assertRaises(ValueError):
            self.convert(payload, converter, received_wall=1001.062, received_mono=11.062)

    def test_same_sequence_fences_all_retained_input_metadata(self):
        for field, changed in (("captured_at", 1000.04), ("age_s", 0.), ("available", False),
                               ("source", "other"), ("used", True)):
            converter = ObservationConverter()
            self.convert(converter=converter)
            payload = state(at=1000.06)
            payload["brain"]["inputs"]["ultrasonic"][field] = changed
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unchanged sequence"):
                self.convert(payload, converter)

    def test_reported_boot_mismatch_and_missing_do_not_become_current_source(self):
        for reported_boot in ("pi-old", None):
            payload = state()
            payload["brain"]["pipeline"]["transport"]["boot_id"] = reported_boot
            result = self.convert(payload)
            self.assertEqual(result.context.robot_boot_id, "pi-one")
            self.assertEqual(result.companion_context.robot_boot_id, reported_boot or "")
            self.assertEqual(result.inputs[0].context.robot_boot_id, reported_boot or "")
            self.assertFalse(result.companion_fresh)
            self.assertFalse(result.companion_context.timing_verified)
            self.assertFalse(result.inputs[0].context.fresh)
            self.assertFalse(result.jetson.age_valid)
            self.assertTrue(result.pi.age_valid)
            self.assertTrue(result.sonar_cm_valid)

    def test_wrong_host_identity_does_not_authorize_host_health(self):
        payload = state()
        payload["host_vitals"]["host"] = "different"
        result = self.convert(payload)
        self.assertEqual(result.pi.host, "different")
        self.assertFalse(result.pi.host_valid)
        self.assertFalse(result.pi.cpu_percent_valid)
        self.assertFalse(result.pi.age_valid)
        self.assertFalse(result.pi.context.fresh)

    def test_domains_are_preserved_when_explicitly_reported(self):
        payload = state()
        payload["brain"]["inputs"]["ultrasonic"].update(
            captured_clock_domain="original.capture", received_clock_domain="original.receipt")
        item = self.convert(payload).inputs[0]
        self.assertEqual(item.context.captured_clock_domain, "original.capture")
        self.assertEqual(item.context.producer_received_clock_domain, "original.receipt")

    def test_reported_stale_failure_and_provenance_are_separate(self):
        for changes in ({"fresh": False}, {"available": False}, {"failure_reason": "no echo"},
                        {"provenance": "unknown"}, {"used": True, "provenance": "simulated"}):
            value = state()
            value["brain"]["inputs"]["ultrasonic"].update(changes)
            item = self.convert(value).inputs[0]
            self.assertEqual(item.used, changes.get("used", False))
            self.assertFalse(item.physically_verified)
            if "fresh" in changes or "available" in changes:
                self.assertFalse(item.context.fresh)
        value = state()
        value["brain"]["_produced_at_pi_lower"] = None
        self.assertFalse(self.convert(value).inputs[0].context.age_upper_valid)

    def test_malformed_mandatory_envelope_and_nonfinite_rejected(self):
        for key, value in (("server_at", None), ("server_at", True), ("server_at", "1000"),
                           ("server_boot_id", ""), ("revision", -1), ("revision", True),
                           ("brain", []), ("server_at", math.inf)):
            payload = state()
            payload[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.convert(payload)
        payload = state()
        del payload["revision"]
        with self.assertRaises(ValueError):
            self.convert(payload)
        payload = state()
        payload["brain"]["private_unused"] = math.nan
        with self.assertRaises(ValueError):
            self.convert(payload)

    def test_replay_state_revision_sequence_and_publisher_fences(self):
        for later in (state(), state(at=1000.04), state(at=1000.06, revision=2),
                      state(at=1000.06, sequence=2)):
            converter = ObservationConverter()
            self.convert(converter=converter)
            with self.assertRaises(ValueError):
                self.convert(later, converter)
        converter = ObservationConverter()
        self.convert(converter=converter)
        newer = state(at=1000.06)
        newer["brain"].update(_publisher_id="new", _publisher_started_at_ns=101, _sequence=1)
        self.convert(newer, converter)
        with self.assertRaises(ValueError):
            self.convert(state(at=1000.07), converter)

    def test_boot_change_invalidates_clock_and_fences_then_recalibrates(self):
        converter = ObservationConverter()
        self.convert(converter=converter)
        result = self.convert(state(at=999., sequence=1, boot="pi-two", revision=1), converter)
        self.assertFalse(result.context.timing_verified)
        result = self.convert(state(at=1000.06, sequence=2, boot="pi-two", revision=2), converter,
                              clock_boot_id="pi-two")
        self.assertTrue(result.context.timing_verified)
        with self.assertRaises(ValueError):
            self.convert(state(at=1000.08), converter)

    def test_bounds_and_diagnostic_strings_match_rosidl(self):
        value = state()
        raw = value["brain"]["inputs"]["ultrasonic"]
        raw["ignored_reason"] = "é" * 400
        item = self.convert(value).inputs[0]
        self.assertLessEqual(len(item.ignored_reason.encode()), 300)
        value["brain"]["inputs"] = {str(i): raw for i in range(33)}
        with self.assertRaises(ValueError):
            self.convert(value)
        root = Path(__file__).resolve().parents[1] / "ros2/src/crab_observation_msgs/msg"
        for cls in (ObservationContext, InputObservation, HostVitals, ObservationSnapshot):
            declared = {line.split()[-1] for line in (root / (cls.__name__ + ".msg")).read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith("#")}
            self.assertEqual(declared, {field.name for field in fields(cls)})


class FakeClock:
    def __init__(self):
        self.wall, self.mono = 1000., 10.

    def step(self, amount):
        self.wall += amount
        self.mono += amount


class StreamResponse(httpx.Response):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RawStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks()


class FakeHTTP:
    def __init__(self, clock, outcomes=()):
        self.clock, self.outcomes = clock, list(outcomes)
        self.calls, self.options = [], None
        self.closed = False

    def factory(self, **options):
        self.options = options
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url))
        self.clock.step(.006)
        outcome = self.outcomes.pop(0) if self.outcomes else (
            dict(server_at=self.clock.wall, server_boot_id="pi-one") if url.endswith("/sim/clock")
            else state(at=self.clock.wall))
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, bytes):
            return StreamResponse(200, content=outcome, request=httpx.Request(method, url))
        return StreamResponse(200, json=outcome, request=httpx.Request(method, url))


class HTTPObserverTests(unittest.TestCase):
    def make(self, outcomes=(), **kwargs):
        clock = FakeClock()
        fake = FakeHTTP(clock, outcomes)
        observer = HTTPObserver("http://localhost:8000", client_factory=fake.factory,
                                wall=lambda: clock.wall, monotonic=lambda: clock.mono, **kwargs)
        self.addCleanup(observer.close)
        return observer, fake, clock

    def test_slow_raw_trickle_is_not_coalesced_past_body_deadline(self):
        observer, _, clock = self.make(timeout_s=.5)
        consumed = []

        class Drip(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(20000):
                    consumed.append(1)
                    clock.step(.1)
                    yield b" "

        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip()))
        with httpx.Client(transport=transport) as client:
            with self.assertRaisesRegex(ValueError, "deadline"):
                observer._read(client, "/sim/clock")
        self.assertLessEqual(len(consumed), 6)

    def test_fragmented_raw_body_has_a_chunk_budget(self):
        for fragment in (b" ", b""):
            with self.subTest(empty=not fragment):
                observer, _, _ = self.make()
                consumed = []

                class Fragmented(httpx.SyncByteStream):
                    def __iter__(self):
                        for _ in range(20000):
                            consumed.append(1)
                            yield fragment

                transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Fragmented()))
                with httpx.Client(transport=transport) as client:
                    with self.assertRaisesRegex(ValueError, "chunk count"):
                        observer._read(client, "/sim/state")
                self.assertEqual(len(consumed), 4097)

    def test_close_is_checked_between_unbuffered_raw_chunks(self):
        observer, _, _ = self.make()
        consumed = []

        class Closing(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(20000):
                    consumed.append(1)
                    if len(consumed) == 2:
                        observer.close()
                    yield b" "

        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Closing()))
        with httpx.Client(transport=transport) as client:
            with self.assertRaisesRegex(ValueError, "closed"):
                observer._read(client, "/sim/state")
        self.assertEqual(len(consumed), 2)

    def test_safe_construction_explicit_start_single_worker_get_only_latest_slot(self):
        observer, fake, clock = self.make(poll_hz=5.)
        self.assertEqual(fake.calls, [])
        self.assertFalse(observer.health()["running"])
        self.assertTrue(observer.start())
        self.assertTrue(observer.start())
        deadline = time.monotonic() + 1
        while observer.health()["completed"] < 2 and time.monotonic() < deadline:
            time.sleep(.005)
        observer.close()
        self.assertGreaterEqual(observer.health()["completed"], 2)
        self.assertGreaterEqual(observer.health()["replaced"], 1)
        self.assertTrue(all(method == "GET" for method, _ in fake.calls))
        self.assertEqual({url for _, url in fake.calls},
                         {"http://localhost:8000/sim/clock", "http://localhost:8000/sim/state"})
        self.assertFalse(fake.options["trust_env"])
        self.assertFalse(fake.options["follow_redirects"])
        self.assertLessEqual(fake.options["timeout"].read, 1.)
        self.assertTrue(fake.closed)
        result = observer.take_latest()
        self.assertIsNone(result.snapshot)
        self.assertEqual(result.error, "observer closed")
        self.assertIsNone(observer.take_latest())
        self.assertFalse(observer.start())

    def test_one_poll_failure_replaces_success_without_stale_snapshot(self):
        observer, fake, _ = self.make()
        with fake:
            observer.poll_once(fake)
            self.assertIsNotNone(observer.take_latest().snapshot)
            fake.outcomes = [dict(server_at=fake.clock.wall + .006, server_boot_id="pi-one"),
                             httpx.ReadTimeout("slow")]
            observer.poll_once(fake)
            result = observer.take_latest()
        self.assertIsNone(result.snapshot)
        self.assertTrue(result.error)
        self.assertEqual(observer.health()["failed"], 1)

    def test_calibration_failure_still_returns_untrusted_raw_state(self):
        observer, fake, _ = self.make([dict(server_at=2000., server_boot_id="pi-one"), state()])
        observer.poll_once(fake)
        result = observer.take_latest()
        self.assertIsNotNone(result.snapshot)
        self.assertEqual(result.snapshot.sonar_cm, 25.)
        self.assertFalse(result.snapshot.context.timing_verified)
        self.assertEqual(observer.health()["calibration_failed"], 1)

    def test_oversize_invalid_json_and_nonfinite_are_failed_results(self):
        for payload in (b" " * (MAX_RESPONSE_BYTES + 1), b"{bad", b'{"server_at":NaN}',
                        b"[" * 30 + b"0" + b"]" * 30):
            observer, fake, _ = self.make([dict(server_at=1000.006, server_boot_id="pi-one"), payload])
            observer.poll_once(fake)
            self.assertIsNone(observer.take_latest().snapshot)
            self.assertEqual(observer.health()["failed"], 1)

    def test_no_redirects_compression_or_slow_body(self):
        for kind in ("redirect", "compressed", "slow"):
            observer, fake, clock = self.make()
            original = fake.stream

            def stream(method, url, **kwargs):
                response = original(method, url, **kwargs)
                if url.endswith("/sim/state"):
                    if kind == "redirect":
                        return StreamResponse(302, headers={"location": "http://localhost/control"},
                                              request=httpx.Request(method, url))
                    if kind == "compressed":
                        response.headers["content-encoding"] = "gzip"
                    else:
                        response.is_stream_consumed = False
                        response.stream = RawStream(lambda: (clock.step(2.) or b"{}",))
                return response

            fake.stream = stream
            observer.poll_once(fake)
            self.assertIsNone(observer.take_latest().snapshot)
            self.assertEqual(len(fake.calls), 2)

    def test_stream_cap_without_content_length_and_one_active_poll(self):
        observer, fake, _ = self.make()
        original = fake.stream

        def stream(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if url.endswith("/sim/state"):
                response.headers.pop("content-length", None)
                response.is_stream_consumed = False
                response.stream = RawStream(lambda: [b" " * 16384] * 17)
            return response

        fake.stream = stream
        observer.poll_once(fake)
        self.assertIsNone(observer.take_latest().snapshot)
        self.assertIn("size", observer.health()["failure_reason"])
        observer._poll_lock.acquire()
        self.addCleanup(observer._poll_lock.release)
        with self.assertRaisesRegex(RuntimeError, "already active"):
            observer.poll_once(fake)

    def test_remote_clock_step_is_diagnostics_then_recovery(self):
        observer, fake, clock = self.make()
        observer.poll_once(fake)
        self.assertTrue(observer.take_latest().snapshot.context.timing_verified)
        clock.step(.1)
        fake.outcomes = [dict(server_at=clock.wall + .106, server_boot_id="pi-one"),
                         state(at=clock.wall + .012)]
        observer.poll_once(fake)
        failed = observer.take_latest().snapshot
        self.assertIsNotNone(failed)
        self.assertFalse(failed.context.timing_verified)
        self.assertFalse(failed.inputs[0].context.fresh)
        self.assertEqual(observer.health()["calibration_failed"], 1)
        observer.poll_once(fake)
        self.assertTrue(observer.take_latest().snapshot.context.timing_verified)

    def test_restart_between_probe_and_state_is_untrusted(self):
        observer, fake, _ = self.make([
            dict(server_at=1000.006, server_boot_id="pi-one"), state(at=1000.012, boot="pi-two")])
        observer.poll_once(fake)
        result = observer.take_latest().snapshot
        self.assertFalse(result.context.timing_verified)
        self.assertEqual(result.context.robot_boot_id, "pi-two")
        self.assertIsNone(observer._clock)

    def test_bad_probe_still_fetches_state_and_state_errors_replace_pending_good(self):
        observer, fake, _ = self.make()
        observer.poll_once(fake)
        fake.outcomes = [dict(server_at=None), state(at=1000.03)]
        observer.poll_once(fake)
        self.assertFalse(observer.take_latest().snapshot.context.timing_verified)
        fake.outcomes = [dict(server_at=1000.03, server_boot_id="pi-one"), {"brain": {}}]
        observer.poll_once(fake)
        self.assertIsNone(observer.take_latest().snapshot)
        self.assertEqual(observer.health()["failed"], 1)
        self.assertGreaterEqual(observer.health()["replaced"], 1)

    def test_real_httpx_streaming_api_uses_only_allowlisted_gets(self):
        observer, _, clock = self.make()
        requests = []

        def handle(request):
            requests.append(request)
            clock.step(.006)
            payload = (dict(server_at=clock.wall, server_boot_id="pi-one")
                       if request.url.path == "/sim/clock" else state(at=clock.wall))
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))

        with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False,
                          follow_redirects=False, timeout=.5) as client:
            observer.poll_once(client)
        result = observer.take_latest()
        self.assertIsNotNone(result.snapshot)
        self.assertTrue(result.snapshot.context.timing_verified)
        self.assertEqual([(request.method, request.url.path) for request in requests],
                         [("GET", "/sim/clock"), ("GET", "/sim/state")])
        self.assertTrue(all(request.headers["Accept-Encoding"] == "identity" for request in requests))

    def test_rejected_probe_body_is_not_echoed_to_diagnostics(self):
        observer, fake, _ = self.make([dict(server_at="PRIVATE INPUT", server_boot_id="pi-one"), state()])
        observer.poll_once(fake)
        self.assertNotIn("PRIVATE", json.dumps(observer.health()))

    def test_close_bounded_and_late_request_cannot_restore_good_result(self):
        observer, fake, _ = self.make()
        entered, release = threading.Event(), threading.Event()
        original = fake.stream

        def blocked(method, url, **kwargs):
            entered.set()
            release.wait(1)
            return original(method, url, **kwargs)

        fake.stream = blocked
        observer.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        observer.close(timeout=.01)
        self.assertLess(time.monotonic() - started, .1)
        release.set()
        observer._thread.join(1)
        self.assertIsNone(observer.take_latest().snapshot)
        self.assertFalse(observer.health()["running"])

    def test_destination_frequency_and_timeout_bounds(self):
        for url in ("", "https://example.com", "http://localhost/base", "http://localhost?x=1",
                    "http://user:secret@localhost"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                HTTPObserver(url)
        for options in (dict(poll_hz=5.01), dict(poll_hz=0), dict(poll_hz=True),
                        dict(timeout_s=10), dict(timeout_s=0)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.make(**options)


if __name__ == "__main__":
    unittest.main()
