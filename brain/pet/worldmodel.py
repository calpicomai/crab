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

* **preferences** — things you *taught* it off-body (TUI / ``teach()``): drive
  (chase/approach/avoid/neutral), weight, valence (-1..1), aliases, notes. Persist
  in SQLite and override the static ``PET_CHASE_LABELS`` config on the Jetson.
* **concepts** (laptop LLM training) — semantic knowledge with rich **keyword**
  lists so detector labels like ``tabby cat`` match a taught ``cat`` concept at
  runtime **without** an LLM on the Jetson. See ``brain/pet/world_train.py``.

Config is read lazily (only when an arg is omitted) so the self-test runs anywhere:
    python -m brain.pet.worldmodel
    python -m brain.pet.world_tui          # laptop TUI
    python -m brain.pet.world_train        # laptop training sessions → deploy world.db
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass

# Detection sources whose boxes are fabricated (never fuse into world knowledge) —
# same rule the costmap uses (brain/costmap.py:integrate_camera).
_FAKE_SOURCES = {"dummy"}
DRIVES = ("chase", "approach", "avoid", "neutral")


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
        self._migrate_db()

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
        c.execute(
            """CREATE TABLE IF NOT EXISTS preferences(
                 label TEXT PRIMARY KEY,
                 drive TEXT NOT NULL DEFAULT 'approach',
                 weight REAL NOT NULL DEFAULT 0.6,
                 valence REAL NOT NULL DEFAULT 0,
                 aliases TEXT DEFAULT '',
                 note TEXT DEFAULT '',
                 taught_ts REAL)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS concepts(
                 canonical TEXT PRIMARY KEY,
                 category TEXT DEFAULT 'thing',
                 description TEXT DEFAULT '',
                 drive TEXT NOT NULL DEFAULT 'approach',
                 weight REAL NOT NULL DEFAULT 0.6,
                 valence REAL NOT NULL DEFAULT 0,
                 keywords TEXT DEFAULT '[]',
                 aliases TEXT DEFAULT '',
                 note TEXT DEFAULT '',
                 session TEXT DEFAULT '',
                 updated_ts REAL,
                 embedding TEXT DEFAULT '[]')"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS training_queue(
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 session TEXT NOT NULL,
                 kind TEXT NOT NULL,
                 payload TEXT NOT NULL,
                 note TEXT DEFAULT '',
                 created_ts REAL,
                 processed INTEGER DEFAULT 0)"""
        )
        c.commit()

    def _migrate_db(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(concepts)")}
        if "embedding" not in cols:
            self._conn.execute("ALTER TABLE concepts ADD COLUMN embedding TEXT DEFAULT '[]'")
            self._conn.commit()
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
    # Taught preferences (off-body learning — overrides static config)
    # ------------------------------------------------------------------ #
    def _preference_for(self, label: str) -> sqlite3.Row | None:
        """Match a label to a taught preference (exact label or alias overlap)."""
        lab = self._norm(label)
        row = self._conn.execute("SELECT * FROM preferences WHERE label=?", (lab,)).fetchone()
        if row is not None:
            return row
        for row in self._conn.execute("SELECT * FROM preferences"):
            for alias in (row["aliases"] or "").split(","):
                a = alias.strip().lower()
                if a and (a in lab or lab in a):
                    return row
        return None

    def _concept_for(self, label: str) -> sqlite3.Row | None:
        """Match a detector label to an LLM-trained concept (keywords + embeddings)."""
        from . import world_semantic

        lab = self._norm(label)
        for row in self._conn.execute("SELECT * FROM concepts"):
            if lab == row["canonical"]:
                return row
        best: sqlite3.Row | None = None
        best_hits = 0
        for row in self._conn.execute("SELECT * FROM concepts"):
            try:
                keywords = json.loads(row["keywords"] or "[]")
            except json.JSONDecodeError:
                keywords = []
            if world_semantic.label_matches_concept(lab, keywords):
                hits = sum(1 for k in keywords if k in lab or lab in k)
                if hits > best_hits:
                    best, best_hits = row, hits
        if best is not None:
            return best
        try:
            from . import world_embeddings

            if not world_embeddings.WORLD_EMBED_AT_RUNTIME:
                return None
            loaded: list[tuple[sqlite3.Row, list[float]]] = []
            for row in self._conn.execute("SELECT * FROM concepts"):
                vec = world_embeddings.parse_embedding(row["embedding"] if "embedding" in row.keys() else None)
                if vec:
                    loaded.append((row, vec))
            if loaded:
                emb = world_embeddings.best_embedding_match(lab, loaded)
                if emb is not None:
                    return emb
        except Exception:
            pass
        return None

    def _learned_for(self, label: str) -> sqlite3.Row | None:
        """Concept (LLM) beats manual preference for the same label."""
        return self._concept_for(label) or self._preference_for(label)

    def _drive_for(self, label: str) -> str:
        learned = self._learned_for(label)
        if learned is not None:
            return str(learned["drive"])
        if self._matches(label, self.chase_labels):
            return "chase"
        if self._matches(label, self.interest_labels):
            return "approach"
        return "neutral"

    def _valence_for(self, label: str) -> float:
        learned = self._learned_for(label)
        if learned is not None:
            return float(learned["valence"])
        row = self._conn.execute("SELECT valence FROM objects WHERE label=?", (self._norm(label),)).fetchone()
        return float(row["valence"]) if row else 0.0

    def _weight(self, label: str) -> float:
        learned = self._learned_for(label)
        if learned is not None:
            return float(learned["weight"])
        if self._matches(label, self.chase_labels):
            return 1.0
        if self._matches(label, self.interest_labels):
            return 0.6
        return 0.12

    def teach(
        self,
        label: str,
        *,
        drive: str = "approach",
        weight: float | None = None,
        valence: float = 0.0,
        aliases: list[str] | None = None,
        note: str = "",
    ) -> None:
        """Teach the pet about something without the robot body or a camera."""
        lab = self._norm(label)
        drive = drive.strip().lower()
        if drive not in DRIVES:
            raise ValueError(f"drive must be one of {DRIVES}, got {drive!r}")
        if weight is None:
            weight = {"chase": 1.0, "approach": 0.6, "avoid": 0.0, "neutral": 0.12}[drive]
        weight = max(0.0, min(1.0, float(weight)))
        valence = max(-1.0, min(1.0, float(valence)))
        alias_s = ",".join(sorted({self._norm(a) for a in (aliases or []) if a.strip()}))
        now = time.time()
        self._conn.execute(
            """INSERT INTO preferences(label,drive,weight,valence,aliases,note,taught_ts)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(label) DO UPDATE SET
                 drive=excluded.drive, weight=excluded.weight, valence=excluded.valence,
                 aliases=excluded.aliases, note=excluded.note, taught_ts=excluded.taught_ts""",
            (lab, drive, weight, valence, alias_s, note.strip(), now),
        )
        self._conn.execute(
            "INSERT INTO objects(label,times_seen,first_ts,last_ts,valence) VALUES(?,0,?,?,?) "
            "ON CONFLICT(label) DO UPDATE SET valence=excluded.valence",
            (lab, now, now, valence),
        )
        self._conn.commit()

    def forget(self, label: str) -> bool:
        """Remove a taught preference. Returns True if one existed."""
        lab = self._norm(label)
        cur = self._conn.execute("DELETE FROM preferences WHERE label=?", (lab,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_preferences(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT label,drive,weight,valence,aliases,note,taught_ts FROM preferences ORDER BY label"
        ).fetchall()
        return [dict(r) for r in rows]

    def simulate_see(
        self,
        label: str,
        *,
        bearing_deg: float = 0.0,
        range_cm: float | None = None,
    ) -> None:
        """Record a synthetic sighting — learn objects/places without a camera."""
        width, height = 640, 480
        box = _box_for_bearing(bearing_deg, self.camera_hfov, width, height)
        if range_cm is not None:
            dim = max(0.05, min(0.95, 1.0 - range_cm / self.max_range))
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            half = int(min(width, height) * dim * 0.25)
            box = [int(cx - half), int(cy - half), int(cx + half), int(cy + half)]
        snap = _snap(width, height, [(label, box, "taught")])
        dist = range_cm if range_cm is not None else 90.0
        self.observe(snap, {"distance_cm": dist})

    def teach_place(self, labels: list[str]) -> None:
        """Register a place fingerprint without being there."""
        clean = sorted({self._norm(l) for l in labels if l.strip()})
        if not clean:
            return
        self.visible = clean
        self._update_place(clean, time.time())
        self._conn.commit()

    def list_objects(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            """SELECT label,times_seen,last_bearing,last_range_cm,valence,last_ts
               FROM objects ORDER BY times_seen DESC, label LIMIT ?""",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["drive"] = self._drive_for(r["label"])
            d["interest"] = round(self.interest(r["label"]), 3)
            pref = self._preference_for(r["label"])
            d["taught"] = pref is not None and pref["label"] == self._norm(r["label"])
            out.append(d)
        return out

    def list_places(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id,labels,visits,last_ts FROM places ORDER BY visits DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_outcomes(self, limit: int = 40) -> list[dict]:
        rows = self._conn.execute(
            """SELECT context,action,tries,reflex,progressed FROM outcomes
               ORDER BY tries DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            pred = self.predict(r["context"], r["action"])
            d.update(pred)
            out.append(d)
        return out

    # ------------------------------------------------------------------ #
    # LLM semantic concepts + training sessions (laptop)
    # ------------------------------------------------------------------ #
    def upsert_concept(self, spec: dict, *, session: str = "", embed_simulate: bool = False) -> None:
        """Store an LLM-extracted concept and mirror it into preferences for runtime."""
        from . import world_embeddings

        canonical = self._norm(spec["canonical"])
        keywords = spec.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]
        kw_json = json.dumps([self._norm(k) for k in keywords if k])
        aliases = spec.get("aliases") or []
        alias_s = ",".join(sorted({self._norm(a) for a in aliases if a}))
        now = time.time()
        try:
            vec = world_embeddings.embed_text(
                world_embeddings.concept_embed_text(spec), simulate=embed_simulate)
            emb_json = world_embeddings.serialize_embedding(vec)
        except Exception:
            emb_json = "[]"
        self._conn.execute(
            """INSERT INTO concepts(canonical,category,description,drive,weight,valence,
                                    keywords,aliases,note,session,updated_ts,embedding)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(canonical) DO UPDATE SET
                 category=excluded.category, description=excluded.description,
                 drive=excluded.drive, weight=excluded.weight, valence=excluded.valence,
                 keywords=excluded.keywords, aliases=excluded.aliases, note=excluded.note,
                 session=excluded.session, updated_ts=excluded.updated_ts,
                 embedding=excluded.embedding""",
            (canonical, spec.get("category", "thing"), spec.get("description", ""),
             spec["drive"], float(spec["weight"]), float(spec["valence"]),
             kw_json, alias_s, spec.get("note", ""), session, now, emb_json),
        )
        all_aliases = list(dict.fromkeys([self._norm(k) for k in keywords if k] + [self._norm(a) for a in aliases if a]))
        self.teach(
            canonical,
            drive=spec["drive"],
            weight=float(spec["weight"]),
            valence=float(spec["valence"]),
            aliases=[a for a in all_aliases if a != canonical],
            note=spec.get("description") or spec.get("note", ""),
        )
        self._conn.commit()

    def list_concepts(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT canonical,category,description,drive,weight,valence,keywords,note,session "
            "FROM concepts ORDER BY canonical"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["keywords"] = json.loads(d["keywords"] or "[]")
            except json.JSONDecodeError:
                d["keywords"] = []
            out.append(d)
        return out

    def queue_training(self, session: str, kind: str, payload: str, note: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO training_queue(session,kind,payload,note,created_ts,processed) VALUES(?,?,?,?,?,0)",
            (session.strip(), kind.strip(), payload.strip(), note.strip(), time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def pending_training(self, session: str | None = None) -> list[dict]:
        if session:
            rows = self._conn.execute(
                "SELECT id,session,kind,payload,note,created_ts FROM training_queue "
                "WHERE processed=0 AND session=? ORDER BY id",
                (session.strip(),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id,session,kind,payload,note,created_ts FROM training_queue "
                "WHERE processed=0 ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def consolidate_training(self, session: str | None = None, *, simulate: bool = False) -> int:
        """Process queued training items with the laptop LLM → concepts table."""
        from . import world_semantic

        pending = self.pending_training(session)
        done = 0
        for item in pending:
            kind, payload, note = item["kind"], item["payload"], item.get("note") or ""
            try:
                if kind == "text":
                    spec = world_semantic.analyze_text(payload + ("\n" + note if note else ""), simulate=simulate)
                elif kind == "image":
                    spec = world_semantic.analyze_image(payload, note, simulate=simulate)
                elif kind == "jsonl":
                    spec = world_semantic.analyze_jsonl_line(payload, simulate=simulate)
                else:
                    spec = world_semantic.analyze_text(f"{kind}: {payload} {note}", simulate=simulate)
                if spec:
                    self.upsert_concept(spec, session=item["session"], embed_simulate=simulate)
            except Exception:
                continue
            self._conn.execute("UPDATE training_queue SET processed=1 WHERE id=?", (item["id"],))
            done += 1
        self._conn.commit()
        return done

    def queue_log_file(self, path: str, session: str | None = None) -> int:
        """Queue every line of a pet/wander --log JSONL file for LLM consolidation."""
        from pathlib import Path

        from . import config as pet_config

        p = Path(path)
        if not p.is_file():
            return 0
        sess = (session or pet_config.PET_WORLD_TRAIN_SESSION or p.stem).strip() or "default"
        n = 0
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self.queue_training(sess, "jsonl", line)
                    n += 1
        return n

    def semantic_summary(self, limit: int = 6) -> str:
        """Rich text for the VLM — concept descriptions the pet has learned."""
        concepts = self.list_concepts()[:limit]
        if not concepts:
            return ""
        lines = []
        for c in concepts:
            kw = ", ".join(c["keywords"][:6]) if c.get("keywords") else c["canonical"]
            lines.append(
                f"- {c['canonical']} ({c['drive']}, {c['category']}): {c.get('description') or c.get('note','')} "
                f"[also: {kw}]"
            )
        extra = self.concept_count() - len(concepts)
        if extra > 0:
            lines.append(f"- …and {extra} more learned concepts")
        return "Learned world knowledge:\n" + "\n".join(lines)

    def concept_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0])

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
            val = self._valence_for(label)
            row = self._conn.execute("SELECT times_seen FROM objects WHERE label=?", (label,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO objects(label,times_seen,first_ts,last_ts,last_bearing,last_range_cm,valence) "
                    "VALUES(?,?,?,?,?,?,?)", (label, 1, now, now, bearing, rng, val))
            else:
                self._conn.execute(
                    "UPDATE objects SET times_seen=times_seen+1, last_ts=?, last_bearing=?, "
                    "last_range_cm=?, valence=? WHERE label=?",
                    (now, bearing, rng, val, label))
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

    def interest(self, label: str) -> float:
        """0..1 how much the pet wants to go to this. Taught chase stays exciting;
        approach fades with familiarity; avoid/neutral score near zero."""
        drive = self._drive_for(label)
        if drive == "avoid":
            return 0.0
        w = self._weight(label)
        valence = self._valence_for(label)
        valence_boost = 1.0 + 0.25 * valence
        if drive == "chase":
            return max(0.0, min(1.0, w * valence_boost))
        novelty = 1.0 / (1.0 + 0.5 * self._times_seen(label))
        return max(0.0, min(1.0, w * (0.3 + 0.7 * novelty) * valence_boost))

    def salient_target(self, snapshot: dict, min_interest: float = 0.35) -> Target | None:
        """The most interesting thing in view worth going to, or None."""
        width = snapshot.get("width") or 0
        height = snapshot.get("height") or 0
        best: Target | None = None
        for det in self._real_detections(snapshot):
            label = self._norm(det["label"])
            drive = self._drive_for(label)
            if drive == "avoid":
                continue
            score = self.interest(label)
            if score < min_interest:
                continue
            if best is None or score > best.interest:
                best = Target(
                    label=label,
                    bearing_deg=self._bearing(det["box"], width) if width else 0.0,
                    range_cm=self._range(det["box"], width, height) if width and height else self.max_range,
                    drive="chase" if drive == "chase" else "approach",
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
        prefs = self.list_preferences()
        if prefs:
            taught = ", ".join(f"{p['label']}({p['drive']})" for p in prefs[:4])
            bits.append(f"taught: {taught}")
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


def _box_for_bearing(
    bearing_deg: float,
    hfov: float,
    width: int = 640,
    height: int = 480,
) -> list[int]:
    cx = width * (0.5 + bearing_deg / hfov)
    bw = width * 0.14
    return [int(cx - bw / 2), int(height * 0.2), int(cx + bw / 2), int(height * 0.75)]


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

    # Off-body teaching: snake is scary (avoid), garter snake matches via alias.
    wm.teach("snake", drive="avoid", valence=-0.8, aliases=["garter snake"], note="scary")
    wm.simulate_see("garter snake", bearing_deg=10.0)
    assert wm._drive_for("garter snake") == "avoid"
    assert wm.interest("garter snake") == 0.0
    assert wm.salient_target(_snap(640, 480, [("garter snake", _box_for_bearing(10, hfov), "taught")])) is None

    wm.teach("snake", drive="chase", valence=0.5, aliases=["python"])
    assert wm.interest("python") > 0.9
    print("  teach: snake chase + alias python")

    # LLM-style concept: keyword generalization at runtime (no LLM on Jetson).
    wm.upsert_concept({
        "canonical": "cat", "category": "animal", "drive": "chase", "valence": 0.7, "weight": 1.0,
        "keywords": ["cat", "tabby", "kitten", "feline", "a cat"],
        "description": "House cat to greet and chase.",
    }, session="test", embed_simulate=True)
    assert wm._drive_for("tabby cat") == "chase"
    assert wm.interest("a fluffy tabby") > 0.5
    t2 = wm.salient_target(_snap(640, 480, [("tabby cat", _box_for_bearing(5, hfov), "yolo")]))
    assert t2 is not None and t2.drive == "chase", t2
    print("  concept: tabby cat → chase via keywords")

    # Embeddings stored for runtime similarity (simulate = deterministic test vectors).
    from . import world_embeddings as we

    spec = {
        "canonical": "roomba", "category": "appliance", "drive": "avoid", "valence": -0.9,
        "weight": 0.0, "keywords": ["roomba"],
        "description": "Robot vacuum cleaner loud scary",
    }
    wm.upsert_concept(spec, session="test", embed_simulate=True)
    row = wm._conn.execute("SELECT embedding FROM concepts WHERE canonical='roomba'").fetchone()
    vec = we.parse_embedding(row["embedding"])
    assert vec and len(vec) > 8
    q = we.embed_text(we.concept_embed_text(spec), simulate=True)
    assert we.cosine(q, vec) > 0.99
    print("  embedding: stored + cosine self-match")

    print("  summary:", wm.summary(context=ctx))
    print("\nAll world-model self-test assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
