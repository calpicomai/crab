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
# Synthetic battery voltage in simulate mode — a healthy 2S pack, so off-hardware
# runs never trip the brain's low-battery slowdown.
_SIMULATED_BATTERY_V = 7.6


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


class BatterySensor:
    """Robot HAT battery voltage. read_v() -> volts or None.

    Guarded/best-effort (we don't assume a specific robot_hat version): prefer the
    library's own battery reading if present, else an ADC channel scaled by ``scale``,
    else simulate a healthy pack. Any read error -> None (never raises)."""

    def __init__(self, channel: str = "A4", scale: float = 3.0, simulate: bool = False) -> None:
        self.simulate: bool = bool(simulate) or not _ULTRASONIC_AVAILABLE  # robot_hat present?
        self.scale = float(scale)
        self._get_v = None   # robot_hat.get_battery_voltage, if available
        self._adc = None     # fallback ADC on `channel`
        if self.simulate:
            logger.warning("BatterySensor running in SIMULATE mode — synthetic voltage")
            return
        try:  # pragma: no cover - requires hardware
            import robot_hat  # noqa: PLC0415

            if hasattr(robot_hat, "get_battery_voltage"):
                self._get_v = robot_hat.get_battery_voltage
                logger.info("Battery via robot_hat.get_battery_voltage")
            else:
                from robot_hat import ADC  # noqa: PLC0415

                self._adc = ADC(channel)
                logger.info("Battery via ADC(%s) x%.2f", channel, self.scale)
        except Exception as exc:  # noqa: BLE001
            logger.info("battery sensor unavailable (%s); simulating", exc)
            self.simulate = True

    def read_v(self) -> float | None:
        """Latest pack voltage in volts, or None if unreadable."""
        if self.simulate:
            return _SIMULATED_BATTERY_V
        try:  # pragma: no cover - hardware
            if self._get_v is not None:
                v = float(self._get_v())
            elif hasattr(self._adc, "read_voltage"):
                v = float(self._adc.read_voltage()) * self.scale
            else:
                v = (float(self._adc.read()) / 4095.0 * 3.3) * self.scale
        except Exception as exc:  # noqa: BLE001
            logger.warning("battery read failed: %s", exc)
            return None
        return round(v, 2) if v and v > 0 else None
