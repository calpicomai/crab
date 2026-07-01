"""Local occupancy costmap — autonomy v2's spatial model.

A robot-centered **polar occupancy histogram** (Vector Field Histogram style):
the forward arc is split into angular bins over +/-``fov_deg`` (0 deg = straight
ahead). Each bin holds an obstacle **confidence** in ``[0, 1]`` and the nearest
estimated **range** (cm). The two obstacle senses write into it where each is
strong:

    * ultrasonic -> an accurate range at the forward bins (a cone ~20 deg wide),
    * camera     -> a bearing per detection (from its pixel x-center), with a
      coarse range from box size, at slightly lower confidence.

Combining by ``max`` (never by overwrite) is deliberate: a thin pole the narrow
sonar beam misses but the camera sees stays blocked instead of being cleared by
a "sonar sees nothing ahead" reading — the exact case that let the robot walk
into a pole. Stale evidence fades via a per-cycle ``decay`` (short memory), which
also bounds the open-loop dead-reckoning error (no IMU): after each turn the
histogram is rotated by the *commanded* degrees.

Reading the map ("which way can I go") inflates every obstacle by the robot's own
angular half-width at its range (so we never aim at a gap the body won't fit) and
returns the clearest wide-enough heading. This is a LOCAL, ephemeral model of
free-vs-blocked *directions* (Roomba-class reactive avoidance) — deliberately NOT
a metric world/house map, which this robot's sensors (fixed sonar, mono camera,
no odometry/IMU/depth) can't support.

Pure Python / numpy-free so it runs and is testable anywhere. Self-test:
    python -m brain.costmap
"""

from __future__ import annotations

import math

from . import config

# Confidence written by a fresh camera detection is scaled by this vs. sonar, as
# its range estimate (from box size, no depth) is coarser than a sonar echo.
_CAMERA_WEIGHT = 0.85


