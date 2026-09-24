"""SQLite-backed durable mailbox for the c2c hub.

Stores whole envelopes (payload opaque) with an expiry, and tracks per-host
acknowledgement so delivery is at-least-once and never repeats after ack.
"""
from __future__ import annotations

import json
import sqlite3

from c2c import envelope as _env

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
    origin_host  TEXT NOT NULL,
    target_kind  TEXT NOT NULL,
    target_host  TEXT,
    type         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    body         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project);
CREATE TABLE IF NOT EXISTS acks (
    msg_id     TEXT NOT NULL,
    host       TEXT NOT NULL,
    PRIMARY KEY (msg_id, host)
);
"""


class Mailbox:
    def __init__(self, db_path: str, max_per_project: int = 1000) -> None:
        self._max = max_per_project
        self._db = sqlite3.connect(db_path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def put(self, env: dict) -> None:
        _env.validate(env)
        t = env["target"]
        self._db.execute(
            "INSERT OR IGNORE INTO messages "
            "(msg_id, project, origin_host, target_kind, target_host, "
            " type, created_at, expires_at, body) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                env["msg_id"], env["project"], env["origin_host"],
                t["kind"], t.get("host"), env["type"],
                env["created_at"], _env.expires_at(env),
                json.dumps(env, separators=(",", ":")),
            ),
        )
        self._evict_over_cap(env["project"])
        self._db.commit()

    def _evict_over_cap(self, project: str) -> None:
        victims = self._db.execute(
            "SELECT msg_id FROM messages WHERE project = ? "
            "ORDER BY created_at DESC, msg_id DESC LIMIT -1 OFFSET ?",
            (project, self._max),
        ).fetchall()
        for row in victims:
            self._db.execute("DELETE FROM messages WHERE msg_id = ?", (row["msg_id"],))
            self._db.execute("DELETE FROM acks WHERE msg_id = ?", (row["msg_id"],))

    def sweep_expired(self, now_ms: int) -> int:
        rows = self._db.execute(
            "SELECT msg_id FROM messages WHERE expires_at <= ?", (now_ms,)
        ).fetchall()
        for row in rows:
            self._db.execute("DELETE FROM messages WHERE msg_id = ?", (row["msg_id"],))
            self._db.execute("DELETE FROM acks WHERE msg_id = ?", (row["msg_id"],))
        self._db.commit()
        return len(rows)

    def pending_for(self, host: str, projects: set[str], now_ms: int) -> list[dict]:
        rows = self._db.execute(
            "SELECT m.body FROM messages m "
            "WHERE m.expires_at > ? "
            "  AND NOT EXISTS (SELECT 1 FROM acks a "
            "                  WHERE a.msg_id = m.msg_id AND a.host = ?) "
            "  AND ( (m.target_kind = 'host' AND m.target_host = ?) "
            "     OR (m.target_kind = 'project' AND m.origin_host != ?) ) "
            "ORDER BY m.created_at ASC, m.msg_id ASC",
            (now_ms, host, host, host),
        ).fetchall()
        out = []
        for r in rows:
            env = json.loads(r["body"])
            if env["target"]["kind"] == "project" and env["project"] not in projects:
                continue
            out.append(env)
        return out

    def ack(self, msg_id: str, host: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO acks (msg_id, host) VALUES (?, ?)",
            (msg_id, host),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
