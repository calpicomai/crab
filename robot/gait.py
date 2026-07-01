"""Gait engine — the stable seam between the network protocol and the legs.

Stage 1 backs every ability with SunFounder picrawler's built-in ``do_action``
so the robot really moves across the network. picrawler applies the offsets you
already set with the SunFounder calibration tool, so there is intentionally NO
calibration code here (see CLAUDE.md).

The important property is the SEAM: the public method signatures
(``stand``/``sit``/``walk``/``turn``/``get_status``) are fixed. When we build the
real custom, coordinate-based gait later, we replace the bodies (canned
``do_action`` -> ``crawler.do_step(coords, speed)`` reading the stored offsets)
WITHOUT touching the HTTP protocol, the Jetson client, or these signatures.

If picrawler / robot_hat are not importable (e.g. a dev laptop or this CI box),
the engine drops into ``simulate`` mode: it logs the intended action and returns
success, so the whole robot<->brain link is runnable off-hardware.
"""

from __future__ import annotations

import logging
import time

from shared import DEFAULT_SPEED, Pose, RobotStatus

logger = logging.getLogger("picrawler.gait")

# Guarded hardware import. Missing on non-Pi machines -> simulate mode.
try:  # pragma: no cover - depends on deploy target
    from picrawler import Picrawler

    _PICRAWLER_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - any import failure means "no hardware"
    Picrawler = None  # type: ignore[assignment,misc]
    _PICRAWLER_AVAILABLE = False
    logger.info("picrawler unavailable (%s); GaitEngine will run in simulate mode", exc)

SERVO_COUNT = 12  # 4 legs x 3 joints


class GaitEngine:
    """High-level robot abilities. Real-time timing lives entirely here."""

    def __init__(self, simulate: bool = False) -> None:
        # Simulate if explicitly forced OR if the hardware libs are missing.
        self.simulate: bool = bool(simulate) or not _PICRAWLER_AVAILABLE
        self._pose: Pose = Pose.UNKNOWN
        self._is_moving: bool = False
        self._started_at: float = time.monotonic()

        if self.simulate:
            self._crawler = None
            logger.warning("GaitEngine running in SIMULATE mode — no servos will move")
        else:  # pragma: no cover - requires hardware
            # Default pin map; picrawler reads the calibration you already saved.
            self._crawler = Picrawler()
            logger.info("GaitEngine initialised with picrawler hardware")

    # ----------------------------------------------------------------- #
    # Internal helper
    # ----------------------------------------------------------------- #
    def _do_action(self, action: str, step: int, speed: int) -> None:
        """Run a picrawler action, or log it in simulate mode.

        # TODO: real custom gait — replace canned do_action with a
        # coordinate-based gait via crawler.do_step(coords, speed), reading
        # picrawler's stored calibration offsets. Signatures above stay fixed.
        """
        if self.simulate or self._crawler is None:
            logger.info("[simulate] do_action(%r, step=%d, speed=%d)", action, step, speed)
            return
        self._crawler.do_action(action, step, speed)  # pragma: no cover - hardware

    # ----------------------------------------------------------------- #
    # Public abilities (mirror shared.Action). Signatures are the seam.
    # ----------------------------------------------------------------- #
    def stand(self) -> None:
        self._is_moving = True
        try:
            self._do_action("stand", 1, DEFAULT_SPEED)
            self._pose = Pose.STANDING
        finally:
            self._is_moving = False

    def sit(self) -> None:
        self._is_moving = True
        try:
            self._do_action("sit", 1, DEFAULT_SPEED)
            self._pose = Pose.SITTING
        finally:
            self._is_moving = False

    def walk(self, steps: int, speed: int = DEFAULT_SPEED) -> None:
        self._is_moving = True
        try:
            for _ in range(max(0, steps)):
                self._do_action("forward", 1, speed)
            self._pose = Pose.STANDING
        finally:
            self._is_moving = False

    def turn(self, degrees: float, speed: int = DEFAULT_SPEED) -> None:
        # Positive degrees = turn right (clockwise), negative = left.
        # TODO: real custom gait — map degrees to a precise number of turn
        # cycles / a coordinate trajectory. Stage 1 uses one canned turn action.
        action = "turn right" if degrees >= 0 else "turn left"
        self._is_moving = True
        try:
            self._do_action(action, 1, speed)
            self._pose = Pose.STANDING
        finally:
            self._is_moving = False

    def get_status(self) -> RobotStatus:
        return RobotStatus(
            pose=self._pose,
            is_moving=self._is_moving,
            servo_count=SERVO_COUNT,
            simulate=self.simulate,
            uptime_s=round(time.monotonic() - self._started_at, 3),
        )