class LocalCostmap:
    """Robot-centered polar occupancy histogram. All angles in degrees, 0 = ahead,
    positive = the robot's right (matching ``RobotClient.turn`` sign)."""

    def __init__(
        self,
        bins: int | None = None,
        fov_deg: float | None = None,
        camera_hfov_deg: float | None = None,
        sonar_beam_deg: float | None = None,
        footprint_radius_cm: float | None = None,
        decay: float | None = None,
        blocked_conf: float | None = None,
        max_range_cm: float | None = None,
        min_gap_deg: float | None = None,
        clearance_cm: float | None = None,
    ) -> None:
        self.bins = int(bins if bins is not None else config.COSTMAP_BINS)
        self.fov = float(fov_deg if fov_deg is not None else config.COSTMAP_FOV_DEG)
        self.camera_hfov = float(camera_hfov_deg if camera_hfov_deg is not None else config.CAMERA_HFOV_DEG)
        self.sonar_beam = float(sonar_beam_deg if sonar_beam_deg is not None else config.SONAR_BEAM_DEG)
        self.footprint = float(footprint_radius_cm if footprint_radius_cm is not None else config.FOOTPRINT_RADIUS_CM)
        self.decay_factor = float(decay if decay is not None else config.COSTMAP_DECAY)
        self.blocked_conf = float(blocked_conf if blocked_conf is not None else config.COSTMAP_BLOCKED_CONF)
        self.max_range = float(max_range_cm if max_range_cm is not None else config.COSTMAP_MAX_RANGE_CM)
        self.clearance = float(clearance_cm if clearance_cm is not None else config.COSTMAP_CLEARANCE_CM)

        # Angular width of one bin, and the center bearing of each bin.
        self.bin_width = (2.0 * self.fov) / self.bins
        self._centers = [-self.fov + (i + 0.5) * self.bin_width for i in range(self.bins)]

        # Minimum passable gap: explicit override, else the angular width the
        # robot footprint subtends at the clearance range (its own size).
        override = float(min_gap_deg if min_gap_deg is not None else config.MIN_GAP_DEG)
        if override > 0:
            self.min_gap = override
        else:
            self.min_gap = 2.0 * math.degrees(math.atan2(self.footprint, self.clearance))

        self.conf = [0.0] * self.bins
        self.range = [self.max_range] * self.bins

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _bin_for_bearing(self, bearing: float) -> int | None:
        """Index of the bin containing ``bearing``, or None if outside the FOV."""
        if bearing < -self.fov or bearing > self.fov:
            return None
        idx = int((bearing + self.fov) / self.bin_width)
        return max(0, min(self.bins - 1, idx))

    def _conf_from_range(self, range_cm: float) -> float:
        """Closer -> higher confidence. 0 beyond max_range, 1 at the robot."""
        if range_cm >= self.max_range:
            return 0.0
        if range_cm <= 0:
            return 1.0
        return max(0.0, min(1.0, (self.max_range - range_cm) / self.max_range))

    def _write(self, idx: int, conf: float, range_cm: float) -> None:
        """Fuse evidence into a bin: confidence combines by max (see module docs),
        range keeps the nearest."""
        if conf <= 0:
            return
        if conf > self.conf[idx]:
            self.conf[idx] = conf
        if range_cm < self.range[idx]:
            self.range[idx] = range_cm

    # ------------------------------------------------------------------ #
    # Sensor integration
    # ------------------------------------------------------------------ #
    def integrate_sonar(self, distance_cm: float | None) -> None:
        """Splat a forward ultrasonic reading across the sonar beam arc. ``None``
        (no echo) contributes nothing — decay handles the fade."""
        if distance_cm is None:
            return
        conf = self._conf_from_range(distance_cm)
        if conf <= 0:
            return
        half = self.sonar_beam / 2.0
        for idx, center in enumerate(self._centers):
            if abs(center) <= half:
                self._write(idx, conf, distance_cm)

    def integrate_camera(self, snapshot: dict) -> None:
        """Write each detection as an arc from its box's left edge to its right
        edge, at a bearing from the pixel x-center and a coarse range from box
        size. Consumes the perception ``/snapshot`` JSON (PerceptionSnapshot)."""
        width = snapshot.get("width") or 0
        height = snapshot.get("height") or 0
        if not width or not height:
            return
        for det in snapshot.get("detections", []):
            box = det.get("box")
            if not box or len(box) != 4:
                continue
            x1, y1, x2, y2 = box
            # Coarse proximity, no depth: a nearer obstacle is large in at least
            # one dimension, so use the max of its width/height fraction — this
            # (unlike area) keeps a thin-but-close thing (a pole) reading as near.
            dim_frac = max((x2 - x1) / width, (y2 - y1) / height)
            dim_frac = max(0.0, min(1.0, dim_frac))
            est_range = max(5.0, self.max_range * (1.0 - dim_frac))
            conf = self._conf_from_range(est_range) * _CAMERA_WEIGHT
            if conf <= 0:
                continue
            bearing_l = ((x1 / width) - 0.5) * self.camera_hfov
            bearing_r = ((x2 / width) - 0.5) * self.camera_hfov
            lo, hi = min(bearing_l, bearing_r), max(bearing_l, bearing_r)
            # Always mark the bin under the box center (a detection narrower than
            # one bin would otherwise fall between bin centers and write nothing),
            # then any further bins the box arc spans.
            mid = self._bin_for_bearing((lo + hi) / 2.0)
            if mid is not None:
                self._write(mid, conf, est_range)
            for idx, center in enumerate(self._centers):
                if lo <= center <= hi:
                    self._write(idx, conf, est_range)

    # ------------------------------------------------------------------ #
    # Memory: decay + dead-reckoning
    # ------------------------------------------------------------------ #
    def decay(self) -> None:
        """Fade every bin's confidence one cycle (short memory)."""
        self.conf = [c * self.decay_factor for c in self.conf]

    def apply_motion(self, turn_deg: float = 0.0, walked: bool = False) -> None:
        """Open-loop dead-reckoning from the command we just issued (no IMU).

        A turn of ``turn_deg`` (positive = right) shifts every obstacle's bearing
        by ``-turn_deg`` relative to the new heading; we resample the histogram
        accordingly. Walking forward makes the forward reading stale (the obstacle
        is now closer or passed), so we extra-decay the forward bins — fresh sonar
        repopulates them next cycle.
        """
        if turn_deg:
            new_conf = [0.0] * self.bins
            new_range = [self.max_range] * self.bins
            for i, center in enumerate(self._centers):
                # What is now at bearing `center` was previously at `center + turn`.
                src = self._bin_for_bearing(center + turn_deg)
                if src is not None:
                    new_conf[i] = self.conf[src]
                    new_range[i] = self.range[src]
            self.conf, self.range = new_conf, new_range
        if walked:
            half = self.sonar_beam / 2.0
            for idx, center in enumerate(self._centers):
                if abs(center) <= half:
                    self.conf[idx] *= 0.5

    # ------------------------------------------------------------------ #
    # Reading the map
    # ------------------------------------------------------------------ #
    def _blocked_mask(self) -> list[bool]:
        """Blocked bins, size-inflated by the robot's angular half-width at each
        obstacle's range (so a gap the body can't fit through reads as blocked)."""
        blocked = [c >= self.blocked_conf for c in self.conf]
        inflated = list(blocked)
        for i, is_blocked in enumerate(blocked):
            if not is_blocked:
                continue
            rng = self.range[i] if self.range[i] > 0 else self.clearance
            inflate_deg = math.degrees(math.atan2(self.footprint, max(rng, 1.0)))
            reach = int(math.ceil(inflate_deg / self.bin_width))
            for j in range(max(0, i - reach), min(self.bins, i + reach + 1)):
                inflated[j] = True
        return inflated

    def _valleys(self, blocked: list[bool]) -> list[tuple[int, int]]:
        """Contiguous runs of free bins as (start_idx, end_idx_inclusive)."""
        valleys: list[tuple[int, int]] = []
        start = None
        for i, b in enumerate(blocked):
            if not b and start is None:
                start = i
            elif b and start is not None:
                valleys.append((start, i - 1))
                start = None
        if start is not None:
            valleys.append((start, self.bins - 1))
        return valleys

    def best_heading(self) -> tuple[float, bool]:
        """Pick a steering heading (deg) from the histogram.

        Returns ``(heading_deg, forward_clear)``. ``forward_clear`` is True when a
        passable gap contains straight-ahead (0 deg) — the caller should walk.
        Otherwise ``heading_deg`` points at the nearest passable gap's center; if
        no gap is wide enough it points hard toward the more-open side (the caller
        should rotate-to-scan / turn to open room).
        """
        blocked = self._blocked_mask()
        valleys = self._valleys(blocked)
        passable = [(s, e) for (s, e) in valleys if (e - s + 1) * self.bin_width >= self.min_gap]

        # Forward clear? A passable gap straddling bearing 0 -> keep going straight.
        for s, e in passable:
            if self._centers[s] <= 0.0 <= self._centers[e]:
                return 0.0, True

        # Else steer at the passable gap whose center is nearest forward.
        if passable:
            best = min(passable, key=lambda se: abs((self._centers[se[0]] + self._centers[se[1]]) / 2.0))
            center = (self._centers[best[0]] + self._centers[best[1]]) / 2.0
            return center, False

        # Boxed in: no gap wide enough. Turn hard toward the more-open half.
        mid = self.bins // 2
        left_conf = sum(self.conf[:mid])
        right_conf = sum(self.conf[mid:])
        return (self.fov if left_conf > right_conf else -self.fov), False

    # ------------------------------------------------------------------ #
    # Visualization
    # ------------------------------------------------------------------ #
    def render_ascii(self) -> str:
        """One-line polar bar for logs / the self-test: ``#`` blocked (inflated),
        ``:`` weak evidence, space free; ``^`` marks the chosen heading bin."""
        blocked = self._blocked_mask()
        chars = []
        for i in range(self.bins):
            if blocked[i]:
                chars.append("#")
            elif self.conf[i] > 0.1:
                chars.append(":")
            else:
                chars.append(" ")
        heading, forward_clear = self.best_heading()
        hidx = self._bin_for_bearing(heading)
        if hidx is not None:
            chars[hidx] = "^"
        bar = "".join(chars)
        return f"L[{bar}]R head={heading:+.0f} fwd={'ok' if forward_clear else 'x'}"


