"""Pi-owned ALSA capture/playback with explicit simulation and honest results.

Missing tools never become synthetic healthy devices. No imports enable the HAT
amplifier or change ALSA/GPIO configuration: see docs/audio-acceptance.md.
"""

from __future__ import annotations

import getpass
import glob
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

from shared.audio import ERROR_LIMIT, attenuate_wav, bounded_error, run_audio_process, sound_wav, wav_duration
from .speaker_hat import RobotHatV4, silence_wav

logger = logging.getLogger("picrawler.audio")
_ARECORD = shutil.which("arecord")
_APLAY = shutil.which("aplay")


class PiMic:
    """Capture mono PCM. Suppression deliberately discards speaker echo."""

    def __init__(self, rate: int = 16000, device: str | None = None,
                 simulate: bool = False, suppress: Callable[[], bool] | None = None) -> None:
        self.rate = int(rate)
        self.device = device or None
        self.simulate = bool(simulate)
        self.suppress = suppress or (lambda: False)
        self._reason = "explicit simulation: silent stream" if simulate else (
            "arecord unavailable" if _ARECORD is None else "capture not tested")
        self._timestamp: float | None = None
        self._exit_code: int | None = None
        self._capturing = False
        self._suppressed = False

    @property
    def available(self) -> bool:
        return self.simulate or _ARECORD is not None

    def health(self) -> dict:
        age = None if self._timestamp is None else max(0.0, time.time() - self._timestamp)
        return {
            "available": self.available, "provenance": "simulated" if self.simulate else "real",
            "timestamp": self._timestamp, "age_s": age,
            "captured_at": self._timestamp, "failure_reason": self._reason,
            "source": f"ALSA capture {self.device or 'default'}", "max_age_s": 2,
            "fresh": age is not None and age < 2.0 and self._capturing,
            "confidence": None, "reason": self._reason, "device": self.device or "default",
            "capturing": self._capturing, "suppressed": self._suppressed,
            "exit_code": self._exit_code,
        }

    def input_health(self):
        from shared.health import InputHealth
        return InputHealth(
            available=self.available, provenance="simulated" if self.simulate else "real",
            captured_at=self._timestamp, max_age_s=2, failure_reason=self._reason,
            source=f"ALSA capture {self.device or 'default'}", used=self._capturing and not self._suppressed,
            ignored_reason="speaker playback/tail" if self._suppressed else self._reason,
        )

    def stream(self, chunk: int = 4096):
        """Yield S16_LE mono. Missing/failed real capture terminates, never fakes it."""
        if not self.available:
            return
        if self.simulate:
            self._capturing = True
            try:
                while True:
                    self._timestamp = time.time()
                    yield b"\x00" * chunk
                    time.sleep(chunk / (self.rate * 2))
            finally:
                self._capturing = False
            return
        command = [_ARECORD, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(self.rate), "-c", "1"]
        if self.device:
            command += ["-D", self.device]
        proc = None
        with tempfile.TemporaryFile() as errors:
            try:
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
                self._capturing = True
                while True:
                    data = proc.stdout.read(chunk)
                    if not data:
                        self._exit_code = proc.wait(timeout=1)
                        errors.seek(0)
                        self._reason = bounded_error(errors.read(ERROR_LIMIT)) or "microphone stream ended"
                        break
                    self._timestamp = time.time()
                    self._suppressed = bool(self.suppress())
                    self._reason = "ignored during speaker playback/tail" if self._suppressed else None
                    # Consume the device continuously so echoed audio does not queue.
                    yield b"\x00" * len(data) if self._suppressed else data
            except (OSError, subprocess.SubprocessError) as exc:
                self._reason = bounded_error(exc)
                logger.warning("microphone failed: %s", self._reason)
            finally:
                self._capturing = False
                if proc is not None:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                    if proc.stdout:
                        proc.stdout.close()


