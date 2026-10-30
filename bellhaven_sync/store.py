"""SQLite review queue. The fingerprint is the primary key, which is what makes
re-runs safe: a proposal that was already approved or rejected is never
queued again, and a pending one is refreshed in place rather than duplicated.

Statuses: pending -> approved -> applied | failed
          pending -> rejected
          pending -> superseded   (a later run no longer produces it)
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, kind TEXT, title TEXT, account_id TEXT, slug TEXT,
  body TEXT, status TEXT, first_seen TEXT, last_seen TEXT,
  decided_at TEXT, decided_by TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, summary TEXT
);
"""

DECIDED = ("approved", "rejected", "applied", "failed")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def enqueue(db: sqlite3.Connection, proposals) -> dict:
    """Insert new proposals, refresh pending ones, skip decided ones."""
    ts, counts, seen = now(), {"new": 0, "still_pending": 0, "already_decided": 0, "superseded": 0}, set()
    for p in proposals:
        pid = p.fingerprint
        seen.add(pid)
        body = json.dumps({"changes": p.changes, "new_account": p.new_account, "note": p.note,
                           "evidence": p.evidence, "snapshot": p.snapshot})
        row = db.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()
        if row is None:
            db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, p.kind, p.title, p.account_id, p.slug, body, "pending", ts, ts, None, None, None))
            counts["new"] += 1
        elif row["status"] in DECIDED:
            counts["already_decided"] += 1
        else:
            db.execute("UPDATE proposals SET body=?, title=?, status='pending', last_seen=? WHERE id=?",
                       (body, p.title, ts, pid))
            counts["still_pending"] += 1
    for row in db.execute("SELECT id FROM proposals WHERE status='pending'").fetchall():
        if row["id"] not in seen:
            db.execute("UPDATE proposals SET status='superseded', last_seen=? WHERE id=?", (ts, row["id"]))
            counts["superseded"] += 1
    db.commit()
    return counts


def get(db: sqlite3.Connection, pid: str) -> dict | None:
    row = db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return _hydrate(row) if row else None


def list_by_status(db: sqlite3.Connection, *statuses: str) -> list[dict]:
    q = f"SELECT * FROM proposals WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY kind, title"
    return [_hydrate(r) for r in db.execute(q, statuses).fetchall()]


def decide(db: sqlite3.Connection, pid: str, status: str, by: str, result: dict | None = None):
    db.execute("UPDATE proposals SET status=?, decided_at=?, decided_by=?, result=? WHERE id=?",
               (status, now(), by, json.dumps(result) if result is not None else None, pid))
    db.commit()


def record_run(db: sqlite3.Connection, started: str, summary: dict):
    db.execute("INSERT INTO runs (started, finished, summary) VALUES (?,?,?)", (started, now(), json.dumps(summary)))
    db.commit()


def last_run(db: sqlite3.Connection) -> dict | None:
    row = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return {**dict(row), "summary": json.loads(row["summary"])} if row else None


def _hydrate(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.update(json.loads(d.pop("body")))
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d
