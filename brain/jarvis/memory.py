"""Durable memory: facts about the user, conversation log, scheduled items.

SQLite, because it survives restarts, is a single file, and needs no server.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    source TEXT DEFAULT 'user',
    created REAL NOT NULL,
    used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    fire_at REAL NOT NULL,
    text TEXT NOT NULL,
    label TEXT DEFAULT '',
    created REAL NOT NULL,
    done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_schedule_pending ON schedule(done, fire_at);
"""

_WORD = re.compile(r"[a-z0-9']+")


def _tokens(s: str) -> set[str]:
    return set(_WORD.findall((s or "").lower()))


@dataclass
class Fact:
    id: int
    text: str
    source: str
    created: float


@dataclass
class ScheduledItem:
    id: int
    kind: str  # "timer" | "reminder"
    fire_at: float
    text: str
    label: str


class Memory:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    # ---------------------------------------------------------------- facts
    def add_fact(self, text: str, source: str = "user") -> int:
        text = (text or "").strip()
        if not text:
            return 0
        with self._lock:
            # Replace a near-duplicate rather than accumulating restatements.
            for row in self.db.execute("SELECT id, text FROM facts"):
                a, b = _tokens(text), _tokens(row["text"])
                if a and b and len(a & b) / len(a | b) > 0.75:
                    self.db.execute("UPDATE facts SET text=?, created=? WHERE id=?", (text, time.time(), row["id"]))
                    self.db.commit()
                    return int(row["id"])
            cur = self.db.execute(
                "INSERT INTO facts (text, source, created) VALUES (?,?,?)", (text, source, time.time())
            )
            self.db.commit()
            return int(cur.lastrowid)

    def list_facts(self, limit: int = 200) -> list[Fact]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM facts ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Fact(r["id"], r["text"], r["source"], r["created"]) for r in rows]

    def search_facts(self, query: str, limit: int = 8) -> list[Fact]:
        q = _tokens(query)
        scored = []
        for f in self.list_facts(500):
            t = _tokens(f.text)
            if not t:
                continue
            overlap = len(q & t)
            if overlap:
                scored.append((overlap / len(q | t), f))
        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:limit]]

    def forget(self, query: str) -> list[str]:
        hits = self.search_facts(query, limit=3)
        removed = []
        with self._lock:
            for f in hits:
                self.db.execute("DELETE FROM facts WHERE id=?", (f.id,))
                removed.append(f.text)
            self.db.commit()
        return removed

    # --------------------------------------------------------- conversation
    def log_turn(self, role: str, text: str, meta: dict | None = None) -> None:
        if self._closed:
            return
        with self._lock:
            self.db.execute(
                "INSERT INTO turns (ts, role, text, meta) VALUES (?,?,?,?)",
                (time.time(), role, text, json.dumps(meta) if meta else None),
            )
            self.db.commit()

    def recent_turns(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT ts, role, text FROM turns ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ------------------------------------------------------------- schedule
    def add_scheduled(self, kind: str, fire_at: float, text: str, label: str = "") -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO schedule (kind, fire_at, text, label, created) VALUES (?,?,?,?,?)",
                (kind, fire_at, text, label, time.time()),
            )
            self.db.commit()
            return int(cur.lastrowid)

    def pending_scheduled(self) -> list[ScheduledItem]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM schedule WHERE done=0 ORDER BY fire_at"
            ).fetchall()
        return [ScheduledItem(r["id"], r["kind"], r["fire_at"], r["text"], r["label"]) for r in rows]

    def complete_scheduled(self, item_id: int) -> None:
        with self._lock:
            self.db.execute("UPDATE schedule SET done=1 WHERE id=?", (item_id,))
            self.db.commit()

    def cancel_scheduled(self, item_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM schedule WHERE id=? AND done=0", (item_id,))
            self.db.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ kv
    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_kv(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.db.close()

    @property
    def closed(self) -> bool:
        return self._closed