class PiSpeaker:
    """Serialized short WAV playback. Success means aplay exited zero, not audible proof."""

    def __init__(self, device: str | None = None, simulate: bool = False, *, backend="alsa",
                 verified_board=None, expected_pin=None, gain=None) -> None:
        self.device = device or None
        self.simulate = bool(simulate)
        self.backend = backend
        self._configuration_error = None
        self._hat = None
        self.gain = .5 if gain is None and backend == "robot_hat_v4" else (1. if gain is None else gain)
        try:
            if type(self.gain) is bool:
                raise ValueError("output gain must be finite and between 0 and 1")
            self.gain = float(self.gain)
            if not math.isfinite(self.gain) or not 0 <= self.gain <= 1:
                raise ValueError("output gain must be finite and between 0 and 1")
            if backend not in {"alsa", "robot_hat_v4"}:
                raise ValueError("unknown speaker backend; no fallback")
            if backend == "robot_hat_v4":
                if verified_board != "robot_hat_v4":
                    raise ValueError("verified Robot HAT v4 board attestation required")
                if type(expected_pin) is str and expected_pin.isdecimal():
                    expected_pin = int(expected_pin)
                if type(expected_pin) is not int or not 0 <= expected_pin <= 53:
                    raise ValueError("explicit verified speaker enable pin required")
                if self.device != "plug:softvol":
                    raise ValueError("verified plug:softvol ALSA route required; no default/PulseAudio fallback")
                self._hat = RobotHatV4(verified_board=verified_board, expected_pin=expected_pin)
        except (TypeError, ValueError) as exc:
            self._configuration_error = str(exc)
            self.gain = None  # invalid/nonfinite configuration must remain finite JSON
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process = None
        self._cancel = threading.Event()
        self._closed = False
        self._playing = False
        self._quiet_until = 0.0
        self._sessions: dict[str, int] = {}
        self._active_session: str | None = None
        self.last_result = self._result(False, "playback not tested")

    def _result(self, played: bool, reason: str | None, exit_code: int | None = None) -> dict:
        return {"ok": played, "played": played, "simulate": self.simulate,
                "reason": bounded_error(reason) if reason else None,
                "exit_code": exit_code, "timestamp": time.time(), "audible_verified": False,
                "backend": self.backend, "device": self.device or "default", "output_gain": self.gain}

    @property
    def available(self) -> bool:
        return not self._closed and (self.simulate or (_APLAY is not None and self._configuration_error is None
                                 and (self._hat is None or self._hat.metadata_verified and self._hat.fault is None)))

    @property
    def playing(self) -> bool:
        return self._playing

    def suppressed(self) -> bool:
        return self._playing or time.monotonic() < self._quiet_until

    def health(self) -> dict:
        reason = "speaker closed" if self._closed else "explicit simulation: no sound" if self.simulate else (
            self._configuration_error or ("aplay unavailable" if _APLAY is None else self.last_result["reason"]))
        return {"available": self.available, "provenance": "simulated" if self.simulate else "real",
                "timestamp": self.last_result["timestamp"], "confidence": None,
                "captured_at": self.last_result["timestamp"], "failure_reason": reason,
                "source": f"ALSA playback {self.device or 'default'}", "max_age_s": 60,
                "reason": reason, "device": self.device or "default", "playing": self.playing,
                "listening_suppressed": self.suppressed(), "last_result": dict(self.last_result),
                "audible_verified": False,
                "backend": self.backend, "output_gain": self.gain,
                "peak_limit": .25 if self._hat else None,
                "amplifier": self._hat.state if self._hat else
                    "unverified; board-specific enable and routing require hardware acceptance",
                "board_attestation": self._hat.verified_board if self._hat else None,
                "expected_enable_pin": self._hat.expected_pin if self._hat else None,
                "board_metadata_verified": self._hat.metadata_verified if self._hat else False,
                "amplifier_fault": self._hat.fault if self._hat else None}

    def input_health(self):
        from shared.health import InputHealth
        return InputHealth(
            available=self.available, provenance="simulated" if self.simulate else "real",
            captured_at=self.last_result["timestamp"], max_age_s=60,
            failure_reason=self.health()["reason"], source=f"ALSA playback {self.device or 'default'}",
            used=self._playing, ignored_reason=self.health()["reason"],
        )

    def _register_process(self, proc: subprocess.Popen) -> None:
        with self._state_lock:
            self._process = proc
            if self._cancel.is_set():
                proc.terminate()

    def _accept_sequence(self, session_id: str | None, sequence: int | None) -> str | None:
        """Called under state lock. Never evict a tombstone and allow an old request."""
        if session_id is None and sequence is None:
            return None  # Pi-local self-test/global stop
        if not session_id or len(session_id) > 128 or type(sequence) is not int or not 1 <= sequence < 2**63:
            return "invalid audio session/sequence"
        if session_id not in self._sessions and len(self._sessions) >= 64:
            return "audio session capacity reached; restart required"
        if sequence <= self._sessions.get(session_id, 0):
            return "stale or duplicate audio sequence rejected"
        self._sessions[session_id] = sequence
        return None

    def stop(self, session_id: str | None = None, sequence: int | None = None) -> dict:
        with self._state_lock:
            reason = self._accept_sequence(session_id, sequence)
            if reason:
                return {"ok": False, "reason": reason}
            if session_id is None or session_id == self._active_session:
                self._cancel.set()
                if self._process is not None and self._process.poll() is None:
                    try:
                        self._process.terminate()
                    except ProcessLookupError:
                        pass  # already exited; the playback owner still reaps it
                    except OSError as exc:
                        return {"ok": False, "reason": "player cancellation failed: " + bounded_error(exc)}
        return {"ok": True, "reason": "playback cancellation requested"}

    def close(self, timeout=5.0):
        with self._state_lock:
            self._closed = True
        stop = self.stop()
        if not self._lock.acquire(timeout=timeout):
            return {"ok": False, "reason": "playback/amplifier cleanup still pending at shutdown"}
        try:
            if self._hat and self._hat.fault:
                return {"ok": False, "reason": "amplifier cleanup failed: " + self._hat.fault}
            return stop
        finally:
            self._lock.release()

    def _publish(self, result: dict) -> dict:
        self.last_result = result
        return result

    def play(self, wav_bytes: bytes, session_id: str | None = None, sequence: int | None = None) -> bool:
        """Compatibility wrapper; network handlers must return play_result() directly."""
        return self.play_result(wav_bytes, session_id, sequence)["ok"]

    def play_result(self, wav_bytes: bytes, session_id: str | None = None, sequence: int | None = None,
                    authorize: Callable[[], str | None] | None = None) -> dict:
        """Return this request's immutable-by-convention outcome, not shared status."""
        try:
            duration = wav_duration(wav_bytes)
        except ValueError as exc:
            return self._publish(self._result(False, str(exc)))
        # Do not queue stale utterances behind an existing one.
        if not self._lock.acquire(blocking=False):
            return self._publish(self._result(False, "speaker busy; audio dropped"))
        hat_owned = False
        try:
            with self._state_lock:
                if self._closed:
                    return self._publish(self._result(False, "speaker closed"))
                if authorize:
                    reason = authorize()
                    if reason:
                        return self._publish(self._result(False, reason))
                reason = self._accept_sequence(session_id, sequence)
                if reason:
                    return self._publish(self._result(False, reason))
                self._active_session = session_id
                self._cancel.clear()
            if self.simulate:
                return self._publish(self._result(False, "explicit simulation: no sound emitted"))
            if self._configuration_error:
                return self._publish(self._result(False, self._configuration_error))
            if _APLAY is None:
                return self._publish(self._result(False, "aplay unavailable"))
            if self.gain == 0:
                return self._publish(self._result(False, "output gain is zero; muted"))
            if self._hat:
                try:
                    hat_owned = self._hat.acquire()
                except (OSError, ImportError) as exc:
                    return self._publish(self._result(False, "speaker ownership: " + bounded_error(exc)))
                if not hat_owned:
                    return self._publish(self._result(False, "Robot HAT speaker busy; audio dropped"))
                return self._play_hat(wav_bytes, duration)
            self._playing = True
            command = [_APLAY, "-q"]
            if self.device:
                command += ["-D", self.device]
            code, error = run_audio_process(
                command, data=attenuate_wav(wav_bytes, self.gain, peak_limit=1) if self.gain != 1 else wav_bytes,
                timeout=min(25, duration + 3), on_start=self._register_process,
            )
            played = code == 0 and not self._cancel.is_set()
            reason = None if played else ("playback cancelled" if self._cancel.is_set() else
                                         error or f"aplay exited {code}")
            result = self._publish(self._result(played, reason, code))
            if not played:
                logger.warning("speaker playback failed: %s", result["reason"])
            return result
        finally:
            if hat_owned:
                self._hat.release()
            with self._state_lock:
                self._process = None
                self._active_session = None
            self._playing = False
            self._quiet_until = time.monotonic() + .7
            self._lock.release()

    def _play_hat(self, wav_bytes, duration):
        """One locked transaction, including checked cleanup after partial enable."""
        attempted_enable = False
        code = None
        played = False
        cleanup_ok = None
        errors = []
        stage = "board verification"
        try:
            self._hat.prepare()
            audio = attenuate_wav(wav_bytes, self.gain)
            fill = silence_wav()
            stage = "amplifier ownership readback"
            self._hat.verify_level(False, self._run_hat_process)
            self._playing = True  # suppress listening through priming and cleanup
            if self._cancel.is_set():
                raise RuntimeError("playback cancelled before amplifier enable")
            stage = "amplifier enable"
            attempted_enable = True
            self._hat.set_enabled(True, self._run_hat_process)
            stage = "I2S prefill"
            code, error = self._run_hat_process([_APLAY, "-q", "-D", self.device], data=fill, timeout=2)
            if code != 0:
                raise RuntimeError(error or f"I2S prefill exited {code}")
            stage = "playback"
            code = None
            code, error = self._run_hat_process([_APLAY, "-q", "-D", self.device],
                                               data=audio, timeout=min(25, duration+3))
            played = code == 0 and not self._cancel.is_set()
            if not played:
                raise RuntimeError("playback cancelled" if self._cancel.is_set() else error or f"aplay exited {code}")
        except Exception as exc:
            errors.append(f"{stage}: {bounded_error(exc)}")
        finally:
            if attempted_enable:
                try:
                    # Cancellation must never terminate the disabler. It has
                    # independent finite timeouts and remains inside the lock.
                    self._hat.set_enabled(False, run_audio_process)
                    cleanup_ok = True
                except Exception as exc:
                    cleanup_ok = False
                    self._hat.state = "unknown"
                    self._hat.fault = bounded_error(exc)
                    errors.append("amplifier disable: " + self._hat.fault)
        result = self._result(played, "; ".join(errors) or None, code)
        result.update(ok=played and cleanup_ok is True, playback_succeeded=played,
                      cleanup_ok=cleanup_ok, amplifier=self._hat.state)
        return self._publish(result)

    def _run_hat_process(self, command, **kwargs):
        if self._cancel.is_set():
            return None, "playback cancelled"
        return run_audio_process(command, on_start=self._register_process, **kwargs)

    def self_test(self, play: bool = False, *, authorize: Callable[[], str | None] | None = None) -> dict:
        """Report readiness without sound by default; explicit play emits one quiet cue."""
        report = {"speaker": self.health(), "user": getpass.getuser(), "uid": os.geteuid(),
                  "groups": os.getgroups(), "play_requested": play,
                  "hardware_validation": "pending; process success cannot prove audible output"}
        report["alsa"] = {}
        if not self.simulate:
            for name, binary in (("playback_devices", _APLAY), ("capture_devices", _ARECORD)):
                if binary:
                    code, output = run_audio_process([binary, "-l"], timeout=3, capture_output=True)
                    report["alsa"][name] = {"exit_code": code, "output": output}
                else:
                    report["alsa"][name] = {"exit_code": None, "output": "tool unavailable"}
        report["configuration"] = {}
        for path in ("/proc/asound/cards", "/etc/asound.conf", "/boot/firmware/config.txt", "/boot/config.txt"):
            try:
                contents = Path(path).read_text()
                if path.endswith("config.txt"):
                    contents = "\n".join(line for line in contents.splitlines()
                                         if any(word in line.lower() for word in ("audio", "i2s", "hifiberry", "voicehat")))
                report["configuration"][path] = bounded_error(contents)
            except OSError as exc:
                report["configuration"][path] = bounded_error(exc)
        report["device_permissions"] = {
            path: {"readable": os.access(path, os.R_OK), "writable": os.access(path, os.W_OK)}
            for path in glob.glob("/dev/snd/*")[:32]
        }
        if play:
            # Diagnostics can be slow. A network lifecycle guard is checked at
            # the existing playback admission lock after those diagnostics.
            self.play_result(sound_wav("acknowledge"), authorize=authorize)
            report["speaker"] = self.health()
        return report


