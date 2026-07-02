"""The pet's voice — local Piper TTS, spoken through the Pi's speaker.

Synthesis (Piper) runs on the Jetson; playback happens on the **Pi** (same split
as the camera/mic — the Pi is the audio device, the Jetson is the compute). The
``sink`` selects where the audio goes:
  * ``"pi"``   — POST the synthesized WAV to the robot's /audio/play (default on
    hardware); the Pi plays it on its speaker.
  * ``"local"`` — play on this machine with ``aplay`` (handy for dev on a laptop).

Fully local (no cloud) and **entirely optional**: missing piper / model / sink →
the pet stays text-only (its lines are already printed), the same graceful-degrade
pattern as the camera/VLM. Speech is fire-and-forget on a background thread so it
never stalls the loop, and overlapping lines are dropped.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading

import httpx


class Voice:
    def __init__(
        self,
        enabled: bool,
        model: str | None,
        player: str = "aplay -q",
        sink: str = "pi",
        play_url: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._speaking = False
        self.sink = sink
        self.play_url = play_url
        self._model = model
        self._player = player
        self._piper = shutil.which("piper") if enabled else None
        self._player_bin = shutil.which(player.split()[0]) if (enabled and player) else None
        # Voice needs piper + a model always; then either a Pi play URL (sink=pi)
        # or a local player (sink=local).
        sink_ok = bool(play_url) if sink == "pi" else bool(self._player_bin)
        self.enabled: bool = bool(enabled and self._piper and self._model and sink_ok)
        if enabled and not self.enabled:
            missing = []
            if not self._piper:
                missing.append("piper")
            if not self._model:
                missing.append("a voice model (PET_VOICE_MODEL)")
            if not sink_ok:
                missing.append("a Pi /audio/play URL" if sink == "pi" else f"player '{player.split()[0]}'")
            print(f"  (voice off — missing {', '.join(missing)}; the pet stays text-only)")

    def say(self, text: str) -> None:
        """Speak `text` in the background. No-op if voice is off or already busy."""
        if not self.enabled or not text:
            return
        with self._lock:
            if self._speaking:
                return  # don't pile up; drop this line
            self._speaking = True
        threading.Thread(target=self._speak, args=(text,), daemon=True).start()

    def _speak(self, text: str) -> None:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
                subprocess.run(
                    [self._piper, "--model", self._model, "--output_file", wav.name],
                    input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30, check=True,
                )
                if self.sink == "pi":
                    wav.seek(0)
                    httpx.post(self.play_url, content=wav.read(), timeout=30)  # Pi plays it
                else:
                    subprocess.run(
                        [*self._player.split(), wav.name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
                    )
        except Exception:  # noqa: BLE001 - speech must never crash the pet
            pass
        finally:
            with self._lock:
                self._speaking = False
