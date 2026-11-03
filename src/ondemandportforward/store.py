from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS plaintext_code (
    hash    TEXT PRIMARY KEY,
    set_at  TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS processed_emails (
    uid     TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS replay_cache (
    body_hash TEXT PRIMARY KEY,
    seen_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    host       TEXT NOT NULL,
    port       INTEGER NOT NULL,
    ssh_user   TEXT NOT NULL,
    forwards   TEXT NOT NULL,
    ttl        INTEGER NOT NULL,
    status     TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    imap_uid   TEXT,
    sender     TEXT,
    mode       TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    reason     TEXT,
    session_id TEXT
);
CREATE TABLE IF NOT EXISTS session_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON attempts(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id);
"""


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def hash_body(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- processed emails -------------------------------------------------

    def email_seen(self, uid: str) -> bool:
        cur = self._db.execute("SELECT 1 FROM processed_emails WHERE uid = ?", (uid,))
        return cur.fetchone() is not None

    def mark_email_seen(self, uid: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO processed_emails(uid, seen_at) VALUES(?, ?)",
            (uid, _now()),
        )
        self._db.commit()

    # -- replay cache -------------------------------------------------------

    def replay_seen(self, body_hash: str) -> bool:
        cur = self._db.execute("SELECT 1 FROM replay_cache WHERE body_hash = ?", (body_hash,))
        return cur.fetchone() is not None

    def replay_add(self, body_hash: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO replay_cache(body_hash, seen_at) VALUES(?, ?)",
            (body_hash, _now()),
        )
        self._db.commit()

    # -- one-time plaintext code -------------------------------------------

    def armed_code_hash(self) -> str | None:
        """Hash of the currently armed (not yet used) code, if any."""
        cur = self._db.execute("SELECT hash FROM plaintext_code WHERE used_at IS NULL")
        row = cur.fetchone()
        return row["hash"] if row else None

    def arm_code(self, code: str) -> str:
        """Arm a new one-time code; any previously armed (unused) code dies."""
        h = hash_code(code)
        now = _now()
        self._db.execute(
            "UPDATE plaintext_code SET used_at = ? WHERE used_at IS NULL AND hash != ?",
            (now, h),
        )
        self._db.execute(
            "INSERT INTO plaintext_code(hash, set_at) VALUES(?, ?) "
            "ON CONFLICT(hash) DO UPDATE SET set_at = excluded.set_at, used_at = NULL",
            (h, now),
        )
        self._db.commit()
        return h

    def burn_code(self, code_hash: str) -> None:
        self._db.execute(
            "UPDATE plaintext_code SET used_at = ? WHERE hash = ?", (_now(), code_hash)
        )
        self._db.commit()

    def code_used(self, code_hash: str) -> bool:
        cur = self._db.execute(
            "SELECT 1 FROM plaintext_code WHERE hash = ? AND used_at IS NOT NULL",
            (code_hash,),
        )
        return cur.fetchone() is not None

    def clear_codes(self) -> int:
        cur = self._db.execute("DELETE FROM plaintext_code")
        self._db.commit()
        return cur.rowcount

    # -- audit --------------------------------------------------------------

    def log_attempt(
        self,
        *,
        imap_uid: str | None,
        sender: str | None,
        mode: str,
        outcome: str,
        reason: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO attempts(ts, imap_uid, sender, mode, outcome, reason, session_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (_now(), imap_uid, sender, mode, outcome, reason, session_id),
        )
        self._db.commit()

    def recent_attempts(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self._db.execute(
            "SELECT ts, imap_uid, sender, mode, outcome, reason, session_id "
            "FROM attempts ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    # -- sessions -----------------------------------------------------------

    def create_session(
        self,
        *,
        session_id: str,
        host: str,
        port: int,
        ssh_user: str,
        forwards: str,
        ttl: int,
        status: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO sessions(id, host, port, ssh_user, forwards, ttl, status, started_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (session_id, host, port, ssh_user, forwards, ttl, status, _now()),
        )
        self._db.commit()

    def update_session(self, session_id: str, status: str) -> None:
        self._db.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
            (status, _now(), session_id),
        )
        self._db.commit()

    def add_session_event(
        self, session_id: str, event: str, detail: str | None = None
    ) -> None:
        self._db.execute(
            "INSERT INTO session_events(session_id, ts, event, detail) VALUES(?,?,?,?)",
            (session_id, _now(), event, detail),
        )
        self._db.commit()

    def session_events(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        cur = self._db.execute(
            "SELECT ts, event, detail FROM session_events WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        cur = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()

    # -- retention ------------------------------------------------------------

    def prune(self, retention_days: int) -> None:
        if retention_days <= 0:
            return
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        for table in ("attempts", "session_events"):
            self._db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        self._db.commit()
