"""Pi-owned bounded primitives with explicit simulation and fail-closed limits.

SunFounder owns all servo timing and saved calibration. A completed vendor call
is an executed open-loop command, never proof of a measured body displacement.
Stop is latched and observed between vendor cycles / staged leg changes; it
cannot interrupt a blocking vendor call or replace a physical power cutoff.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import math
import threading
import time
from typing import Callable

from shared import DEFAULT_SPEED, LEG_COUNT, Pose, RobotStatus
from . import config

logger = logging.getLogger("picrawler.gait")
try:  # import only; constructing Picrawler resets the MCU
    from picrawler import Picrawler
    _PICRAWLER_AVAILABLE = True
    _IMPORT_ERROR = ""
except Exception as exc:  # hardware is optional, not implicitly simulated
    Picrawler = None
    _PICRAWLER_AVAILABLE = False
    _IMPORT_ERROR = str(exc)[:240]

SERVO_COUNT = 12  # 4 legs x 3 joints

# picrawler leg coordinates (from the picrawler source) for the two static poses.
# Order is [leg0, leg1, leg2, leg3], each [x, y, z].
STAND_COORD: list[list[int]] = [[45, 45, -50], [45, 0, -50], [45, 0, -50], [45, 45, -50]]
SIT_COORD: list[list[int]] = [[45, 45, -30], [70, 0, -30], [70, 0, -30], [45, 45, -30]]

# picrawler's built-in forward gait, as its exact do_step keyframes (leg order
# 0=FL,1=FR,2=RL,3=RR). This is the KNOWN-GOOD coordinate sequence that actually
# translates the body forward — the custom gait plays these (optionally with a
# longer stride) rather than inventing coordinates. A v1 attempt that modulated x
# uniformly just "danced" in place: forward motion here lives in the per-leg
# y-sweep + z-lift, not a global +x, because the legs sit at rotated corners.
FORWARD_FRAMES: list[list[list[int]]] = [
    [[45, 45, -50], [70, 0, -30], [45, 0, -50], [45, 45, -50]],
    [[45, 45, -50], [45, 90, -30], [45, 0, -50], [45, 45, -50]],
    [[45, 45, -50], [45, 90, -50], [45, 0, -50], [45, 45, -50]],
    [[45, 0, -50], [45, 45, -50], [45, 45, -50], [45, 90, -50]],
    [[45, 0, -50], [45, 45, -50], [45, 45, -50], [45, 90, -30]],
    [[45, 0, -50], [45, 45, -50], [45, 45, -50], [70, 0, -30]],
    [[45, 0, -50], [45, 45, -50], [45, 45, -50], [45, 0, -50]],
]


@dataclass
class MotionResult:
    ok: bool
    action: str
    requested_cycles: int = 0
    completed_cycles: int = 0
    applied_speed: int = 0
    reason: str = ""
    cancelled: bool = False
    estimated_turn_degrees: float | None = None
    estimate_provenance: str = "unmeasured"
    sequence: int = 0
    movement_uncertain: bool = False
    initialization_executed: bool = False


class GaitEngine:
    """Only this component executes motion; all entry points share its limits."""

    def __init__(self, simulate: bool = False) -> None:
        self.simulate = bool(simulate)
        self.enabled = self.simulate or (config.ACTUATORS_ENABLED and _PICRAWLER_AVAILABLE)
        self.unavailable_reason = ""
        if not self.simulate:
            if not config.ACTUATORS_ENABLED:
                self.unavailable_reason = "actuators disabled; hardware acceptance requires explicit enablement"
            elif not _PICRAWLER_AVAILABLE:
                self.unavailable_reason = "picrawler unavailable: " + _IMPORT_ERROR
        self._pose = Pose.UNKNOWN
        self._is_moving = False
        self._started_at = time.monotonic()
        self._motion_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._sequence = 0
        self._reflex_stopped = False
        self.clearance_fn: Callable[[], float | None] | None = None
        self.battery_fn: Callable[[], float | None] | None = None
        self.world = None
        self.last_result: MotionResult | None = None
        self._crawler = None
        self._initialization_executed = False
        # Picrawler construction moves servo positions. Delay it until an
        # explicitly enabled motion passes fresh power AND clearance checks.
        logger.info("Motion simulate=%s enabled=%s reason=%s", self.simulate, self.enabled, self.unavailable_reason)

    def _result(self, action: str, requested: int = 0, reason: str = "") -> MotionResult:
        return MotionResult(False, action, requested_cycles=requested, reason=reason,
                            sequence=self._sequence,
                            estimate_provenance="idealized_simulation" if self.simulate else "unmeasured")

    def _finish(self, result: MotionResult) -> MotionResult:
        result.sequence = self._sequence
        self.last_result = result
        return result

    @staticmethod
    def _valid_speed(speed: int) -> bool:
        return isinstance(speed, int) and not isinstance(speed, bool) and 1 <= speed <= 100

    @staticmethod
    def _read(reader: Callable[[], float | None] | None) -> float | None:
        if reader is None:
            return None
        try:
            value = reader()
            if isinstance(value, bool) or value is None or not math.isfinite(value) or value <= 0:
                return None
            return float(value)
        except Exception:
            return None

    def _guard(self, result: MotionResult, speed: int, *, forward: bool = False,
               min_clearance_cm: float | None = None) -> bool:
        if self._stop_event.is_set():
            result.cancelled = True
            result.reason = "stop latched"
            return False
        if not self.enabled:
            result.reason = self.unavailable_reason
            return False
        voltage = self._read(self.battery_fn)
        if voltage is None:
            result.reason = "battery unavailable; motion inhibited"
            return False
        if voltage < config.BATTERY_CRITICAL_V:
            result.reason = "critical battery; motion inhibited"
            return False
        cap = config.BATTERY_LOW_SPEED if voltage < config.BATTERY_LOW_V else config.MAX_MOTION_SPEED
        result.applied_speed = min(speed, cap, config.MAX_MOTION_SPEED)
        if forward:
            # A caller cannot disable or lower the Pi's minimum safety margin.
            threshold = max(config.REFLEX_STOP_CM, min_clearance_cm or 0.0)
            distance = self._read(self.clearance_fn)
            if distance is None or distance < threshold:
                self._reflex_stopped = True
                result.reason = "clearance unavailable" if distance is None else "forward clearance blocked"
                return False
        return True

    def _execute(self, action: str, requested: int, speed: int, operation,
                 *, initialization_clearance_cm: float | None = None) -> MotionResult:
        result = self._result(action, requested)
        if not self._valid_speed(speed):
            result.reason = "speed must be an integer in 1..100"
            return self._finish(result)
        # Never queue commands: an old command waiting behind a gesture must not
        # start after a newer stop / resume. The coordinator may submit a fresh one.
        if not self._motion_lock.acquire(blocking=False):
            result.reason = "motion busy; command not queued"
            return self._finish(result)
        try:
            self._reflex_stopped = False
            if not self._guard(result, speed):
                return self._finish(result)
            if not self._initialize_driver(result, speed, initialization_clearance_cm):
                return self._finish(result)
            self._is_moving = True
            operation(result)
            result.ok = result.completed_cycles == requested and not result.reason
            if self._stop_event.is_set():
                result.ok = False
                result.cancelled = True
                result.reason = "stop latched; last vendor boundary reached"
            return self._finish(result)
        except Exception as exc:
            # A vendor error may follow partial physical motion. Invalidate every
            # open-loop estimate; a failed command is never a no-movement claim.
            self._sequence += 1
            self._pose = Pose.UNKNOWN
            self._stop_event.set()
            result.ok = False
            result.movement_uncertain = True
            result.reason = self.unavailable_reason or "vendor motion failed: " + str(exc)[:240]
            return self._finish(result)
        finally:
            self._is_moving = False
            self._motion_lock.release()

    def _initialize_driver(self, result: MotionResult, speed: int,
                           min_clearance_cm: float | None = None) -> bool:
        if self.simulate or self._crawler is not None:
            return True
        # Initialization can change all twelve servo targets, even for a turn or
        # posture command. Unknown/blocked forward space inhibits this boundary.
        if not self._guard(result, speed, forward=True, min_clearance_cm=min_clearance_cm):
            return False
        self._is_moving = True
        result.initialization_executed = True
        self._initialization_executed = True
        try:
            # Vendor reads ~/.config/.picrawler.config; never duplicate offsets.
            self._crawler = Picrawler()
        except Exception as exc:
            # Do not automatically retry a partially completed initialization
            # after resume; restart/recovery requires hardware inspection.
            self.enabled = False
            self.unavailable_reason = "driver initialization failed: " + str(exc)[:240]
            raise
        self._sequence += 1
        self._pose = Pose.UNKNOWN
        # Stop cannot preempt the vendor constructor. Honor it immediately on
        # return, and recheck battery/clearance before the requested primitive.
        return self._guard(result, speed, forward=True, min_clearance_cm=min_clearance_cm)

    def _do_action(self, action: str, speed: int) -> None:
        if self.simulate:
            logger.debug("[simulate] %s, one vendor cycle, speed=%s", action, speed)
        else:
            self._crawler.do_action(action, 1, speed)
        self._sequence += 1

    def _do_single_leg(self, leg: int, coord: list[int], speed: int) -> None:
        if not self.simulate:
            self._crawler.do_single_leg(leg, coord, speed)
        self._sequence += 1

    def _pose_motion(self, action: str, coords: list[list[int]], pose: Pose) -> MotionResult:
        def operation(result):
            self._pose = Pose.UNKNOWN
            for leg in range(LEG_COUNT):
                if not self._guard(result, config.STAND_SPEED):
                    return
                self._do_single_leg(leg, coords[leg], result.applied_speed)
                if leg < LEG_COUNT - 1 and self._stop_event.wait(config.LEG_SETTLE_S):
                    result.cancelled = True
                    result.reason = "stop latched between leg stages"
                    return
            self._pose = pose
            result.completed_cycles = 1
        return self._execute(action, 1, config.STAND_SPEED, operation)

    def stand(self) -> MotionResult:
        return self._pose_motion("stand", STAND_COORD, Pose.STANDING)

    def sit(self) -> MotionResult:
        return self._pose_motion("sit", SIT_COORD, Pose.SITTING)

    @staticmethod
    def _scaled_frame(frame: list[list[int]], scale: float) -> list[list[int]]:
        # Retained for offline gait tooling; never amplify beyond stock reach.
        scale = max(0.5, min(1.0, scale))
        return [[round(STAND_COORD[i][0] + (x - STAND_COORD[i][0]) * scale),
                 round(STAND_COORD[i][1] + (y - STAND_COORD[i][1]) * scale), z]
                for i, (x, y, z) in enumerate(frame)]

    def walk(self, steps: int, speed: int = DEFAULT_SPEED, mode: str | None = None,
             min_clearance_cm: float | None = None) -> MotionResult:
        if isinstance(steps, bool) or not isinstance(steps, int) or not 0 <= steps <= config.MAX_MOTION_CYCLES:
            return self._finish(self._result("walk", reason=f"cycles must be 0..{config.MAX_MOTION_CYCLES}"))
        if min_clearance_cm is not None and (not math.isfinite(min_clearance_cm) or min_clearance_cm < 0):
            return self._finish(self._result("walk", steps, "invalid clearance margin"))
        mode = mode or config.GAIT_MODE
        if mode != "canned":
            return self._finish(self._result("walk", steps, "custom gait is not an accepted motion primitive"))
        if steps == 0:
            return self._finish(MotionResult(True, "walk", sequence=self._sequence))
        def operation(result):
            for _ in range(steps):
                if not self._guard(result, speed, forward=True, min_clearance_cm=min_clearance_cm):
                    return
                self._do_action("forward", result.applied_speed)
                result.completed_cycles += 1
                self._pose = Pose.STANDING
                if self.simulate and self.world is not None:
                    self.world.advance(config.SIM_STRIDE_CM)
        return self._execute("walk", steps, speed, operation, initialization_clearance_cm=min_clearance_cm)

    def turn_steps(self, direction: str, cycles: int = 1, speed: int = DEFAULT_SPEED) -> MotionResult:
        if direction not in {"left", "right"}:
            return self._finish(self._result("turn_steps", reason="direction must be left or right"))
        if isinstance(cycles, bool) or not isinstance(cycles, int) or not 1 <= cycles <= config.MAX_MOTION_CYCLES:
            return self._finish(self._result("turn_steps", reason=f"cycles must be 1..{config.MAX_MOTION_CYCLES}"))
        def operation(result):
            for _ in range(cycles):
                if not self._guard(result, speed):
                    return
                self._do_action("turn " + direction, result.applied_speed)
                result.completed_cycles += 1
                self._pose = Pose.STANDING
                if self.simulate:
                    estimate = config.SIM_TURN_CYCLE_DEG * (1 if direction == "right" else -1)
                    result.estimated_turn_degrees = (result.estimated_turn_degrees or 0) + estimate
                    if self.world is not None:
                        self.world.rotate(estimate)
        return self._execute("turn_steps", cycles, speed, operation)

    def turn(self, degrees: float, speed: int = DEFAULT_SPEED) -> MotionResult:
        """Legacy API: precise turns were never supported and are now rejected."""
        if degrees == 0:
            return self._finish(MotionResult(True, "turn", sequence=self._sequence))
        return self._finish(self._result("turn", reason="precise angle unsupported; use bounded turn_steps(left/right)"))

    def test_leg(self, leg: int, speed: int = DEFAULT_SPEED) -> MotionResult:
        if isinstance(leg, bool) or not isinstance(leg, int) or not 0 <= leg < LEG_COUNT:
            return self._finish(self._result("test_leg", reason="leg must be an integer in 0..3"))
        def operation(result):
            self._do_single_leg(leg, STAND_COORD[leg], result.applied_speed)
            self._pose = Pose.UNKNOWN  # one leg does not establish a whole-body pose
            result.completed_cycles = 1
        return self._execute("test_leg", 1, speed, operation)

    def stop(self) -> MotionResult:
        self._stop_event.set()  # deliberately does not wait for the motion lock
        return self._finish(MotionResult(True, "stop", reason="stop latched; active vendor call finishes at its boundary",
                                         sequence=self._sequence))

    def resume(self) -> MotionResult:
        if not self._motion_lock.acquire(blocking=False):
            return self._finish(self._result("resume", reason="motion still stopping"))
        try:
            self._stop_event.clear()
            return self._finish(MotionResult(True, "resume", reason="stop latch cleared; fresh commands still require health checks",
                                             sequence=self._sequence))
        finally:
            self._motion_lock.release()

    @property
    def reflex_stopped(self) -> bool:
        return self._reflex_stopped

    def motion_state(self) -> dict:
        return {"sequence": self._sequence, "enabled": self.enabled,
                "initialized": self._crawler is not None,
                "initialization_executed": self._initialization_executed,
                "stop_latched": self._stop_event.is_set(),
                "unavailable_reason": self.unavailable_reason,
                "last_result": asdict(self.last_result) if self.last_result else None,
                "position_feedback": False,
                "turn_model": "idealized_simulation" if self.simulate else "unmeasured_vendor_cycle"}

    def get_status(self) -> RobotStatus:
        return RobotStatus(pose=self._pose, is_moving=self._is_moving,
                           servo_count=SERVO_COUNT, simulate=self.simulate,
                           reflex_stopped=self._reflex_stopped,
                           uptime_s=round(time.monotonic() - self._started_at, 3))
