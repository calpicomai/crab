"""Opt-in, physically attested Robot HAT v4 speaker control.

No vendor imports or GPIO access occur during construction/health. The vendor's
default Devices.spk_en is not board detection: require the owner's attestation
and expected pin, then reject contradictory metadata before writing anything.
"""
from __future__ import annotations

from importlib import import_module
from importlib.metadata import version as installed_version
import io
import os
import re
import shutil
import stat
import threading
import wave


SUPPORTED_VERSION = "2.5.3"
_PLAYBACK_LOCK = threading.Lock()


def silence_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\0" * 88200)  # vendor's 0.5-second I2S fill, on the selected DAC
    return output.getvalue()


class RobotHatV4:
    def __init__(self, *, verified_board, expected_pin):
        self.verified_board = verified_board
        self.expected_pin = expected_pin
        self.pin = None
        self.tool = None
        self.state = "untouched; verification pending"
        self.fault = None
        self.metadata_verified = False
        self._owner_fd = None

    def acquire(self):
        if not _PLAYBACK_LOCK.acquire(blocking=False):
            return False
        # Serialize a local self-test and server under the same service account,
        # as well as threads/instances. Do not unlink a live lock inode.
        import fcntl
        fd = None
        try:
            fd = os.open(f"/tmp/crab-speaker-hat-v4-{os.geteuid()}.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise OSError("speaker lock must be an owner-only regular file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._owner_fd = fd
            return True
        except Exception as exc:
            if fd is not None:
                os.close(fd)
            _PLAYBACK_LOCK.release()
            if isinstance(exc, BlockingIOError):
                return False
            raise

    def release(self):
        if self._owner_fd is not None:
            os.close(self._owner_fd)
            self._owner_fd = None
        _PLAYBACK_LOCK.release()

    def prepare(self):
        """Read metadata only; do not trust the vendor's unknown-board fallback."""
        if self.fault:
            raise ValueError("amplifier cleanup failed; operator inspection/restart required: " + self.fault)
        self.metadata_verified = False
        if self.verified_board != "robot_hat_v4":
            raise ValueError("verified Robot HAT v4 board attestation required")
        if type(self.expected_pin) is not int or not 0 <= self.expected_pin <= 53:
            raise ValueError("explicit verified speaker enable pin required")
        version = installed_version("robot-hat")
        if version != SUPPORTED_VERSION:
            raise ValueError(f"Robot HAT profile requires verified robot-hat {SUPPORTED_VERSION}; found {version}")
        vendor = import_module("robot_hat")
        device = vendor.__device__
        boards = vendor.Devices.DEVICES
        board = boards["robot_hat_v4x"]
        pin = board["speaker_enbale_pin"]  # vendor spelling, not an application pin guess
        if type(pin) is not int or pin != self.expected_pin or device.spk_en != pin:
            raise ValueError("verified pin disagrees with installed Robot HAT v4 metadata")
        if getattr(device, "uuid", "").strip("\0"):
            raise ValueError("EEPROM identifies another/unknown HAT; v4 profile refused")
        name = getattr(device, "name", "").strip("\0 ").lower()
        if name and "v4" not in name:
            raise ValueError("detected board name contradicts verified v4 profile")
        tool = shutil.which("pinctrl") or shutil.which("raspi-gpio")
        if tool is None:
            raise ValueError("pinctrl/raspi-gpio unavailable; no amplifier fallback")
        self.pin, self.tool = pin, tool
        self.metadata_verified = True
        self.state = "untouched; metadata matches operator attestation"

    def set_enabled(self, enabled, run):
        """Checked, bounded set + readback. run is the player's owned process path."""
        level = "dh" if enabled else "dl"
        code, error = run([self.tool, "set", str(self.pin), "op", level], timeout=1)
        if code != 0:
            self.state = "unknown"
            raise RuntimeError(error or f"GPIO {level} exited {code}")
        self.verify_level(enabled, run)

    def verify_level(self, enabled, run):
        """Never steal an already-high amplifier from another user/process."""
        level = "dh" if enabled else "dl"
        code, output = run([self.tool, "get", str(self.pin)], timeout=1, capture_output=True)
        if code != 0:
            self.state = "unknown"
            raise RuntimeError(output or f"GPIO readback exited {code}")
        if self.tool.endswith("pinctrl"):
            match = re.search(r"^\s*(\d+):\s+op\s+(dh|dl)\b[^\n]*\|\s*(hi|lo)\b", output, re.M)
            correct = (match and int(match[1]) == self.pin and match[2] == level
                       and match[3] == ("hi" if enabled else "lo"))
        else:
            match = re.search(r"^GPIO\s+(\d+):\s+level=([01])\s+fsel=1\s+func=OUTPUT\b", output, re.M)
            correct = match and int(match[1]) == self.pin and int(match[2]) == int(enabled)
        if not correct:
            self.state = "unknown"
            raise RuntimeError("speaker GPIO output/readback did not match requested state")
        self.state = "high verified" if enabled else "low verified"