def main() -> int:
    """No sound or hardware mutation unless the operator explicitly chooses --play."""
    import argparse

    parser = argparse.ArgumentParser(description="Pi audio diagnostics; read-only unless --play")
    parser.add_argument("--play", action="store_true", help="explicitly play a short, quiet self-test")
    parser.add_argument("--simulate", action="store_true", help="report explicit silent simulation")
    parser.add_argument("--device", default=os.environ.get("PICRAWLER_SPEAKER_DEVICE"))
    parser.add_argument("--backend", default=os.environ.get("PICRAWLER_SPEAKER_BACKEND", "alsa"))
    parser.add_argument("--verified-board", default=os.environ.get("PICRAWLER_SPEAKER_VERIFIED_BOARD"))
    parser.add_argument("--expected-pin", default=os.environ.get("PICRAWLER_SPEAKER_EXPECTED_PIN"))
    parser.add_argument("--gain", default=os.environ.get("PICRAWLER_SPEAKER_GAIN"))
    args = parser.parse_args()
    speaker = PiSpeaker(device=args.device, simulate=args.simulate, backend=args.backend,
                        verified_board=args.verified_board, expected_pin=args.expected_pin, gain=args.gain)
    print(json.dumps(speaker.self_test(play=args.play), indent=2))
    return 0 if not args.play or speaker.last_result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
