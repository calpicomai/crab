"""Gait engine — the stable seam between the network protocol and the legs.

``walk``/``turn`` are still backed by SunFounder picrawler's built-in
``do_action``. ``stand``/``sit`` are **staged**: they move one leg at a time to
picrawler's stand/sit coordinates via ``do_single_leg``, at a reduced
``STAND_SPEED`` with a short settle delay between legs. This is a power-safety
measure — driving all 12 servos at once (as ``do_action("stand")`` does) causes a
current spike that can brown out and reset the Pi on the shared Robot HAT rail,
especially if a mis-calibrated leg stalls. Staging keeps only ~3 servos loaded at
a time. picrawler applies the offsets you set with the SunFounder calibration
tool, so there is intentionally NO calibration code here (see CLAUDE.md).

The important property is the SEAM: the public method signatures
(``stand``/``sit``/``walk``/``turn``/``test_leg``/``get_status``) are fixed. The
real custom, coordinate-based gait later replaces the bodies (canned
``do_action`` -> ``crawler.do_step(coords, speed)``) WITHOUT touching the HTTP
protocol, the Jetson client, or these signatures. The staged per-leg stand/sit is
the first step in that direction.

If picrawler / robot_hat are not importable (e.g. a dev laptop or this CI box),
the engine drops into ``simulate`` mode: it logs the intended action and returns
success, so the whole robot<->brain link is runnable off-hardware.
"""

from __future__ import annotations

import logging
import time

from shared import DEFAULT_SPEED, LEG_COUNT, Pose, RobotStatus

from . import config

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

# picrawler leg coordinates (from the picrawler source) for the two static poses.
# Order is [leg0, leg1, leg2, leg3], each [x, y, z].
STAND_COORD: list[list[int]] = [[45, 45, -50], [45, 0, -50], [45, 0, -50], [45, 45, -50]]
SIT_COORD: list[list[int]] = [[45, 45, -30], [70, 0, -30], [70, 0, -30], [45, 45, -30]]

