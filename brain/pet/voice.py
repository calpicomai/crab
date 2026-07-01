"""The pet's voice — local Piper TTS, spoken through the Jetson's audio out.

Fully local (no cloud), and **entirely optional**: if Piper or a voice model or
an audio player isn't available, the pet simply stays text-only (its lines are
already printed) — exactly the graceful-degrade pattern the camera/VLM use. This
is the output half of the roadmap's Voice I/O stage; spoken *commands* (STT) come
later.

Speech is fire-and-forget on a background thread so it never stalls the control
loop, and overlapping lines are dropped (a busy pet talks over itself less).
Piper is invoked as a subprocess: it synthesizes a WAV which is handed to a
player (``aplay`` by default). Configure the model + player via env.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading


class Voice:
    def __init__(self, enabled: bool, model: str | None, player: str) -> None:
        self._lock = threading.Lock()
        self._speaking = False
        self._piper = shutil.which("piper") if enabled else None
        self._model = model
        self._player = player  # e.g. "aplay" or "aplay -q"
        self._player_bin = shutil.which(player.split()[0]) if (enabled and player) else None
        # Voice is live only if the whole chain is present.
        self.enabled: bool = bool(enabled and self._piper and self._model and self._player_bin)
        if enabled and not self.enabled:
            missing = []
            if not self._piper:
                missing.append("piper")
            if not self._model:
                missing.append("a voice model (PET_VOICE_MODEL)")
            if not self._player_bin:
                missing.append(f"player '{player.split()[0]}'")
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
                # piper reads text on stdin, writes a WAV; then the player plays it.
                subprocess.run(
                    [self._piper, "--model", self._model, "--output_file", wav.name],
                    input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30, check=True,
                )
                subprocess.run(
                    [*self._player.split(), wav.name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
                )
        except Exception:  # noqa: BLE001 - speech must never crash the pet
            pass
        finally:
            with self._lock:
                self._speaking = False
