"""Ears — spoken-command input for the brain (Jetson side).

Pulls the Pi's mic stream (raw 16 kHz mono PCM from the robot's /audio/stream),
segments utterances with a simple energy gate, and transcribes each with
**faster-whisper** (local, CUDA or CPU) — the same device/compute split as the
camera (device on the Pi, model on the Jetson). Recognized text is handed to a
callback (the pet turns it into a command / feeds it to its mind).

Entirely optional and degradable: if faster-whisper (or numpy) isn't installed or
the mic stream is unreachable, ``build_ears`` returns None and the pet runs as
before. Runs on a background thread; ``stop()`` ends it.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from shared.local import require_local_url

logger = logging.getLogger("brain.hearing")

_SILENCE_RMS = 500      # int16 RMS below this is "silence"
_END_SILENCE_S = 0.6    # end an utterance after this much trailing silence
_MIN_UTTER_S = 0.3      # ignore blips shorter than this
_MAX_UTTER_S = 8.0      # hard cap so a noisy room can't buffer forever


class Ears:
    def __init__(self, stream_url: str, model: str, on_text, rate: int = 16000,
                 wake_word: str | None = None, device: str = "auto", compute_type: str = "int8",
                 suppress=None, playback_epoch=None) -> None:
        import numpy as np  # noqa: F401  (fail fast if numpy missing)
        from faster_whisper import WhisperModel

        self._np = np
        # Runtime never downloads a model. Pre-provision explicitly during setup.
        self._model = WhisperModel(model, device=device, compute_type=compute_type, local_files_only=True)
        self.stream_url = require_local_url(stream_url)
        self.on_text = on_text
        self.rate = rate
        self.wake = (wake_word or "").strip().lower() or None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.suppress = suppress or (lambda: False)
        self.playback_epoch = playback_epoch or (lambda: 0)
        self._reason: str | None = "microphone stream not connected"
        self._last_chunk_at: float | None = None
        self._last_transcription_s: float | None = None
        self._connected = False
        self._provenance = "unknown"

    def health(self) -> dict:
        age = None if self._last_chunk_at is None else max(0.0, time.time() - self._last_chunk_at)
        return {"available": True, "connected": self._connected, "provenance": self._provenance,
                "timestamp": self._last_chunk_at, "age_s": age,
                "fresh": self._connected and age is not None and age < 2,
                "ignored_reason": "speaker playback/tail" if self.suppress() else self._reason,
                "inference_s": self._last_transcription_s}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._connected = False
        self._reason = "listening stopped"
        if self._thread:
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------ #
    def _rms(self, pcm: bytes) -> float:
        if not pcm:
            return 0.0
        a = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32)
        return float(self._np.sqrt(self._np.mean(a * a))) if a.size else 0.0

    def _transcribe(self, pcm: bytes) -> str:
        audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        segments, _ = self._model.transcribe(audio, language="en", beam_size=1)
        return " ".join(s.text for s in segments).strip()

    def _emit(self, pcm: bytes) -> None:
        if not pcm or len(pcm) % 2:
            self._reason = "invalid 16-bit PCM utterance"
            return
        if self._last_chunk_at is None or time.time() - self._last_chunk_at > 2:
            self._reason = "stale microphone utterance ignored"
            return
        if self.suppress():
            self._reason = "utterance ignored during playback/tail"
            return
        try:
            epoch = self.playback_epoch()
            started = time.monotonic()
            text = self._transcribe(pcm)
            self._last_transcription_s = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001
            self._reason = f"transcription failed: {str(exc)[:1200]}"
            logger.warning("%s", self._reason)
            return
        # A stop or another response may have started while Whisper was working.
        if (self.suppress() or self._last_transcription_s > 5
                or self.playback_epoch() != epoch or time.time() - self._last_chunk_at > 5):
            self._reason = "transcription expired or overlapped playback"
            return
        if not text:
            return
        low = text.lower()
        if self.wake:
            if self.wake not in low:
                return  # not addressed to the pet
            text = low.replace(self.wake, "", 1).strip(" ,.!?") or text
        logger.info("heard: %s", text)
        try:
            self.on_text(text)
        except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the ears
            logger.warning("on_text handler error: %s", exc)

    def _run(self) -> None:
        bytes_per_s = self.rate * 2  # S16 mono
        while not self._stop.is_set():
            try:
                with httpx.stream("GET", self.stream_url, timeout=httpx.Timeout(5, connect=3),
                                  trust_env=False, follow_redirects=False) as resp:
                    resp.raise_for_status()
                    self._provenance = resp.headers.get("X-Audio-Provenance", "unknown")
                    if self._provenance != "real":
                        raise ValueError("microphone with unknown/simulated provenance ignored")
                    self._connected = True
                    self._reason = None
                    buf = bytearray()
                    silence = 0.0
                    speaking = False
                    for chunk in resp.iter_bytes(4096):
                        if self._stop.is_set():
                            return
                        self._last_chunk_at = time.time()
                        if len(chunk) % 2:
                            self._reason = "partial PCM sample discarded"
                            buf.clear()
                            silence = 0.0
                            speaking = False
                            continue
                        if self.suppress():
                            # Drain and discard echoed audio instead of transcribing it later.
                            buf.clear()
                            silence = 0.0
                            speaking = False
                            self._reason = "audio ignored during playback/tail"
                            continue
                        self._reason = None
                        dur = len(chunk) / bytes_per_s
                        loud = self._rms(chunk) > _SILENCE_RMS
                        if loud:
                            speaking = True
                            silence = 0.0
                            buf += chunk
                        elif speaking:
                            buf += chunk
                            silence += dur
                            if silence >= _END_SILENCE_S:
                                if len(buf) / bytes_per_s >= _MIN_UTTER_S:
                                    self._emit(bytes(buf))
                                buf.clear(); silence = 0.0; speaking = False
                        if len(buf) / bytes_per_s >= _MAX_UTTER_S:
                            self._emit(bytes(buf)); buf.clear(); silence = 0.0; speaking = False
                    raise OSError("microphone stream ended")
            except Exception as exc:  # noqa: BLE001 - stream dropped -> back off + retry
                self._connected = False
                self._reason = f"microphone stream unavailable: {str(exc)[:1200]}"
                if self._stop.is_set():
                    return
                logger.warning("%s; retrying", self._reason)
                self._stop.wait(2.0)


def build_ears(stream_url: str, model: str, on_text, **kw):
    """Ears if faster-whisper + numpy are available, else None (STT disabled)."""
    try:
        return Ears(stream_url, model, on_text, **kw)
    except Exception as exc:  # noqa: BLE001 - missing deps / model load fail
        print(f"  (spoken commands off — {exc}; the pet still hears nothing but works)")
        return None
