"""Audio failure and coordination checks; no ALSA device or model is accessed."""
import io
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from brain.hearing import Ears
from brain.pet.voice import Voice
from robot.audio import PiMic, PiSpeaker
from shared.audio import ERROR_LIMIT, run_audio_process, sound_wav, wav_duration


class AudioTests(unittest.TestCase):
    def test_effects_are_deterministic_short_valid_wavs(self):
        for cue in ("acknowledge", "settle", "play"):
            self.assertEqual(sound_wav(cue), sound_wav(cue))
            self.assertLess(wav_duration(sound_wav(cue)), .5)
        with self.assertRaises(ValueError):
            wav_duration(b"not a WAV")

    def test_missing_devices_do_not_auto_simulate(self):
        with patch("robot.audio._APLAY", None), patch("robot.audio._ARECORD", None):
            speaker = PiSpeaker()
            mic = PiMic()
            self.assertFalse(speaker.simulate)
            self.assertFalse(speaker.available)
            self.assertFalse(speaker.play(sound_wav()))
            self.assertIn("unavailable", speaker.last_result["reason"])
            self.assertFalse(mic.simulate)
            self.assertEqual(list(mic.stream()), [])
            self.assertFalse(mic.health()["fresh"])

    def test_simulated_playback_does_not_claim_sound(self):
        speaker = PiSpeaker(simulate=True)
        with patch("robot.audio.run_audio_process") as runner:
            self.assertFalse(speaker.play(sound_wav()))
            runner.assert_not_called()
        self.assertTrue(speaker.last_result["simulate"])
        self.assertFalse(speaker.last_result["played"])

    def test_invalid_audio_never_reaches_aplay(self):
        with patch("robot.audio._APLAY", "/fake/aplay"), patch("robot.audio.run_audio_process") as run:
            self.assertFalse(PiSpeaker().play(b"invalid"))
            run.assert_not_called()

    def test_aplay_failure_is_bounded_and_observable(self):
        with patch("robot.audio._APLAY", "/fake/aplay"), patch(
            "robot.audio.run_audio_process", return_value=(1, "Permission denied " * 300)
        ):
            speaker = PiSpeaker()
            self.assertFalse(speaker.play(sound_wav()))
            self.assertEqual(speaker.last_result["exit_code"], 1)
            self.assertLessEqual(len(speaker.last_result["reason"]), ERROR_LIMIT)
            self.assertIn("Permission denied", speaker.last_result["reason"])

    def test_success_is_process_success_not_audible_verification(self):
        with patch("robot.audio._APLAY", "/fake/aplay"), patch(
            "robot.audio.run_audio_process", return_value=(0, "")
        ) as runner:
            speaker = PiSpeaker(device="robothat")
            self.assertTrue(speaker.play(sound_wav()))
            self.assertIn("robothat", runner.call_args.args[0])
            self.assertFalse(speaker.health()["audible_verified"])
            self.assertTrue(speaker.suppressed())

    def test_aplay_cancellation_terminates_owned_process(self):
        registered = threading.Event()
        release = threading.Event()
        child = Mock()
        child.poll.return_value = None
        def run(*args, **kwargs):
            kwargs["on_start"](child)
            registered.set()
            release.wait(2)
            return -15, "terminated"
        with patch("robot.audio._APLAY", "/fake/aplay"), patch("robot.audio.run_audio_process", side_effect=run):
            speaker = PiSpeaker()
            thread = threading.Thread(target=speaker.play, args=(sound_wav(),))
            thread.start()
            self.assertTrue(registered.wait(1))
            speaker.stop()
            release.set()
            thread.join(2)
            child.terminate.assert_called_once()
            self.assertFalse(speaker.last_result["ok"])
            self.assertIn("cancelled", speaker.last_result["reason"])

    def test_stop_fence_rejects_delayed_play_and_allows_newer_request(self):
        with patch("robot.audio._APLAY", "/fake/aplay"), patch(
            "robot.audio.run_audio_process", return_value=(0, "")
        ) as run:
            speaker = PiSpeaker()
            self.assertTrue(speaker.stop("session", 2)["ok"])
            self.assertFalse(speaker.play(sound_wav(), "session", 1))
            run.assert_not_called()
            self.assertIn("stale", speaker.last_result["reason"])
            self.assertTrue(speaker.play(sound_wav(), "session", 3))
            self.assertFalse(speaker.play(sound_wav(), "session", 3))
            self.assertEqual(run.call_count, 1)

    def test_concurrent_requests_keep_their_own_results(self):
        started, release = threading.Event(), threading.Event()
        results = {}
        def run(*args, **kwargs):
            started.set()
            release.wait(2)
            return 0, ""
        with patch("robot.audio._APLAY", "/fake/aplay"), patch("robot.audio.run_audio_process", side_effect=run):
            speaker = PiSpeaker()
            thread = threading.Thread(target=lambda: results.update(first=speaker.play_result(sound_wav())))
            thread.start()
            self.assertTrue(started.wait(1))
            results["busy"] = speaker.play_result(sound_wav())
            release.set()
            thread.join(2)
        self.assertTrue(results["first"]["played"])
        self.assertFalse(results["busy"]["played"])
        self.assertIn("busy", results["busy"]["reason"])

    def test_session_limit_fails_closed_without_evicting_stop_fence(self):
        speaker = PiSpeaker(simulate=True)
        for i in range(64):
            self.assertTrue(speaker.stop(f"session-{i}", 3)["ok"])
        self.assertFalse(speaker.stop("overflow", 1)["ok"])
        self.assertFalse(speaker.play(sound_wav(), "session-0", 1))
        self.assertIn("stale", speaker.last_result["reason"])

    def test_stop_after_play_acceptance_but_before_spawn_cancels_child(self):
        child = Mock()
        child.poll.return_value = None
        speaker = PiSpeaker()
        def run(*args, **kwargs):
            speaker.stop("session", 2)
            kwargs["on_start"](child)
            return -15, "terminated"
        with patch("robot.audio._APLAY", "/fake/aplay"), patch("robot.audio.run_audio_process", side_effect=run):
            self.assertFalse(speaker.play(sound_wav(), "session", 1))
            child.terminate.assert_called_once()

    def test_process_exit_diagnostic_and_timeout(self):
        code, error = run_audio_process([sys.executable, "-c", "import sys;sys.stderr.write('wrong device');sys.exit(9)"])
        self.assertEqual(code, 9)
        self.assertEqual(error, "wrong device")
        code, error = run_audio_process([sys.executable, "-c", "import time;time.sleep(1)"], timeout=.01)
        self.assertIsNone(code)
        self.assertIn("timed out", error)

    def test_default_self_test_never_plays(self):
        speaker = PiSpeaker(simulate=True)
        with patch.object(speaker, "play") as play:
            report = speaker.self_test()
            play.assert_not_called()
        self.assertFalse(report["play_requested"])
        self.assertIn("pending", report["hardware_validation"])

    def voice(self):
        return Voice(True, None, play_url="http://localhost/audio/play")

    def lease_response(self):
        return httpx.Response(200, json={"ok": True, "lease_id": "test-lease"},
                              request=httpx.Request("POST", "http://localhost/control/lease"))

    def test_effects_do_not_require_piper(self):
        with patch("brain.pet.voice.shutil.which", return_value=None):
            voice = self.voice()
        self.assertFalse(voice.enabled)
        self.assertTrue(voice.effects_enabled)
        self.assertFalse(voice.say("hello"))
        self.assertIn("Piper", voice.last_result["reason"])

    def test_pi_http_failure_not_success(self):
        voice = self.voice()
        response = httpx.Response(503, request=httpx.Request("POST", voice.play_url))
        with patch("brain.pet.voice.httpx.post", side_effect=[self.lease_response(), response]):
            voice._work("effect", sound_wav(), 0)
        self.assertFalse(voice.last_result["ok"])
        self.assertIn("503", voice.last_result["reason"])

    def test_pi_http_200_must_confirm_playback(self):
        for payload in ({"ok": False, "played": False, "simulate": True}, [], {"ok": True}):
            voice = self.voice()
            response = httpx.Response(200, json=payload, request=httpx.Request("POST", voice.play_url))
            with patch("brain.pet.voice.httpx.post", side_effect=[self.lease_response(), response]):
                voice._work("effect", sound_wav(), 0)
            self.assertFalse(voice.last_result["ok"])
        voice = self.voice()
        response = httpx.Response(200, json={"ok": True, "played": True, "simulate": False},
                                  request=httpx.Request("POST", voice.play_url))
        with patch("brain.pet.voice.httpx.post", side_effect=[self.lease_response(), response]):
            voice._work("effect", sound_wav(), 0)
        self.assertTrue(voice.last_result["ok"])

    def test_remote_stop_has_newer_sequence_than_playback(self):
        voice = self.voice()
        response = httpx.Response(200, json={"ok": True, "played": True, "simulate": False},
                                  request=httpx.Request("POST", voice.play_url))
        with patch("brain.pet.voice.httpx.post", side_effect=[self.lease_response(), response, response]) as post:
            voice._work("effect", sound_wav(), 0)
            play_headers = post.call_args.kwargs["headers"]
            voice.stop()  # URL unset so cancellation is local for now.
            voice.stop_url = "http://localhost/audio/stop"
            voice._stop_remote()
            stop_headers = post.call_args.kwargs["headers"]
        self.assertEqual(play_headers["X-Crab-Audio-Session"], stop_headers["X-Crab-Audio-Session"])
        self.assertGreater(int(stop_headers["X-Crab-Audio-Sequence"]), int(play_headers["X-Crab-Audio-Sequence"]))

    def test_failed_lease_never_dispatches_audio(self):
        voice = self.voice()
        response = httpx.Response(200, json={"ok": False, "reason": "busy"},
                                  request=httpx.Request("POST", "http://localhost/control/lease"))
        with patch("brain.pet.voice.httpx.post", return_value=response) as post:
            voice._work("effect", sound_wav(), 0)
        self.assertEqual(post.call_count, 1)
        self.assertFalse(voice.last_result["ok"])
        self.assertIn("lease", voice.last_result["reason"])

    def test_dispatch_authorization_rejects_pre_restart_or_stopped_work(self):
        with patch("robot.audio._APLAY", "/fake/aplay"), patch("robot.audio.run_audio_process") as run:
            speaker = PiSpeaker()
            result = speaker.play_result(sound_wav(), "session", 1,
                                         authorize=lambda: "lease from earlier boot or stop")
            self.assertFalse(result["played"])
            self.assertIn("lease", result["reason"])
            run.assert_not_called()

    def test_piper_failure_and_cancel_prevent_network_playback(self):
        voice = self.voice()
        voice._piper = "/fake/piper"
        voice._model = "/fake/voice.onnx"
        with patch("brain.pet.voice.run_audio_process", return_value=(2, "bad model")), patch(
            "brain.pet.voice.httpx.post"
        ) as post:
            voice._work("speech", "Hello", 0)
            self.assertEqual(voice.last_result["stage"], "tts")
            self.assertEqual(voice.last_result["exit_code"], 2)
            post.assert_not_called()
        voice.stop()
        with patch("brain.pet.voice.httpx.post") as post:
            voice._work("effect", sound_wav(), 0)
            post.assert_not_called()
            self.assertEqual(voice.last_result["stage"], "cancelled")

    def test_microphone_suppresses_speaker_and_resumes(self):
        proc = Mock()
        proc.stdout = io.BytesIO(b"\x01\x02" * 4)
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        suppress = Mock(side_effect=[True, False])
        with patch("robot.audio._ARECORD", "/fake/arecord"), patch("robot.audio.subprocess.Popen", return_value=proc):
            mic = PiMic(suppress=suppress)
            stream = mic.stream(chunk=4)
            self.assertEqual(next(stream), b"\x00" * 4)
            self.assertTrue(mic.health()["suppressed"])
            self.assertEqual(next(stream), b"\x01\x02" * 2)
            self.assertFalse(mic.health()["suppressed"])
            stream.close()

    def test_whisper_echo_suppression_before_and_after_inference(self):
        ears = object.__new__(Ears)
        ears.suppress = Mock(return_value=True)
        ears._transcribe = Mock(return_value="stop")
        ears.on_text = Mock()
        ears.wake = None
        ears._last_chunk_at = time.time()
        ears.playback_epoch = Mock(return_value=0)
        ears._emit(b"sample")
        ears._transcribe.assert_not_called()
        ears.suppress = Mock(side_effect=[False, True])
        ears._emit(b"sample")
        ears._transcribe.assert_called_once()
        ears.on_text.assert_not_called()

    def test_invalid_stale_and_playback_overlapping_utterances_are_ignored(self):
        ears = object.__new__(Ears)
        ears.suppress = Mock(return_value=False)
        ears._transcribe = Mock(return_value="hello")
        ears.on_text = Mock()
        ears.wake = None
        ears._last_chunk_at = time.time() - 10
        ears._emit(b"sample")
        self.assertIn("stale", ears._reason)
        ears._last_chunk_at = time.time()
        ears._emit(b"odd")
        self.assertIn("invalid", ears._reason)
        ears._transcribe.assert_not_called()
        ears.playback_epoch = Mock(side_effect=[0, 1])
        ears._emit(b"sample")
        ears.on_text.assert_not_called()
        self.assertIn("overlapped", ears._reason)

    def test_voice_close_rejects_new_work(self):
        voice = self.voice()
        voice.close()
        self.assertFalse(voice.sound("acknowledge"))
        self.assertIn("closed", voice.last_result["reason"])

    def test_whisper_never_downloads_at_runtime(self):
        model = Mock()
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=model), "numpy": Mock()}):
            Ears("http://localhost/audio/stream", "cached-base", Mock())
        self.assertTrue(model.call_args.kwargs["local_files_only"])


if __name__ == "__main__":
    unittest.main()
