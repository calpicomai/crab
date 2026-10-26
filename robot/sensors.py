"""Pi adapters: missing or failed real hardware is unknown, never clear.

Only explicit simulation permits generated values. The stock forward sonar is
not collision or cliff protection. Battery conversion belongs to the vendor.
"""
from __future__ import annotations

import logging
import math
import time

from shared.health import InputHealth, unavailable
from . import config

logger = logging.getLogger("picrawler.sensors")

try:  # pragma: no cover - deploy target dependent
    from robot_hat import Pin, Ultrasonic
    _ULTRASONIC_AVAILABLE = True
except Exception as exc:
    Pin = Ultrasonic = None
    _ULTRASONIC_AVAILABLE = False
    logger.info("Ultrasonic unavailable: %s", exc)

_SIMULATED_CM = 80.0
_SIMULATED_BATTERY_V = 7.6


class DistanceSensor:
    def __init__(self, trig: str = "D2", echo: str = "D3", simulate: bool = False) -> None:
        self.simulate = bool(simulate)
        self._sonar = None
        self.world = None
        self._health = unavailable("no sample yet", source="ultrasonic",
                                   provenance="simulated" if self.simulate else "real")
        if not self.simulate:
            if not _ULTRASONIC_AVAILABLE:
                self._health.failure_reason = "robot_hat Ultrasonic unavailable"
            else:
                try:
                    self._sonar = Ultrasonic(Pin(trig), Pin(echo))
                except Exception as exc:
                    self._health.failure_reason = f"ultrasonic initialization failed: {exc}"[:300]

    def health(self) -> InputHealth:
        return self._health.model_copy(deep=True)

    def read_cm(self) -> float | None:
        try:
            if self.simulate:
                distance = self.world.sonar() if self.world is not None else _SIMULATED_CM
            elif self._sonar is None:
                self._health.available = False
                self._health.ignored_reason = self._health.failure_reason
                return None
            else:
                try:
                    distance = self._sonar.read(config.ULTRASONIC_PINGS)
                except TypeError:
                    distance = self._sonar.read()
            distance = float(distance)
            if not math.isfinite(distance) or distance <= 0:
                raise ValueError("no valid ultrasonic echo")
        except Exception as exc:
            self._health = unavailable(f"ultrasonic read failed: {exc}", source="ultrasonic",
                                       provenance="simulated" if self.simulate else "real")
            return None
        now = time.time()
        self._health = InputHealth(available=True, provenance="simulated" if self.simulate else "real",
                                   captured_at=now, received_at=now, source="ultrasonic", max_age_s=1)
        return distance


class BatterySensor:
    """Use the installed vendor getter; never guess scaling on a different board.

    ``channel`` and ``scale`` remain accepted for config compatibility but do not
    override the vendor conversion. Compare its reading with the actual pack
    before hardware acceptance.
    """
    def __init__(self, channel: str = "A4", scale: float = 3.0, simulate: bool = False) -> None:
        self.simulate = bool(simulate)
        self._get_v = None
        self._health = unavailable("no sample yet", source="battery",
                                   provenance="simulated" if self.simulate else "real")
        if self.simulate:
            return
        try:
            import robot_hat
            self._get_v = getattr(robot_hat, "get_battery_voltage", None)
            if not callable(self._get_v):
                from robot_hat.device import get_battery_voltage
                self._get_v = get_battery_voltage
            if not callable(self._get_v):
                raise RuntimeError("vendor battery getter unavailable")
        except Exception as exc:
            self._health.failure_reason = f"battery unavailable: {exc}"[:300]
            self._health.ignored_reason = self._health.failure_reason

    def health(self) -> InputHealth:
        return self._health.model_copy(deep=True)

    def read_v(self) -> float | None:
        try:
            if self.simulate:
                voltage = _SIMULATED_BATTERY_V
            elif self._get_v is None:
                self._health.available = False
                return None
            else:
                voltage = float(self._get_v())
            if not math.isfinite(voltage) or not 0 < voltage <= 9:
                raise ValueError("invalid 2S battery voltage")
        except Exception as exc:
            self._health = unavailable(f"battery read failed: {exc}", source="battery",
                                       provenance="simulated" if self.simulate else "real")
            return None
        now = time.time()
        self._health = InputHealth(available=True, provenance="simulated" if self.simulate else "real",
                                   captured_at=now, received_at=now, max_age_s=5, source="battery")
        return round(voltage, 2)
