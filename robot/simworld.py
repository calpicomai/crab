"""A lightweight 2D world so the whole robot can be simulated off-hardware.

The existing ``simulate`` modes are shallow — the gait just logs, the sonar
returns a fixed 80 cm, the camera draws an unrelated block — so nothing actually
moves in a space and the robot never has to avoid anything. ``SimWorld`` fixes
that: it holds the robot's **pose** and a set of **obstacles**, integrates
walk/turn into motion, ray-casts the ultrasonic against the world, and renders a
first-person camera view — so wander/agent/pet run end-to-end against a real
virtual environment (and the dashboard can draw it).

It is **kinematic / behavior-level**, not physics: walk advances a fixed stride,
turn rotates by the commanded degrees (idealized odometry — the same assumption
the costmap's dead-reckoning already makes). It validates behavior/logic
(avoidance, costmap, reflex, moods), not servo dynamics or slippage.

Geometry: world in cm, x right / y up (top-down). Heading is degrees CCW from +x;
forward = (cos, sin). Bearings are right-positive (clockwise), matching the
robot's ``turn`` sign and the costmap. Pure-Python + Pillow (no numpy) so the Pi
node stays minimal. Thread-safe: the server touches it from request + camera
threads.
"""

from __future__ import annotations

import math
import threading

# Distinct fill colors for obstacles so the sim camera renders them separately
# (and the simblob detector can tell them apart). RGB.
_PALETTE = [
    (220, 60, 60), (60, 120, 220), (230, 180, 40), (150, 70, 200),
    (40, 190, 160), (240, 120, 40), (200, 80, 160), (120, 200, 70),
]