# Per-leg lateral (y) neutral, taken from the stand pose above. The custom trot
# keeps each leg's y fixed and only modulates x (stride) and z (lift) — the axes
# whose meaning is unambiguous across legs (x forward+, z up+).
LEG_Y: list[int] = [45, 0, 0, 45]
# Trot diagonals for leg order 0=FL, 1=FR, 2=RL, 3=RR: FL+RR swing while FR+RL
# support, then swap — the alternating diagonal pairs that make a trot.
TROT_DIAGONAL_A: tuple[int, int] = (0, 3)  # front-left + rear-right
TROT_DIAGONAL_B: tuple[int, int] = (1, 2)  # front-right + rear-left


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
    # Internal helpers
    # ----------------------------------------------------------------- #
    def _do_action(self, action: str, step: int, speed: int) -> None:
        """Run a canned picrawler action, or log it in simulate mode.

        Used by walk/turn. # TODO: real custom gait — replace with a
        coordinate-based gait via crawler.do_step(coords, speed).
        """
        if self.simulate or self._crawler is None:
            logger.info("[simulate] do_action(%r, step=%d, speed=%d)", action, step, speed)
            return
        self._crawler.do_action(action, step, speed)  # pragma: no cover - hardware

    def _do_single_leg(self, leg: int, coord: list[int], speed: int) -> None:
        """Move ONE leg to a coordinate, or log it in simulate mode.

        This is the power-safe primitive behind the staged stand/sit and the
        per-leg diagnostic: only one leg (3 servos) is driven at a time.
        """
        if self.simulate or self._crawler is None:
            logger.info("[simulate] do_single_leg(leg=%d, coord=%s, speed=%d)", leg, coord, speed)
            return
        self._crawler.do_single_leg(leg, coord, speed)  # pragma: no cover - hardware

    def _move_pose_staged(self, coords: list[list[int]]) -> None:
        """Move to a full-body pose one leg at a time, gently.

        Drives each leg in turn at config.STAND_SPEED with config.LEG_SETTLE_S
        between legs, so only ~3 servos draw current simultaneously.
        """
        for leg in range(LEG_COUNT):
            self._do_single_leg(leg, coords[leg], config.STAND_SPEED)
            if leg < LEG_COUNT - 1:
                time.sleep(config.LEG_SETTLE_S)

    def _do_step(self, coords: list[list[int]], speed: int) -> None:
        """Move all four legs to a coordinate frame at once, or log it (simulate).

        This is the primitive of the custom coordinate gait. Out-of-range coords
        are clamped by picrawler (israise defaults False).
        """
        if self.simulate or self._crawler is None:
            logger.info("[simulate] do_step(%s, speed=%d)", coords, speed)
            return
        self._crawler.do_step(coords, speed)  # pragma: no cover - hardware

    def _trot_frames(self) -> list[list[list[int]]]:
        """Build one forward diagonal-trot cycle as four coordinate frames.

        Diagonal A (FL+RR) and B (FR+RL) alternate: one pair swings forward through
        the air (foot lifted, x moves to `fwd`) while the other stays planted and
        drags the body forward (x moves to `back`), then they swap. Only x/z are
        modulated; each leg keeps its neutral y (LEG_Y).
        """
        xn, stride = config.GAIT_X_NEUTRAL, config.GAIT_STRIDE
        fwd, back = xn + stride // 2, xn - stride // 2
        up, down = config.GAIT_LIFT_Z, config.GAIT_DOWN_Z

        def frame(a_x: int, a_z: int, b_x: int, b_z: int) -> list[list[int]]:
            coords = [[0, 0, 0] for _ in range(LEG_COUNT)]
            for i in TROT_DIAGONAL_A:
                coords[i] = [a_x, LEG_Y[i], a_z]
            for i in TROT_DIAGONAL_B:
                coords[i] = [b_x, LEG_Y[i], b_z]
            return coords

        return [
            frame(fwd, up, back, down),    # A swings forward (lifted); B stance back
            frame(fwd, down, back, down),  # A plants forward
            frame(back, down, fwd, up),    # A drags body back; B swings forward (lifted)
            frame(back, down, fwd, down),  # B plants forward
        ]

    def _trot_walk(self, steps: int, speed: int) -> None:
        """Custom coordinate trot. Enters from a stand, then cycles the frames."""
        if self._pose != Pose.STANDING:
            self.stand()  # staged, safe entry so the first frame doesn't jerk
        frames = self._trot_frames()
        for _ in range(max(0, steps)):
            for coords in frames:
                self._do_step(coords, speed)

    # ----------------------------------------------------------------- #
    # Public abilities (mirror shared.Action). Signatures are the seam.
    # ----------------------------------------------------------------- #
    def stand(self) -> None:
        self._is_moving = True
        try:
            self._move_pose_staged(STAND_COORD)
            self._pose = Pose.STANDING
        finally:
            self._is_moving = False

    def sit(self) -> None:
        self._is_moving = True
        try:
            self._move_pose_staged(SIT_COORD)
            self._pose = Pose.SITTING
        finally:
            self._is_moving = False

    def walk(self, steps: int, speed: int = DEFAULT_SPEED, mode: str | None = None) -> None:
        # mode: "canned" (picrawler do_action) or "trot" (custom do_step gait).
        # Defaults to config.GAIT_MODE; the tuning tool passes mode="trot" explicitly.
        # Network protocol is unchanged — WalkCommand carries no mode.
        mode = (mode or config.GAIT_MODE).lower()
        self._is_moving = True
        try:
            if mode == "trot":
                self._trot_walk(steps, speed)
            else:
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

    def test_leg(self, leg: int, speed: int = DEFAULT_SPEED) -> None:
        """Diagnostic: move a single leg to its standing coordinate, gently.

        Isolates a leg that drives to a wrong/extreme position. Only the one leg
        moves, so a stall can't load all 12 servos at once.
        """
        if not 0 <= leg < LEG_COUNT:
            raise ValueError(f"leg must be 0..{LEG_COUNT - 1}, got {leg}")
        self._is_moving = True
        try:
            self._do_single_leg(leg, STAND_COORD[leg], speed)
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
