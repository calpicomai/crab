"""Local speech/effects with observable results and bounded asynchronous work."""

from __future__ import annotations

import logging
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

from shared.audio import bounded_error, run_audio_process, sound_wav, wav_duration
from shared.protocol import AUDIO_LEASE_HEADER, AUDIO_SEQUENCE_HEADER, AUDIO_SESSION_HEADER, CONTROL_LEASE_PATH
from shared.local import require_local_url

logger = logging.getLogger("brain.voice")


class Voice:
    def __init__(self, enabled: bool, model: str | None, player: str = "aplay -q",
                 sink: str = "pi", play_url: str | None = None,
                 stop_url: str | None = None, simulate: bool = False) -> None:
        self._lock = threading.RLock()
        self._speaking = False
        self._quiet_until = 0.0
        self._generation = 0
        self._process = None
        self._thread = None
        self._closed = False
        self._stop_thread = None
        self._listening_epoch = 0
        self._session_id = uuid.uuid4().hex
        self._sequence = 0
        self._pending_stop_sequence = 0
        self.sink = sink
        self.play_url = require_local_url(play_url) if play_url else None
        self.stop_url = require_local_url(stop_url) if stop_url else None
        self.simulate = bool(simulate)
        self._model = model
        self._player = shlex.split(player) if player else []
        self._piper = shutil.which("piper") if enabled else None
        self._player_bin = shutil.which(self._player[0]) if enabled and self._player else None
        sink_ok = bool(play_url) if sink == "pi" else bool(sink == "local" and self._player_bin)
        self.effects_enabled = bool(enabled and (sink_ok or simulate))
        model_ok = bool(model and Path(model).is_file() and Path(model + ".json").is_file())
        self.enabled = bool(self.effects_enabled and self._piper and model_ok)
        missing = []
        if not enabled:
            missing.append("audio disabled")
        elif not sink_ok and not simulate:
            missing.append("playback sink unavailable")
        if enabled and not self._piper:
            missing.append("Piper executable unavailable")
        if enabled and not model_ok:
            missing.append("local Piper .onnx/.onnx.json model unavailable")
        self._readiness_reason = "; ".join(missing) or None
        self.last_result = {"ok": False, "stage": "readiness", "reason": self._readiness_reason or
                            "playback not tested", "timestamp": time.time(), "played": False}

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._speaking

    def listening_suppressed(self) -> bool:
        return self.busy or time.monotonic() < self._quiet_until

    @property
    def listening_epoch(self) -> int:
        """Changes whenever output starts, even if it ends before STT finishes."""
        with self._lock:
            return self._listening_epoch

    def health(self) -> dict:
        return {"enabled": self.enabled, "effects_enabled": self.effects_enabled,
                "busy": self.busy, "listening_suppressed": self.listening_suppressed(),
                "sink": self.sink, "simulate": self.simulate,
                "reason": self._readiness_reason, "last_result": dict(self.last_result),
                "closed": self._closed,
                "cancel_pending": bool(self._stop_thread and self._stop_thread.is_alive())}

    def _record(self, ok: bool, stage: str, reason: str | None = None, **extra) -> None:
        self.last_result = {"ok": ok, "stage": stage, "reason": bounded_error(reason) if reason else None,
                            "timestamp": time.time(), "played": ok, **extra}
        if reason:
            logger.warning("audio %s: %s", stage, bounded_error(reason))

    def _begin(self, target, argument) -> bool:
        with self._lock:
            if self._closed:
                self._record(False, "queue", "audio service closed")
                return False
            if self._speaking:
                self._record(False, "queue", "busy; audio dropped")
                return False
            self._speaking = True
            self._listening_epoch += 1
            self._sequence += 1
            generation = self._generation
            self._thread = threading.Thread(target=self._work, args=(target, argument, generation, self._sequence), daemon=True)
            self._thread.start()
        return True

    def say(self, text: str) -> bool:
        """Accept a short utterance, or return False with an inspectable reason."""
        if not self.enabled:
            self._record(False, "tts", self._readiness_reason or "TTS unavailable")
            return False
        if not text or len(text) > 500:
            self._record(False, "tts", "speech must contain 1–500 characters")
            return False
        return self._begin("speech", text)

    def sound(self, name: str) -> bool:
        if not self.effects_enabled:
            self._record(False, "effect", "audio output disabled/unavailable")
            return False
        try:
            wav = sound_wav(name)
        except ValueError as exc:
            self._record(False, "effect", str(exc))
            return False
        return self._begin("effect", wav)

    def _register(self, process, generation) -> None:
        with self._lock:
            self._process = process
            if generation != self._generation:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass  # it exited between registration and cancellation

    def _cancelled(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation

    def _work(self, kind: str, argument, generation: int, sequence: int | None = None) -> None:
        started = time.monotonic()
        if sequence is None:
            with self._lock:
                self._sequence += 1
                sequence = self._sequence
        try:
            wav_bytes = argument
            if kind == "speech":
                with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
                    code, error = run_audio_process(
                        [self._piper, "--model", self._model, "--output_file", wav.name],
                        data=argument.encode(), timeout=20,
                        on_start=lambda proc: self._register(proc, generation),
                    )
                    if self._cancelled(generation):
                        self._record(False, "cancelled", "speech cancelled")
                        return
                    if code != 0:
                        self._record(False, "tts", error or f"Piper exited {code}", exit_code=code)
                        return
                    wav.seek(0)
                    wav_bytes = wav.read(4 * 1024 * 1024 + 1)
            duration = wav_duration(wav_bytes)
            if self._cancelled(generation):
                self._record(False, "cancelled", "audio cancelled before playback")
                return
            if self.simulate:
                self._record(False, "simulated", "explicit simulation: no sound emitted", simulate=True)
                return
            if self.sink == "pi":
                parts = urlsplit(self.play_url)
                lease_url = urlunsplit((parts.scheme, parts.netloc, CONTROL_LEASE_PATH, "", ""))
                lease_response = httpx.post(lease_url, timeout=2, trust_env=False, follow_redirects=False)
                lease_response.raise_for_status()
                lease = lease_response.json()
                if (not isinstance(lease, dict) or lease.get("ok") is not True
                        or not isinstance(lease.get("lease_id"), str) or not 1 <= len(lease["lease_id"]) <= 128):
                    raise ValueError("Pi did not grant a fresh audio command lease")
                if self._cancelled(generation):
                    self._record(False, "cancelled", "audio cancelled while obtaining dispatch lease")
                    return
                response = httpx.post(self.play_url, content=wav_bytes,
                                      headers={"Content-Type": "audio/wav",
                                               AUDIO_LEASE_HEADER: lease["lease_id"],
                                               AUDIO_SESSION_HEADER: self._session_id,
                                               AUDIO_SEQUENCE_HEADER: str(sequence)}, timeout=duration + 5,
                                      trust_env=False, follow_redirects=False)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError("invalid Pi audio response: expected an object")
                if not (result.get("ok") is True and result.get("played") is True and
                        result.get("simulate") is False):
                    self._record(False, "playback", result.get("reason") or "Pi did not confirm real playback")
                    return
            else:
                with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
                    wav.write(wav_bytes)
                    wav.flush()
                    code, error = run_audio_process(
                        [*self._player, wav.name], timeout=duration + 3,
                        on_start=lambda proc: self._register(proc, generation),
                    )
                if code != 0:
                    self._record(False, "playback", error or f"player exited {code}", exit_code=code)
                    return
            if self._cancelled(generation):
                self._record(False, "cancelled", "audio cancelled; playback stop is best effort")
            else:
                self._record(True, "playback", elapsed_s=round(time.monotonic() - started, 4))
        except (httpx.HTTPError, ValueError, OSError, subprocess.SubprocessError) as exc:
            self._record(False, "playback" if kind == "effect" else "speech", bounded_error(exc))
        finally:
            with self._lock:
                self._process = None
                self._speaking = False
                self._quiet_until = time.monotonic() + .7

    def stop(self) -> None:
        """Cancel pending synthesis/local playback; request Pi cancellation asynchronously."""
        with self._lock:
            self._generation += 1
            self._sequence += 1
            self._pending_stop_sequence = self._sequence
            try:
                if self._process is not None and self._process.poll() is None:
                    self._process.terminate()
            except ProcessLookupError:
                pass  # poll/terminate is inherently racy; remote stop still wins
            except Exception as exc:
                self._record(False, "cancelled", f"local process cancellation unconfirmed: {bounded_error(exc)}")
        if self.stop_url and not self.simulate:
            with self._lock:
                if self._stop_thread is None or not self._stop_thread.is_alive():
                    self._stop_thread = threading.Thread(target=self._stop_remote, daemon=True)
                    self._stop_thread.start()

    def close(self) -> None:
        """Stop output and join only for a bounded interval; never stall behavior shutdown."""
        with self._lock:
            self._closed = True
        self.stop()
        deadline = time.monotonic()+1.0
        for thread in (self._thread, self._stop_thread):
            if thread and thread.ident is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline-time.monotonic()))

    def _stop_remote(self) -> None:
        while True:
            with self._lock:
                sequence = self._pending_stop_sequence
            try:
                response = httpx.post(self.stop_url, timeout=2, headers={
                    AUDIO_SESSION_HEADER: self._session_id, AUDIO_SEQUENCE_HEADER: str(sequence),
                }, trust_env=False, follow_redirects=False)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ValueError("Pi rejected playback cancellation")
            except (httpx.HTTPError, ValueError) as exc:
                self._record(False, "cancelled", f"Pi playback cancellation unconfirmed: {bounded_error(exc)}")
            with self._lock:
                if self._pending_stop_sequence == sequence:
                    self._stop_thread = None
                    return
