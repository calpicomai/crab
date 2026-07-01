"""Episodic memory for the robot pet — a local, on-device SQLite log of what it
saw, how it felt, and what it did, persisted across runs.

This is the concrete first piece of the roadmap's learning stack (episodic
memory, 5a) and the pet's raw material: recall drives recognition ("I've seen you
before") and preferences, and the periodic character re-summary (see
brain/pet/identity.py) reads from it. Deliberately simple — recent + keyword
recall, no embeddings yet. No cloud; everything stays in one SQLite file.
"""

from __future__ import annotations

import sqlite3
import time


class MemoryStore:
    """A tiny episodic memory. Open on a file path (persists) or ``:memory:``."""

    def __init__(self, path: str = ":memory:") -> None:
        # check_same_thread=False: the pet runs a body thread + a mind thread and
        # only the mind thread touches memory, but this keeps it safe if that
        # changes. Writes are serialized by SQLite's own lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS episodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL,
                mood        TEXT,
                pose        TEXT,
                distance_cm REAL,
                action      TEXT,
                observation TEXT,   -- what it saw/noticed (labels or a phrase)
                note        TEXT    -- its own remark / feeling about it
            )
            """
        )
        self._conn.commit()

    def remember(
        self,
        *,
        mood: str | None = None,
        pose: str | None = None,
        distance_cm: float | None = None,
        action: str | None = None,
        observation: str | None = None,
        note: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO episodes (ts, mood, pose, distance_cm, action, observation, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), mood, pose, distance_cm, action, observation, note),
        )
        self._conn.commit()

    def recent(self, n: int = 8) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (int(n),))
        return [dict(r) for r in reversed(cur.fetchall())]

    def recall(self, keywords: str, n: int = 5) -> list[dict]:
        """Loose keyword recall over what it saw/said (case-insensitive LIKE)."""
        like = f"%{keywords.strip()}%"
        cur = self._conn.execute(
            "SELECT * FROM episodes WHERE observation LIKE ? OR note LIKE ? ORDER BY id DESC LIMIT ?",
            (like, like, int(n)),
        )
        return [dict(r) for r in cur.fetchall()]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def summary(self, n: int = 8) -> str:
        """Compact text block of recent life, for the pet's prompt."""
        rows = self.recent(n)
        if not rows:
            return "(no memories yet — everything is new)"
        lines = []
        for r in rows:
            bits = []
            if r["observation"]:
                bits.append(f"saw {r['observation']}")
            if r["action"]:
                bits.append(r["action"])
            if r["note"]:
                bits.append(f'"{r["note"]}"')
            mood = f"[{r['mood']}] " if r["mood"] else ""
            if bits:
                lines.append(f"- {mood}{'; '.join(bits)}")
        return "\n".join(lines) if lines else "(quiet so far)"

    def close(self) -> None:
        self._conn.close()