def _norm180(deg: float) -> float:
    """Wrap to (-180, 180]."""
    d = (deg + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


class Obstacle:
    __slots__ = ("cx", "cy", "r", "label", "color")

    def __init__(self, cx: float, cy: float, r: float, label: str, color: tuple[int, int, int]) -> None:
        self.cx, self.cy, self.r, self.label, self.color = cx, cy, r, label, color

    def as_dict(self) -> dict:
        return {"cx": round(self.cx, 1), "cy": round(self.cy, 1), "r": round(self.r, 1),
                "label": self.label, "color": list(self.color)}


class SimWorld:
    def __init__(
        self,
        width_cm: float = 300.0,
        height_cm: float = 300.0,
        sonar_cone_deg: float = 20.0,
        sonar_max_cm: float = 150.0,
        camera_fov_deg: float = 54.0,
    ) -> None:
        self.width = float(width_cm)
        self.height = float(height_cm)
        self.sonar_cone = float(sonar_cone_deg)
        self.sonar_max = float(sonar_max_cm)
        self.camera_fov = float(camera_fov_deg)
        self._lock = threading.RLock()
        self.obstacles: list[Obstacle] = []
        self._start = (self.width / 2.0, self.height / 2.0, 0.0)
        self.x, self.y, self.heading = self._start
        self.paused = False
        self.trail: list[tuple[float, float]] = [(self.x, self.y)]
        self._last_sonar = self.sonar_max
        self._color_i = 0

    # ------------------------------------------------------------------ #
    # Mutation (motion) — no-ops while paused so the dashboard can freeze it.
    # ------------------------------------------------------------------ #
    def advance(self, dist_cm: float) -> None:
        with self._lock:
            if self.paused:
                return
            th = math.radians(self.heading)
            nx = self.x + dist_cm * math.cos(th)
            ny = self.y + dist_cm * math.sin(th)
            # Clamp to the room (a wall stops you); a little margin for the body.
            m = 8.0
            self.x = min(self.width - m, max(m, nx))
            self.y = min(self.height - m, max(m, ny))
            self.trail.append((self.x, self.y))
            if len(self.trail) > 400:
                self.trail = self.trail[-400:]

    def rotate(self, deg: float) -> None:
        with self._lock:
            if self.paused:
                return
            # turn(deg): positive = right = clockwise. In CCW heading, that's a decrease.
            self.heading = _norm180(self.heading - deg)

    def reset(self) -> None:
        with self._lock:
            self.x, self.y, self.heading = self._start
            self.trail = [(self.x, self.y)]

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.paused = bool(paused)

    def add_obstacle(self, cx: float, cy: float, r: float = 14.0, label: str = "obstacle") -> None:
        with self._lock:
            color = _PALETTE[self._color_i % len(_PALETTE)]
            self._color_i += 1
            self.obstacles.append(Obstacle(float(cx), float(cy), float(r), label, color))

    def remove_near(self, cx: float, cy: float, radius: float = 25.0) -> bool:
        with self._lock:
            for i, o in enumerate(self.obstacles):
                if math.hypot(o.cx - cx, o.cy - cy) <= radius + o.r:
                    del self.obstacles[i]
                    return True
        return False

    def clear_obstacles(self) -> None:
        with self._lock:
            self.obstacles = []
            self._color_i = 0

    # ------------------------------------------------------------------ #
    # Sensing
    # ------------------------------------------------------------------ #
    def _ray_hit(self, ox: float, oy: float, ang_rad: float) -> float:
        """Nearest hit distance of a ray from (ox,oy) heading ang_rad, against all
        obstacles + the boundary walls. Returns sonar_max if nothing closer."""
        dx, dy = math.cos(ang_rad), math.sin(ang_rad)
        best = self.sonar_max
        # Obstacles (circles): |(O + t d) - C|^2 = r^2.
        for o in self.obstacles:
            fx, fy = ox - o.cx, oy - o.cy
            b = 2.0 * (fx * dx + fy * dy)
            c = fx * fx + fy * fy - o.r * o.r
            disc = b * b - 4.0 * c
            if disc < 0:
                continue
            sq = math.sqrt(disc)
            for t in ((-b - sq) / 2.0, (-b + sq) / 2.0):
                if 0.0 <= t < best:
                    best = t
        # Boundary walls: x=0, x=W, y=0, y=H.
        for t in self._wall_ts(ox, oy, dx, dy):
            if 0.0 <= t < best:
                best = t
        return best

    def _wall_ts(self, ox: float, oy: float, dx: float, dy: float):
        ts = []
        if abs(dx) > 1e-9:
            for wx in (0.0, self.width):
                t = (wx - ox) / dx
                if t >= 0 and 0 <= oy + t * dy <= self.height:
                    ts.append(t)
        if abs(dy) > 1e-9:
            for wy in (0.0, self.height):
                t = (wy - oy) / dy
                if t >= 0 and 0 <= ox + t * dx <= self.width:
                    ts.append(t)
        return ts

    def sonar(self) -> float:
        """Forward ultrasonic clearance (cm): the min over a fan of rays across the
        beam cone. Never returns None here (no-echo is modeled as sonar_max)."""
        with self._lock:
            th = math.radians(self.heading)
            half = math.radians(self.sonar_cone / 2.0)
            best = self.sonar_max
            n = 7
            for i in range(n):
                off = -half + (2 * half) * (i / (n - 1)) if n > 1 else 0.0
                best = min(best, self._ray_hit(self.x, self.y, th + off))
            self._last_sonar = best
            return round(best, 1)

    def visible(self):
        """Obstacles within the camera FOV: (bearing_deg_right_positive, dist_cm,
        radius_cm, label, color), nearest last (for painter's-order rendering)."""
        with self._lock:
            th = self.heading
            out = []
            for o in self.obstacles:
                dc = math.hypot(o.cx - self.x, o.cy - self.y)
                if dc <= 1e-6:
                    continue
                world_ang = math.degrees(math.atan2(o.cy - self.y, o.cx - self.x))
                bearing = _norm180(-(world_ang - th))  # right-positive
                ang_r = math.degrees(math.asin(min(1.0, o.r / dc)))  # angular half-size
                if abs(bearing) <= self.camera_fov / 2.0 + ang_r:
                    out.append((bearing, max(0.0, dc - o.r), o.r, o.label, o.color))
            out.sort(key=lambda v: -v[1])  # far first
            return out

    # ------------------------------------------------------------------ #
    # Rendering (first-person camera)
    # ------------------------------------------------------------------ #
    def render_camera(self, width: int, height: int):
        """A simple first-person view: obstacles as their solid color boxes, placed
        by bearing and sized by proximity. Returns a PIL.Image (RGB)."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), (135, 170, 200))  # sky
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, height // 2, width, height], fill=(70, 90, 70))  # ground
        half_fov = self.camera_fov / 2.0
        for bearing, dist, radius, _label, color in self.visible():
            # Horizontal placement from bearing; box size grows as distance shrinks.
            px = width / 2.0 + (bearing / half_fov) * (width / 2.0)
            scale = max(6.0, min(height * 0.9, (radius * 2.0) / max(dist, 1.0) * 900.0))
            x0, x1 = px - scale / 2.0, px + scale / 2.0
            y_mid = height / 2.0
            y0, y1 = y_mid - scale / 2.0, y_mid + scale / 2.0
            draw.rectangle([x0, y0, x1, y1], fill=color, outline=(20, 20, 20))
        return img

    # ------------------------------------------------------------------ #
    # State for the dashboard
    # ------------------------------------------------------------------ #
    def state(self) -> dict:
        with self._lock:
            return {
                "width": self.width, "height": self.height,
                "robot": {"x": round(self.x, 1), "y": round(self.y, 1), "heading": round(self.heading, 1)},
                "obstacles": [o.as_dict() for o in self.obstacles],
                "sonar": {"distance": round(self._last_sonar, 1), "cone": self.sonar_cone, "max": self.sonar_max},
                "trail": [[round(x, 1), round(y, 1)] for x, y in self.trail[-200:]],
                "paused": self.paused,
            }


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #
def _mk(world: SimWorld, obstacles: list[tuple[float, float, float, str]]) -> SimWorld:
    for cx, cy, r, label in obstacles:
        world.add_obstacle(cx, cy, r, label)
    return world


def build_scenario(name: str) -> SimWorld:
    """Return a SimWorld for a named preset. Unknown -> 'poles'."""
    name = (name or "poles").strip().lower()
    if name == "empty" or name == "room":
        return SimWorld(300, 300)
    if name == "corridor":
        w = SimWorld(360, 300)
        obs = [(x, 110, 12, "wall") for x in range(60, 320, 40)] + \
              [(x, 190, 12, "wall") for x in range(60, 320, 40)]
        return _mk(w, obs)
    if name == "slalom":
        w = SimWorld(300, 360)
        obs = [(90 if i % 2 == 0 else 210, 70 + i * 55, 16, "pole") for i in range(5)]
        return _mk(w, obs)
    # default: poles scattered around a room
    w = SimWorld(320, 320)
    return _mk(w, [
        (120, 110, 16, "pole"), (220, 160, 18, "pole"),
        (90, 230, 20, "box"), (240, 250, 14, "pole"), (170, 300, 16, "chair leg"),
    ])
