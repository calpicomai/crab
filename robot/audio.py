"""Audio on the Pi — a mic and a speaker, streamed to/from the Jetson brain.

Same split as the camera: the **device** is on the Pi, the **compute** (Whisper
STT, Piper TTS) is on the Jetson. The Pi just captures mic PCM and plays back WAVs
the Jetson sends. Uses ALSA's ``arecord``/``aplay`` (system tools — no pip dep),
and mirrors the GaitEngine/PiCamera simulate philosophy: if the tools or a device
aren't there (dev laptop, no USB audio), it drops into ``simulate`` so the link
still runs (silent mic, no-op speaker).

Speaker note: on the SunFounder Robot HAT the speaker is an onboard I2S amp. Run
SunFounder's ``i2samp.sh`` once so it becomes the default ALSA sink; then this
``aplay``-based playback routes to the HAT speaker with the default device (leave
``PICRAWLER_SPEAKER_DEVICE`` empty). See brain/setup_voice.sh.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger("picrawler.audio")

_ARECORD = shutil.which("arecord")
_APLAY = shutil.which("aplay")


class PiMic:
    """Captures mic audio as raw 16-bit mono PCM and yields it in chunks."""

    def __init__(self, rate: int = 16000, device: str | None = None, simulate: bool = False) -> None:
        self.rate = int(rate)
        self.device = device or None
        self.simulate = bool(simulate) or _ARECORD is None
        if self.simulate:
            logger.warning("PiMic in SIMULATE mode — no real microphone (silent stream)")

    def stream(self, chunk: int = 4096):
        """Yield raw PCM chunks (S16_LE, mono, self.rate). Terminates its arecord
        subprocess when the consumer stops (StreamingResponse close / disconnect)."""
        if self.simulate:
            # Silence so the Jetson's reader stays happy; ~1 chunk per 0.1s.
            silence = b"\x00" * chunk
            try:
                while True:
                    yield silence
                    time.sleep(0.1)
            except GeneratorExit:
                return
        cmd = [_ARECORD, "-q", "-t", "raw", "-f", "S16_LE", "-r", str(self.rate), "-c", "1"]
        if self.device:
            cmd += ["-D", self.device]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # pragma: no cover - hardware
        try:
            while True:
                data = proc.stdout.read(chunk)
                if not data:
                    break
                yield data
        except GeneratorExit:
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                proc.kill()


class PiSpeaker:
    """Plays a WAV (bytes) the Jetson sends, on the Pi's speaker."""

    def __init__(self, device: str | None = None, simulate: bool = False) -> None:
        self.device = device or None
        self.simulate = bool(simulate) or _APLAY is None
        self._lock = threading.Lock()
        if self.simulate:
            logger.warning("PiSpeaker in SIMULATE mode — no real speaker (playback is a no-op)")

    def play(self, wav_bytes: bytes) -> bool:
        """Play WAV bytes. Returns True if handed to aplay. Serialized so two
        replies don't overlap. Best-effort — never raises."""
        if not wav_bytes:
            return False
        if self.simulate:
            # Accepts the audio but makes no sound (off-hardware) — report success
            # so the brain's voice link runs end-to-end in sim.
            logger.info("[simulate] PiSpeaker.play(%d bytes)", len(wav_bytes))
            return True
        with self._lock:  # pragma: no cover - hardware
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                    f.write(wav_bytes)
                    f.flush()
                    cmd = [_APLAY, "-q"]
                    if self.device:
                        cmd += ["-D", self.device]
                    cmd.append(f.name)
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=30, check=False)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("aplay failed: %s", exc)
                return False
