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
from typing import Callable

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


class GaitEngine:
    """High-level robot abilities. Real-time timing lives entirely here."""

    def __init__(self, simulate: bool = False) -> None:
        # Simulate if explicitly forced OR if the hardware libs are missing.
        self.simulate: bool = bool(simulate) or not _PICRAWLER_AVAILABLE
        self._pose: Pose = Pose.UNKNOWN
        self._is_moving: bool = False
        self._started_at: float = time.monotonic()
        # Optional forward-clearance reader (cm), injected by the server from the
        # ultrasonic sensor. When set, walk() checks it between gait cycles and
        # aborts early if clearance drops below the reflex distance — a fast
        # on-robot safety stop so a blocking stride can't blindly ram an obstacle.
        self.clearance_fn: Callable[[], float | None] | None = None
        self._reflex_stopped: bool = False
        # Optional 2D sim world (robot/simworld.py). When set (simulate + SIM_WORLD),
        # walk/turn move the virtual robot so the whole stack runs off-hardware.
        self.world = None

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

    @staticmethod
    def _scaled_frame(frame: list[list[int]], scale: float) -> list[list[int]]:
        """Amplify a frame's stride: scale each leg's x/y offset from the stand
        neutral by `scale` (z, the lift/plant, is left as-is). scale=1.0 reproduces
        picrawler's exact frame (proven to walk); >1.0 lengthens the step."""
        out = []
        for i in range(LEG_COUNT):
            nx, ny, _nz = STAND_COORD[i]
            x, y, z = frame[i]
            out.append([round(nx + (x - nx) * scale), round(ny + (y - ny) * scale), z])
        return out

    def _reflex_blocks(self, min_clearance_cm: float | None) -> bool:
        """True if the forward reflex should abort the walk now: the injected
        clearance reader sees less than the reflex distance. Inert when disabled
        or no clearance reader is wired (e.g. ultrasonic off / simulate laptop)."""
        if not config.REFLEX_ENABLED or self.clearance_fn is None:
            return False
        threshold = config.REFLEX_STOP_CM if min_clearance_cm is None else min_clearance_cm
        if threshold <= 0:
            return False
        distance = self.clearance_fn()
        return distance is not None and distance < threshold

    def _custom_walk(self, steps: int, speed: int, min_clearance_cm: float | None) -> None:
        """Custom coordinate gait: play picrawler's forward keyframes via do_step,
        with a tunable stride scale. Enters from a stand so the first frame doesn't jerk.
        Checks the reflex between frames so it can stop mid-stride."""
        if self._pose != Pose.STANDING:
            self.stand()  # staged, safe entry
        scale = config.GAIT_STRIDE_SCALE
        frames = [self._scaled_frame(f, scale) for f in FORWARD_FRAMES]
        for _ in range(max(0, steps)):
            for coords in frames:
                if self._reflex_blocks(min_clearance_cm):
                    self._reflex_stopped = True
                    return
                self._do_step(coords, speed)
            if self.world is not None:
                self.world.advance(config.SIM_STRIDE_CM)

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

    def walk(
        self,
        steps: int,
        speed: int = DEFAULT_SPEED,
        mode: str | None = None,
        min_clearance_cm: float | None = None,
    ) -> None:
        # mode: "canned" (picrawler do_action) or "custom" (coordinate do_step gait).
        # Defaults to config.GAIT_MODE; the tuning tool passes mode="custom" explicitly.
        # Network protocol is unchanged — WalkCommand carries no mode.
        # The reflex checks forward clearance between gait cycles and aborts early
        # (sets reflex_stopped) so a blocking stride can't blindly ram an obstacle.
        mode = (mode or config.GAIT_MODE).lower()
        self._is_moving = True
        self._reflex_stopped = False
        try:
            if mode == "custom":
                self._custom_walk(steps, speed, min_clearance_cm)
            else:
                for _ in range(max(0, steps)):
                    if self._reflex_blocks(min_clearance_cm):
                        self._reflex_stopped = True
                        break
                    self._do_action("forward", 1, speed)
                    if self.world is not None:
                        self.world.advance(config.SIM_STRIDE_CM)
            self._pose = Pose.STANDING
        finally:
            self._is_moving = False

    @property
    def reflex_stopped(self) -> bool:
        """Whether the most recent walk aborted early on the forward reflex."""
        return self._reflex_stopped

    def turn(self, degrees: float, speed: int = DEFAULT_SPEED) -> None:
        # Positive degrees = turn right (clockwise), negative = left.
        # TODO: real custom gait — map degrees to a precise number of turn
        # cycles / a coordinate trajectory. Stage 1 uses one canned turn action.
        action = "turn right" if degrees >= 0 else "turn left"
        self._is_moving = True
        try:
            self._do_action(action, 1, speed)
            if self.world is not None:
                self.world.rotate(degrees)
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
