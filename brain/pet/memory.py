"""Local episodic memory with explicit evidence kinds and recoverable migration.

A generated description never becomes an observed fact merely by being stored.
Legacy fields remain exportable, but their ambiguous evidence is not upgraded.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
import time

SCHEMA_VERSION = 2
OBSERVATION_SOURCES = frozenset({
    "dashboard", "microphone", "user", "operator", "user_transcript", "sensor",
    "detector", "simulation_detector", "runtime", "coordinator",
})
INTERACTION_SOURCES = frozenset({"dashboard", "microphone", "user", "operator", "user_transcript"})


class MemoryStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.migration_backup: str | None = None
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).absolute().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        try:
            self._conn.row_factory = sqlite3.Row
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError(f"unsupported memory schema {version}; original database preserved")
            exists = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='episodes'"
            ).fetchone()
            if exists and version < SCHEMA_VERSION:
                self.migration_backup = self.backup("pre-v2")
            with self._conn:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS episodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mood TEXT,
                        pose TEXT, distance_cm REAL, action TEXT, observation TEXT, note TEXT
                    )
                """)
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(episodes)")}
                additions = {
                    "observed_json": "TEXT", "inferred_context": "TEXT", "narrative": "TEXT",
                    "source": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
                }
                for name, declaration in additions.items():
                    if name not in columns:
                        self._conn.execute(f"ALTER TABLE episodes ADD COLUMN {name} {declaration}")
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            self._conn.close()
            raise

    def backup(self, label: str = "backup") -> str | None:
        if self.path == ":memory:":
            return None
        # SQLite backup includes committed pages from a WAL safely.
        target = f"{self.path}.{label}-{time.time_ns()}.bak"
        with self._lock:
            with sqlite3.connect(target) as destination:
                self._conn.backup(destination)
        return target

    def remember(self, *, mood: str | None = None, pose: str | None = None,
                 distance_cm: float | None = None, action: str | None = None,
                 observation: str | None = None, note: str | None = None,
                 observed_facts: dict | None = None, inferred_context: str | None = None,
                 narrative: str | None = None, source: str = "legacy_unverified") -> None:
        if observed_facts is not None and (not isinstance(observed_facts, dict) or source not in OBSERVATION_SOURCES):
            raise ValueError("observed_facts require an explicit non-model capture source")
        encoded = json.dumps(observed_facts, ensure_ascii=False, allow_nan=False) if observed_facts is not None else None
        if encoded is not None and len(encoded) > 8192:
            raise ValueError("observed facts exceed one bounded memory record")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO episodes (ts,mood,pose,distance_cm,action,observation,note,"
                "observed_json,inferred_context,narrative,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), mood, pose, distance_cm, action, observation, note, encoded,
                 inferred_context, narrative, source),
            )

    @staticmethod
    def _decode(row) -> dict:
        result = dict(row)
        encoded = result.pop("observed_json")
        result["observed_facts"] = json.loads(encoded) if encoded else None
        return result

    def recent(self, n: int = 8) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM episodes ORDER BY id DESC LIMIT ?",
                                      (max(0, min(int(n), 1000)),)).fetchall()
        return [self._decode(row) for row in reversed(rows)]

    def recall(self, keywords: str, n: int = 5) -> list[dict]:
        # Literal text search: SQL wildcard characters in user speech are data.
        like = "%" + keywords.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM episodes WHERE observation LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\' "
                "OR observed_json LIKE ? ESCAPE '\\' OR inferred_context LIKE ? ESCAPE '\\' "
                "OR narrative LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (like, like, like, like, like, max(0, min(int(n), 1000))),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def interaction_history(self) -> dict:
        """Grounded continuity across sessions, without recognizing individuals.

        A microphone transcript is recorded as captured text, not a guaranteed
        verbatim human statement. Model prose and legacy observations never
        contribute to this history.
        """
        from .commands import interpret

        history = {"engagements": 0, "greetings": 0, "play_requests": 0, "last_interaction_at": None}
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, observed_json, source FROM episodes WHERE observed_json IS NOT NULL ORDER BY id"
            ).fetchall()
        for row in rows:
            if row["source"] not in INTERACTION_SOURCES:
                continue
            facts = json.loads(row["observed_json"])
            utterance = facts.get("utterance", facts.get("transcript"))
            if not isinstance(utterance, str) or not utterance.strip():
                continue
            command = interpret(utterance)
            history["engagements"] += 1
            history["greetings"] += int(command is not None and command.name == "greet")
            history["play_requests"] += int(command is not None and command.name == "play")
            history["last_interaction_at"] = row["ts"]
        return history

    def summary(self, n: int = 8) -> str:
        lines = []
        for row in self.recent(n):
            parts = []
            if row["observed_facts"]:
                parts.append(f"captured [{row['source']}]: {json.dumps(row['observed_facts'], ensure_ascii=False)}")
            if row["inferred_context"]:
                parts.append(f"inference (unverified): {row['inferred_context']}")
            if row["narrative"]:
                parts.append(f"generated narrative: {row['narrative']}")
            if row["observation"]:
                parts.append(f"legacy/unverified description: {row['observation']}")
            if row["note"]:
                parts.append(f"legacy narrative: {row['note']}")
            if row["action"]:
                parts.append(f"recorded action (outcome not implied): {row['action']}")
            if parts:
                lines.append("- " + "; ".join(parts))
        return "\n".join(lines) or "(no recorded interactions)"

    def export(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM episodes ORDER BY id").fetchall()
        return {"schema_version": SCHEMA_VERSION, "exported_at": time.time(),
                "episodes": [self._decode(row) for row in rows]}

    def reset(self) -> str | None:
        """Explicit caller operation; always makes a recoverable file backup."""
        with self._lock:
            backup = self.backup("pre-reset")
            with self._conn:
                self._conn.execute("DELETE FROM episodes")
        return backup

    def close(self) -> None:
        with self._lock:
            self._conn.close()
