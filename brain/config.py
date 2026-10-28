"""Configuration for the Jetson brain — chiefly how to reach the Pi.

The Pi's address is NOT hardcoded anywhere else. Override via environment
variables, or edit the defaults below. The default uses the Raspberry Pi's mDNS
hostname, which works without knowing the IP if avahi/Bonjour is available on
the LAN. If mDNS is not available, set ROBOT_HOST to the Pi's IP address.
"""

from __future__ import annotations

import math
import os

from .legacy_config import _VALUES as _LEGACY_VALUES

__all__ = ["ROBOT_HOST", "ROBOT_PORT", "BASE_URL", "REQUEST_TIMEOUT_S", "PERCEPTION_BASE_URL", *_LEGACY_VALUES]


def _integer(name: str, default: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
        if not minimum <= value <= maximum:
            raise ValueError
    except ValueError:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}") from None
    return value


def _positive_number(name: str, default: str) -> float:
    try:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
    except ValueError:
        raise ValueError(f"{name} must be a finite positive number") from None
    return value

ROBOT_HOST: str = os.environ.get("ROBOT_HOST", "picrawler.local")
ROBOT_PORT: int = _integer("ROBOT_PORT", "8000", minimum=1, maximum=65535)

# Full base URL for the robot command server.
BASE_URL: str = os.environ.get("ROBOT_BASE_URL", f"http://{ROBOT_HOST}:{ROBOT_PORT}")

# Per-request timeout in seconds. Gait actions can take a few seconds, so this
# is generous; tighten once real gait durations are known.
REQUEST_TIMEOUT_S: float = _positive_number("ROBOT_TIMEOUT_S", "15")

# The stationary companion uses the perception endpoint, not navigation knobs.
PERCEPTION_BASE_URL: str = os.environ.get("PERCEPTION_BASE_URL", "http://localhost:8100")


def __getattr__(name: str):
    # Preserve public legacy names for costmap/simulation callers without parsing
    # them as a side effect of importing active link settings.
    from . import legacy_config
    value = getattr(legacy_config, name)
    globals()[name] = value
    return value


def __dir__():
    from . import legacy_config
    return sorted(set(globals()) | set(legacy_config._VALUES))
