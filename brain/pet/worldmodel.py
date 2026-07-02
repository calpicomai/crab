"""The pet's world model — the substrate that makes behavior feel *intentional*.

Three learned, persistent things, grown from what the pet actually sees and does
(SQLite, local, no cloud — mirrors ``memory.py``'s single-connection pattern; the
mind thread is the only writer):

* **objects** — every labelled thing it has seen (a cat, a chair, a person), with
  how often, where it last was (bearing/range), and how it *felt* about it
  (valence). Drives recognition ("that chair again") and novelty/curiosity.
* **places** — a *sense of place* WITHOUT a metric map (this robot has no
  odometry/depth): a place is a **semantic fingerprint** = the set of labels
  currently in view. ``recognize`` matches the current view to remembered places
  by label overlap (Jaccard), so it can say "I know this spot" — Roomba-class, not
  SLAM.
* **outcomes** — an action→outcome tally per rough context (forward-clearance band
  × mood): "walking when it's tight usually reflex-stops". ``predict`` reads it so
  the body can act with a little foresight instead of blind hesitation. Frequency
  stats (Laplace-smoothed), NOT a neural predictor — honest for the hardware.

It also turns raw detections into a **target** to pursue (``salient_target``): the
most *interesting* thing in view, where interest = an intrinsic per-label weight
(cats/dogs are exciting) modulated by novelty (familiar things get boring). That's
what lets the pet chase a cat instead of only avoiding obstacles.

Config is read lazily (only when an arg is omitted) so the self-test runs anywhere:
    python -m brain.pet.worldmodel
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass

# Detection sources whose boxes are fabricated (never fuse into world knowledge) —
# same rule the costmap uses (brain/costmap.py:integrate_camera).
_FAKE_SOURCES = {"dummy"}


@dataclass
class Target:
    """The thing the pet currently wants to go to. ``drive`` is how it feels about
    it: 'chase' (a cat!), 'approach' (mildly interesting), or 'avoid'."""

    label: str
    bearing_deg: float   # 0 = straight ahead, + = the robot's right (costmap sign)
    range_cm: float
    drive: str
    interest: float


def _clearance_band(distance_cm: float | None) -> str:
    """Coarse forward-clearance bucket for context/fingerprint (no false precision)."""
    if distance_cm is None:
        return "open"          # no echo == treat as open ahead
    if distance_cm < 30:
        return "tight"
    if distance_cm < 80:
        return "near"
    return "open"


class WorldModel:
    """Persistent places + objects + action→outcome model. See module docstring."""

    def __init__(
        self,
        path: str | None = None,
        camera_hfov: float | None = None,
        chase_labels: list[str] | None = None,
        interest_labels: list[str] | None = None,
        max_range_cm: float | None = None,
        place_match: float = 0.5,
    ) -> None:
        # Lazy config: only import the (pydantic-dependent) config chain when a
        # value wasn't supplied, so tests can pass everything explicitly.
        if path is None or camera_hfov is None or max_range_cm is None \
                or chase_labels is None or interest_labels is None:
            from .. import config as brain_config  # brain/config.py: stdlib only
            from . import config as pet_config
            path = path if path is not None else pet_config.PET_WORLD_DB
            camera_hfov = camera_hfov if camera_hfov is not None else brain_config.CAMERA_HFOV_DEG
            max_range_cm = max_range_cm if max_range_cm is not None else brain_config.COSTMAP_MAX_RANGE_CM
            chase_labels = chase_labels if chase_labels is not None else pet_config.PET_CHASE_LABELS
            interest_labels = interest_labels if interest_labels is not None else pet_config.PET_INTEREST_LABELS

        self.camera_hfov = float(camera_hfov)
        self.max_range = float(max_range_cm)
        self.place_match = float(place_match)
        self.chase_labels = [s.strip().lower() for s in chase_labels if s.strip()]
        self.interest_labels = [s.strip().lower() for s in interest_labels if s.strip()]

        # Latest recognition state (read by summary()/the dashboard; written by observe()).
        self.place_id: int | None = None
        self.place_familiarity: int = 0       # how many times this place has been visited
        self.visible: list[str] = []

        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        c = self._conn
        c.execute(
            """CREATE TABLE IF NOT EXISTS objects(
                 label TEXT PRIMARY KEY, times_seen INTEGER DEFAULT 0,
                 first_ts REAL, last_ts REAL,
                 last_bearing REAL, last_range_cm REAL, valence REAL DEFAULT 0)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS places(
                 id INTEGER PRIMARY KEY AUTOINCREMENT, labels TEXT,
                 visits INTEGER DEFAULT 1, valence REAL DEFAULT 0,
                 first_ts REAL, last_ts REAL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS outcomes(
                 context TEXT, action TEXT, tries INTEGER DEFAULT 0,
                 reflex INTEGER DEFAULT 0, progressed INTEGER DEFAULT 0,
                 PRIMARY KEY(context, action))"""
        )
        c.commit()

    # ------------------------------------------------------------------ #
    # Detections -> labels / bearings
    # ------------------------------------------------------------------ #
    def _real_detections(self, snapshot: dict) -> list[dict]:
        """Detections worth learning from — drop fabricated (dummy) / simulate ones,
        exactly like the costmap, so phantom boxes never pollute world knowledge."""
        if not snapshot or snapshot.get("simulate"):
            return []
        out = []
        for det in snapshot.get("detections", []):
            if det.get("source") in _FAKE_SOURCES:
                continue
            if det.get("box") and len(det["box"]) == 4 and det.get("label"):
                out.append(det)
        return out

    def _bearing(self, box: list[int], width: int) -> float:
        cx = (box[0] + box[2]) / 2.0
        return ((cx / width) - 0.5) * self.camera_hfov

    def _range(self, box: list[int], width: int, height: int) -> float:
        dim_frac = max((box[2] - box[0]) / width, (box[3] - box[1]) / height)
        dim_frac = max(0.0, min(1.0, dim_frac))
        return max(5.0, self.max_range * (1.0 - dim_frac))

    @staticmethod
    def _norm(label: str) -> str:
        # YOLO gives "cat"; NanoOWL gives the prompt "a cat" — normalise for matching.
        return label.strip().lower()

    def _matches(self, label: str, keywords: list[str]) -> bool:
        lab = self._norm(label)
        return any(k in lab for k in keywords)

    # ------------------------------------------------------------------ #
    # Observe: update objects + current place
    # ------------------------------------------------------------------ #
    def observe(self, snapshot: dict, status: dict | None = None) -> None:
        """Learn from one perception snapshot: upsert each seen object and match/insert
        the current place from the set of visible labels."""
        dets = self._real_detections(snapshot)
        width = snapshot.get("width") or 0
        height = snapshot.get("height") or 0
        now = time.time()
        labels_now: list[str] = []
        for det in dets:
            label = self._norm(det["label"])
            labels_now.append(label)
            bearing = self._bearing(det["box"], width) if width else 0.0
            rng = self._range(det["box"], width, height) if width and height else self.max_range
            row = self._conn.execute("SELECT times_seen FROM objects WHERE label=?", (label,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO objects(label,times_seen,first_ts,last_ts,last_bearing,last_range_cm) "
                    "VALUES(?,?,?,?,?,?)", (label, 1, now, now, bearing, rng))
            else:
                self._conn.execute(
                    "UPDATE objects SET times_seen=times_seen+1, last_ts=?, last_bearing=?, last_range_cm=? "
                    "WHERE label=?", (now, bearing, rng, label))
        self.visible = sorted(set(labels_now))
        self._update_place(self.visible, now)
        self._conn.commit()

    def _update_place(self, labels: list[str], now: float) -> None:
        """Match the current label-set to a remembered place (Jaccard), else create one."""
        if not labels:
            self.place_id = None
            self.place_familiarity = 0
            return
        cur = set(labels)
        best_id, best_j, best_visits = None, 0.0, 0
        for row in self._conn.execute("SELECT id, labels, visits FROM places"):
            other = set((row["labels"] or "").split(","))
            union = cur | other
            j = len(cur & other) / len(union) if union else 0.0
            if j > best_j:
                best_id, best_j, best_visits = row["id"], j, row["visits"]
        if best_id is not None and best_j >= self.place_match:
            self._conn.execute("UPDATE places SET visits=visits+1, last_ts=? WHERE id=?", (now, best_id))
            self.place_id = best_id
            self.place_familiarity = best_visits + 1
        else:
            cur_labels = ",".join(sorted(cur))
            c = self._conn.execute(
                "INSERT INTO places(labels,visits,first_ts,last_ts) VALUES(?,?,?,?)",
                (cur_labels, 1, now, now))
            self.place_id = c.lastrowid
            self.place_familiarity = 1

    # ------------------------------------------------------------------ #
    # Curiosity / targeting
    # ------------------------------------------------------------------ #
    def _times_seen(self, label: str) -> int:
        row = self._conn.execute("SELECT times_seen FROM objects WHERE label=?", (self._norm(label),)).fetchone()
        return int(row["times_seen"]) if row else 0

    def _weight(self, label: str) -> float:
        if self._matches(label, self.chase_labels):
            return 1.0
        if self._matches(label, self.interest_labels):
            return 0.6
        return 0.12

    def interest(self, label: str) -> float:
        """0..1 how much the pet wants to go to this. Chase labels stay exciting even
        when familiar; others fade with familiarity (boredom of the seen-it-all)."""
        w = self._weight(label)
        if self._matches(label, self.chase_labels):
            return w
        novelty = 1.0 / (1.0 + 0.5 * self._times_seen(label))   # 1.0 fresh -> ~0 very familiar
        return w * (0.3 + 0.7 * novelty)

    def salient_target(self, snapshot: dict, min_interest: float = 0.35) -> Target | None:
        """The most interesting thing in view worth going to, or None."""
        width = snapshot.get("width") or 0
        height = snapshot.get("height") or 0
        best: Target | None = None
        for det in self._real_detections(snapshot):
            label = self._norm(det["label"])
            score = self.interest(label)
            if score < min_interest:
                continue
            if best is None or score > best.interest:
                drive = "chase" if self._matches(label, self.chase_labels) else "approach"
                best = Target(
                    label=label,
                    bearing_deg=self._bearing(det["box"], width) if width else 0.0,
                    range_cm=self._range(det["box"], width, height) if width and height else self.max_range,
                    drive=drive,
                    interest=score,
                )
        return best

    # ------------------------------------------------------------------ #
    # Action -> outcome (learned foresight)
    # ------------------------------------------------------------------ #
    @staticmethod
    def context_key(distance_cm: float | None, mood: str | None) -> str:
        return f"{_clearance_band(distance_cm)}/{(mood or '?')}"

    def record(self, context: str, action: str, *, reflex: bool = False, progressed: bool = False) -> None:
        self._conn.execute(
            "INSERT INTO outcomes(context,action,tries,reflex,progressed) VALUES(?,?,1,?,?) "
            "ON CONFLICT(context,action) DO UPDATE SET tries=tries+1, reflex=reflex+?, progressed=progressed+?",
            (context, action, int(reflex), int(progressed), int(reflex), int(progressed)))
        self._conn.commit()

    def predict(self, context: str, action: str) -> dict:
        """Laplace-smoothed outcome estimate for (context, action)."""
        row = self._conn.execute(
            "SELECT tries,reflex,progressed FROM outcomes WHERE context=? AND action=?",
            (context, action)).fetchone()
        tries = row["tries"] if row else 0
        reflex = row["reflex"] if row else 0
        prog = row["progressed"] if row else 0
        return {
            "n": tries,
            "reflex_p": (reflex + 1) / (tries + 2),
            "progress_p": (prog + 1) / (tries + 2),
        }

    # ------------------------------------------------------------------ #
    # Summary for the VLM prompt + the dashboard
    # ------------------------------------------------------------------ #
    def top_objects(self, n: int = 5) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT label, times_seen FROM objects ORDER BY times_seen DESC LIMIT ?", (n,)).fetchall()
        return [(r["label"], r["times_seen"]) for r in rows]

    def summary(self, context: str | None = None) -> str:
        bits: list[str] = []
        if self.place_id is not None:
            bits.append("a familiar spot" if self.place_familiarity > 1
                        else "somewhere new")
        if self.visible:
            bits.append("you see " + ", ".join(self.visible))
        top = self.top_objects(4)
        if top:
            bits.append("known: " + ", ".join(f"{lab}×{n}" for lab, n in top))
        if context:
            walk = self.predict(context, "walk")
            if walk["n"] >= 3:
                bits.append(f"walking here stalls ~{round(walk['reflex_p'] * 100)}%")
        return "; ".join(bits) if bits else "(the world is still new)"

    def object_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0])

    def place_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM places").fetchone()[0])

    def close(self) -> None:
        self._conn.close()


def _snap(width, height, dets):
    return {"width": width, "height": height, "simulate": False,
            "detections": [{"label": lab, "score": 0.9, "box": box, "source": src}
                           for (lab, box, src) in dets]}


def _self_test() -> int:
    hfov = 54.0
    wm = WorldModel(":memory:", camera_hfov=hfov, max_range_cm=120.0,
                    chase_labels=["cat", "dog"], interest_labels=["person", "ball"])

    # A cat slightly to the right, plus a chair.
    snap = _snap(640, 480, [("cat", [360, 120, 460, 400], "yolo"),
                            ("chair", [40, 200, 130, 460], "yolo")])
    wm.observe(snap, {"distance_cm": 90})
    assert wm.object_count() == 2
    assert wm.place_id is not None and wm.place_familiarity == 1

    # The cat should be the salient target (chase), bearing to the right (+).
    t = wm.salient_target(snap)
    assert t is not None and t.label == "cat" and t.drive == "chase", t
    assert t.bearing_deg > 0, t.bearing_deg
    print(f"  target: {t.label} drive={t.drive} bearing={t.bearing_deg:+.0f} interest={t.interest:.2f}")

    # A dummy box is ignored (no phantom targets / objects).
    dummy = _snap(640, 480, [("person", [160, 120, 480, 440], "dummy")])
    wm.observe(dummy, {})
    assert wm.salient_target(dummy) is None
    assert "person" not in [l for l, _ in wm.top_objects(10)]

    # Revisit the same scene -> the place is recognised (familiarity rises).
    wm.observe(snap, {"distance_cm": 90})
    assert wm.place_count() == 1, wm.place_count()
    assert wm.place_familiarity == 2, wm.place_familiarity
    print(f"  place recognised: id={wm.place_id} familiarity={wm.place_familiarity}")

    # Chase labels stay interesting even when very familiar; a wall gets boring.
    for _ in range(10):
        wm.observe(_snap(640, 480, [("cat", [300, 120, 400, 400], "yolo"),
                                    ("wall", [0, 0, 640, 480], "yolo")]), {})
    assert wm.interest("cat") > wm.interest("wall")
    assert wm.interest("wall") < 0.12, wm.interest("wall")

    # Action->outcome: walking in "tight" keeps reflex-stopping -> predicted high.
    ctx = WorldModel.context_key(20, "curious")
    for _ in range(8):
        wm.record(ctx, "walk", reflex=True, progressed=False)
    p = wm.predict(ctx, "walk")
    assert p["reflex_p"] > 0.7, p
    print(f"  predict walk@{ctx}: reflex_p={p['reflex_p']:.2f} over n={p['n']}")

    print("  summary:", wm.summary(context=ctx))
    print("\nAll world-model self-test assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