def _self_test() -> int:
    """Feed synthetic scenes and print the histogram + chosen heading."""
    print("clear scene (sonar 100cm, no detections):")
    cm = LocalCostmap()
    cm.integrate_sonar(100.0)
    cm.integrate_camera({"width": 640, "height": 480, "detections": []})
    print("  ", cm.render_ascii())
    h, fwd = cm.best_heading()
    assert fwd, "clear scene should be forward-clear"
    assert abs(h) < 1e-6

    print("obstacle dead ahead (sonar 25cm):")
    cm = LocalCostmap()
    cm.integrate_sonar(25.0)
    print("  ", cm.render_ascii())
    h, fwd = cm.best_heading()
    assert not fwd, "blocked ahead should not be forward-clear"
    assert abs(h) > 1e-6, "should steer off-center"

    print("camera pole on the left (sonar misses it):")
    cm = LocalCostmap()
    cm.integrate_sonar(None)  # no echo — narrow beam missed the thin pole
    cm.integrate_camera({
        "width": 640, "height": 480,
        "detections": [{"label": "a pole", "score": 0.6, "box": [40, 100, 120, 460], "source": "nanoowl"}],
    })
    print("  ", cm.render_ascii())
    h, fwd = cm.best_heading()
    assert h > 0, "pole on the left -> steer right"

    print("dead-reckoning: turn right 30deg shifts a left obstacle further left:")
    cm = LocalCostmap()
    cm.integrate_camera({
        "width": 640, "height": 480,
        "detections": [{"label": "x", "score": 0.9, "box": [0, 100, 80, 460], "source": "nanoowl"}],
    })
    before = cm.render_ascii()
    cm.apply_motion(turn_deg=30.0)
    print("   before:", before)
    print("   after :", cm.render_ascii())

    print("decay drops confidence:")
    cm = LocalCostmap()
    cm.integrate_sonar(20.0)
    peak = max(cm.conf)
    cm.decay()
    assert max(cm.conf) < peak
    print(f"   {peak:.2f} -> {max(cm.conf):.2f}")

    print("\nAll self-test assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
