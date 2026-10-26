"""Opt-in HAT playback lifecycle with fake metadata/GPIO/ALSA; no devices."""
import io
import asyncio
import json
import os
import struct
import subprocess
import sys
import threading
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import Mock, patch

from robot.audio import PiSpeaker
from shared.audio import attenuate_wav, sound_wav, wav_duration


class HatSpeakerTests(unittest.TestCase):
    def setUp(self):
        self.device = SimpleNamespace(spk_en=20, uuid="", name="")
        self.vendor = SimpleNamespace(__device__=self.device, Devices=SimpleNamespace(DEVICES={
            "robot_hat_v4x": {"speaker_enbale_pin": 20, "uuid": None}}))
        self.calls = []
        self.level = False
        self.failure = None
        self.output = None
        self.before = None
        for target, value in (("robot.audio._APLAY", "/fake/aplay"),
                              ("robot.speaker_hat.installed_version", Mock(return_value="2.5.3")),
                              ("robot.speaker_hat.import_module", Mock(return_value=self.vendor)),
                              ("robot.speaker_hat.shutil.which", Mock(return_value="/fake/pinctrl")),
                              ("robot.audio.run_audio_process", Mock(side_effect=self.run_process))):
            active = patch(target, value)
            active.start()
            self.addCleanup(active.stop)

    def make(self, **changes):
        return PiSpeaker(**{ "backend": "robot_hat_v4", "verified_board": "robot_hat_v4",
            "expected_pin": 20, "device": "plug:softvol", **changes})

    def run_process(self, command, **options):
        self.calls.append((command, options))
        if self.before:
            self.before(command, options)
        if self.failure:
            result = self.failure(command, options)
            if result is not None:
                return result
        if command[1] == "set":
            self.level = command[-1] == "dh"
        if command[1] == "get":
            return 0, self.output or f"20: op {'dh' if self.level else 'dl'} pn | {'hi' if self.level else 'lo'} // GPIO20 = output"
        return 0, ""

    def test_construction_health_disabled_gain_and_simulation_never_import_vendor_or_run_gpio(self):
        with patch("robot.speaker_hat.import_module") as vendor:
            speaker = self.make()
            self.assertFalse(speaker.health()["available"])
            self.assertEqual(self.calls, [])
            speaker.self_test(play=False)
            vendor.assert_not_called()
            # Listing ALSA devices is read-only, not amplifier control.
            self.assertFalse(any(cmd[0].endswith("pinctrl") for cmd, _ in self.calls))
            self.calls.clear()
            self.assertFalse(self.make(simulate=True).play_result(sound_wav())["played"])
            self.assertFalse(self.make(gain=0).play_result(sound_wav())["played"])
            self.assertEqual(self.calls, [])
            vendor.assert_not_called()

    def test_configuration_and_wrong_board_pin_version_fail_without_gpio(self):
        for changes in ({"verified_board": ""}, {"expected_pin": None}, {"expected_pin": True},
                        {"device": "default"}, {"device": "softvol"}, {"device": "plug:default"},
                        {"device": "plug:pulse"}, {"backend": "unknown"},
                        {"gain": float("nan")}, {"gain": 2}, {"gain": "bad"}):
            with self.subTest(changes=changes):
                speaker = self.make(**changes)
                self.assertFalse(speaker.play_result(sound_wav())["ok"])
                json.dumps(speaker.health(), allow_nan=False)
        self.device.spk_en = 12
        self.assertFalse(self.make().play_result(sound_wav())["ok"])
        self.device.spk_en = 20
        self.device.uuid = "v5-or-unknown"
        self.assertFalse(self.make().play_result(sound_wav())["ok"])
        self.device.uuid = ""
        with patch("robot.speaker_hat.installed_version", return_value="2.5.4"):
            self.assertFalse(self.make().play_result(sound_wav())["ok"])
        self.assertEqual(self.calls, [])

    def test_verified_enable_fill_play_disable_uses_configured_route_and_honest_results(self):
        speaker = self.make()
        result = speaker.play_result(sound_wav())
        self.assertTrue(result["ok"])
        self.assertTrue(result["played"])
        self.assertTrue(result["cleanup_ok"])
        self.assertFalse(result["audible_verified"])
        self.assertEqual(result["amplifier"], "low verified")
        self.assertEqual([cmd[1] for cmd, _ in self.calls], ["get", "set", "get", "-q", "-q", "set", "get"])
        players = [(cmd, options) for cmd, options in self.calls if cmd[0].endswith("aplay")]
        self.assertEqual(len(players), 2)
        self.assertTrue(all(cmd[-2:] == ["-D", "plug:softvol"] for cmd, _ in players))
        self.assertEqual(wav_duration(players[0][1]["data"]), .5)
        with wave.open(io.BytesIO(players[0][1]["data"]), "rb") as wav:
            self.assertEqual(set(wav.readframes(wav.getnframes())), {0})
        self.assertLessEqual(players[1][1]["timeout"], 25)
        self.assertFalse(self.level)

    def test_enable_and_prefill_failures_still_disable_and_never_play_payload(self):
        for failed in ("enable", "readback", "fill"):
            self.calls.clear()
            def failure(cmd, options):
                if ((failed == "enable" and cmd[-1] == "dh") or
                    (failed == "readback" and cmd[1] == "get" and self.level) or
                    (failed == "fill" and cmd[0].endswith("aplay"))):
                    return None, "timed out"
            self.failure = failure
            result = self.make().play_result(sound_wav())
            self.assertFalse(result["ok"])
            self.assertTrue(result["cleanup_ok"])
            self.assertFalse(self.level)
            self.assertLessEqual(sum(cmd[0].endswith("aplay") for cmd, _ in self.calls), 1)
            self.assertIn("timed out", result["reason"])

    def test_payload_timeout_and_exception_disable_the_amplifier(self):
        for outcome in ((None, "timed out"), (7, "ALSA failed"), RuntimeError("process raised")):
            count = 0
            def failure(cmd, options):
                nonlocal count
                if cmd[0].endswith("aplay"):
                    count += 1
                    if count == 2:
                        if isinstance(outcome, Exception):
                            raise outcome
                        return outcome
            self.failure = failure
            result = self.make().play_result(sound_wav())
            self.assertFalse(result["ok"])
            self.assertTrue(result["cleanup_ok"])
            self.assertFalse(self.level)

    def test_cleanup_failure_is_not_success_and_inhibits_next_play(self):
        self.failure = lambda cmd, opts: (1, "GPIO off denied") if cmd[-1] == "dl" else None
        speaker = self.make()
        result = speaker.play_result(sound_wav())
        self.assertTrue(result["played"], "player succeeded; cleanup did not")
        self.assertFalse(result["ok"])
        self.assertFalse(result["cleanup_ok"])
        self.assertFalse(speaker.available)
        count = len(self.calls)
        self.assertFalse(speaker.play_result(sound_wav())["ok"])
        self.assertEqual(len(self.calls), count)

    def test_wrong_readback_fails_closed_and_reports_unconfirmed_cleanup(self):
        def before(cmd, options):
            if cmd[1] == "set":
                self.output = "12: op dh pn | hi // wrong pin"
        self.before = before
        result = self.make().play_result(sound_wav())
        self.assertFalse(result["ok"])
        self.assertFalse(result["cleanup_ok"])
        self.assertEqual(result["amplifier"], "unknown")

    def test_already_high_amplifier_is_not_taken_over_or_disabled(self):
        self.level = True
        result = self.make().play_result(sound_wav())
        self.assertFalse(result["ok"])
        self.assertIsNone(result["cleanup_ok"])
        self.assertTrue(self.level)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][1], "get")

    def test_cancellation_terminates_owned_player_but_never_cancels_disable(self):
        entered, release = threading.Event(), threading.Event()
        child = Mock()
        child.poll.return_value = None
        count = 0
        def before(cmd, options):
            nonlocal count
            if cmd[0].endswith("aplay"):
                count += 1
                if count == 2:
                    options["on_start"](child)
                    entered.set()
                    self.assertTrue(release.wait(2))
        self.before = before
        speaker = self.make()
        result = []
        worker = threading.Thread(target=lambda: result.append(speaker.play_result(sound_wav(), "one", 1)))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(speaker.stop("one", 2)["ok"])
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        child.terminate.assert_called_once()
        self.assertFalse(result[0]["ok"])
        self.assertTrue(result[0]["cleanup_ok"])
        self.assertFalse(self.level)
        self.assertNotIn("on_start", self.calls[-2][1])
        self.assertFalse(speaker.play_result(sound_wav(), "one", 1)["ok"])

    def test_close_is_idle_without_gpio_and_rejects_late_playback(self):
        speaker = self.make()
        self.assertTrue(speaker.close()["ok"])
        self.assertFalse(speaker.available)
        self.assertFalse(speaker.play_result(sound_wav())["ok"])
        self.assertEqual(self.calls, [])

    def test_close_reports_pending_cleanup_and_playback_eventually_disables(self):
        entered, release = threading.Event(), threading.Event()
        def before(cmd, options):
            if cmd[0].endswith("aplay"):
                entered.set()
                self.assertTrue(release.wait(2))
        self.before = before
        speaker = self.make()
        results = []
        worker = threading.Thread(target=lambda: results.append(speaker.play_result(sound_wav())))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(speaker.close(timeout=.01)["ok"])
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]["cleanup_ok"])
        self.assertFalse(results[0]["ok"])
        self.assertFalse(self.level)

    def test_another_process_lock_blocks_profile_before_any_gpio(self):
        import fcntl
        path = f"/tmp/crab-speaker-hat-v4-{os.geteuid()}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.make().play_result(sound_wav())
            self.assertIn("busy", result["reason"])
            self.assertEqual(self.calls, [])
        finally:
            os.close(fd)

    def test_concurrent_instances_cannot_overlap_amplifier_transactions(self):
        entered, release = threading.Event(), threading.Event()
        def before(cmd, options):
            if cmd[0].endswith("aplay"):
                entered.set()
                self.assertTrue(release.wait(2))
        self.before = before
        first, second = self.make(), self.make()
        result = []
        worker = threading.Thread(target=lambda: result.append(first.play_result(sound_wav())))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            before_count = len(self.calls)
            busy = second.play_result(sound_wav())
            self.assertIn("busy", busy["reason"])
            self.assertEqual(len(self.calls), before_count)
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(result[0]["ok"])

    def test_attenuation_caps_full_scale_without_changing_wav_format(self):
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(struct.pack("<hhhh", -32768, 32767, -16000, 16000))
        output = attenuate_wav(data.getvalue(), .5)
        with wave.open(io.BytesIO(output), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getframerate()), (2, 44100))
            samples = struct.unpack("<hhhh", wav.readframes(2))
        self.assertLessEqual(max(map(abs, samples)), 8191)
        self.assertLess(abs(samples[2]), abs(samples[0]))

    def test_disabled_server_constructs_no_speaker_profile(self):
        script = """
from unittest.mock import patch
with patch('robot.audio.PiSpeaker', side_effect=AssertionError('speaker constructed while disabled')):
    from robot import server
    assert server.speaker is None
"""
        result = subprocess.run([sys.executable, "-c", script], env={**os.environ,
            "PICRAWLER_SIMULATE": "1", "PICRAWLER_ACTUATORS_ENABLED": "0", "PICRAWLER_HOME_ON_START": "none",
            "PICRAWLER_CAMERA_ENABLED": "0", "PICRAWLER_AUDIO_ENABLED": "0",
            "PICRAWLER_SPEAKER_BACKEND": "robot_hat_v4"}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_payload_success_with_cleanup_failure_is_service_failure(self):
        from robot import server
        from shared.protocol import AUDIO_SESSION_HEADER, AUDIO_SEQUENCE_HEADER
        async def stream():
            yield sound_wav()
        request = SimpleNamespace(headers={AUDIO_SESSION_HEADER: "fixture", AUDIO_SEQUENCE_HEADER: "1"},
                                  stream=stream)
        result = {"ok": False, "played": True, "simulate": False, "cleanup_ok": False}
        speaker = Mock(play_result=Mock(return_value=result))
        with patch.object(server, "speaker", speaker):
            response = asyncio.run(server.audio_play(request))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(json.loads(response.body)["cleanup_ok"])

    def test_explicit_cli_play_exits_failure_when_payload_succeeded_but_cleanup_failed(self):
        from contextlib import redirect_stdout
        from robot import audio
        speaker = Mock(last_result={"played": True, "ok": False, "cleanup_ok": False},
                       self_test=Mock(return_value={"speaker": {"cleanup_ok": False}}))
        with patch.object(audio, "PiSpeaker", return_value=speaker), \
             patch.object(sys, "argv", ["robot.audio", "--play"]), redirect_stdout(io.StringIO()):
            self.assertEqual(audio.main(), 1)


if __name__ == "__main__":
    unittest.main()
