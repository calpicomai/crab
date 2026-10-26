"""Small local audio utilities; no models, downloads or device access on import."""

from __future__ import annotations

import io
import math
import struct
import subprocess
import tempfile
import wave
from collections.abc import Callable

MAX_WAV_BYTES = 4 * 1024 * 1024
MAX_AUDIO_SECONDS = 20.0
ERROR_LIMIT = 1200


def bounded_error(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).replace("\x00", "")[:ERROR_LIMIT]


def wav_duration(data: bytes) -> float:
    """Validate short PCM WAV payloads before handing them to a device."""
    if not data or len(data) > MAX_WAV_BYTES:
        raise ValueError("WAV must contain 1 byte to 4 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2:
                raise ValueError("only 16-bit PCM WAV is supported")
            if wav.getnchannels() not in (1, 2) or not 8000 <= wav.getframerate() <= 48000:
                raise ValueError("WAV must be mono/stereo at 8–48 kHz")
            count = wav.getnframes()
            duration = count / wav.getframerate()
            if not 0 < duration <= MAX_AUDIO_SECONDS:
                raise ValueError("WAV duration must be between 0 and 20 seconds")
            if len(wav.readframes(count)) != count * wav.getnchannels() * 2:
                raise ValueError("truncated WAV data")
            return duration
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"invalid WAV: {exc}") from exc


def sound_wav(name: str = "acknowledge") -> bytes:
    """Quiet, short, deterministic cues generated locally without a TTS model."""
    notes = {
        "acknowledge": ((520, .10), (680, .13)),
        "settle": ((440, .12), (330, .18)),
        "play": ((520, .08), (720, .08), (620, .12)),
    }
    if name not in notes:
        raise ValueError(f"unknown sound: {name}")
    rate = 22050
    samples = bytearray()
    for freq, seconds in notes[name]:
        count = int(rate * seconds)
        for n in range(count):
            envelope = min(1.0, n / (rate * .015), (count - n) / (rate * .025))
            sample = int(2400 * envelope * math.sin(2 * math.pi * freq * n / rate))
            samples += struct.pack("<h", sample)
        samples += b"\x00\x00" * int(rate * .035)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples)
    return output.getvalue()


def run_audio_process(
    command: list[str], *, data: bytes | None = None, timeout: float = 30,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    capture_output: bool = False,
) -> tuple[int | None, str]:
    """Capture status and bounded diagnostics without unbounded PIPE buffering.

    The optional callback registers the owned child for cancellation. It never
    runs a shell and never elevates privileges or changes system configuration.
    """
    with tempfile.TemporaryFile() as errors:
        proc = None
        try:
            proc = subprocess.Popen(
                command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                stdout=errors if capture_output else subprocess.DEVNULL, stderr=errors,
            )
            if on_start:
                on_start(proc)
            proc.communicate(input=data, timeout=timeout)
            errors.seek(0)
            return proc.returncode, bounded_error(errors.read(ERROR_LIMIT))
        except subprocess.TimeoutExpired:
            if proc is not None:
                proc.kill()
                proc.communicate()
            errors.seek(0)
            return None, bounded_error(f"audio process timed out after {timeout:g}s: " +
                                       errors.read(ERROR_LIMIT).decode(errors="replace"))
        except OSError as exc:
            return None, bounded_error(exc)
