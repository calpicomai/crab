"""Ultrasonic distance sensor on the Pi (SunFounder robot_hat).

Reads forward clearance in cm. The brain's wander/avoid loop consumes this via
get_status to decide when to turn away from obstacles.

Mirrors the GaitEngine simulate philosophy: if robot_hat isn't importable (dev
laptop, CI) or the sensor is disabled, it returns a synthetic "clear" distance so
the whole autonomy loop runs off-hardware.
"""

from __future__ import annotations

import logging

from . import config

logger = logging.getLogger("picrawler.sensors")

# Guarded hardware import. Missing off-Pi -> simulate.
try:  # pragma: no cover - depends on deploy target
    from robot_hat import Pin, Ultrasonic

    _ULTRASONIC_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    Pin = None  # type: ignore[assignment,misc]
    Ultrasonic = None  # type: ignore[assignment,misc]
    _ULTRASONIC_AVAILABLE = False
    logger.info("robot_hat Ultrasonic unavailable (%s); DistanceSensor will simulate", exc)

# Synthetic clearance reported in simulate mode (cm). Comfortably clear so the
# wander loop walks; tests raise the avoid threshold to exercise the turn branch.
_SIMULATED_CM = 80.0


class DistanceSensor:
    """Forward-facing ultrasonic rangefinder. read_cm() -> distance or None."""

    def __init__(self, trig: str = "D2", echo: str = "D3", simulate: bool = False) -> None:
        self.simulate: bool = bool(simulate) or not _ULTRASONIC_AVAILABLE
        self._sonar = None
        # Optional 2D sim world (robot/simworld.py). When set, simulate returns the
        # real ray-cast clearance from the world instead of a flat synthetic value.
        self.world = None
        if self.simulate:
            logger.warning("DistanceSensor running in SIMULATE mode — synthetic distance")
        else:  # pragma: no cover - requires hardware
            self._sonar = Ultrasonic(Pin(trig), Pin(echo))
            logger.info("Ultrasonic sensor on trig=%s echo=%s", trig, echo)

    def read_cm(self) -> float | None:
        """Latest clearance in cm. None means no echo (treat as clear/unknown)."""
        if self.simulate or self._sonar is None:
            if self.world is not None:
                return self.world.sonar()
            return _SIMULATED_CM
        try:  # pragma: no cover - hardware
            # Fewer ping retries -> lower worst-case latency (see config).
            try:
                distance = self._sonar.read(config.ULTRASONIC_PINGS)
            except TypeError:  # older robot_hat: read() takes no args
                distance = self._sonar.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ultrasonic read failed: %s", exc)
            return None
        # robot_hat returns -1/-2 (or <=0) on timeout / no echo.
        if distance is None or distance <= 0:
            return None
        return float(distance)
