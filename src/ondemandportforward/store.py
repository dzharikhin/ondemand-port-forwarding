from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

IMAP_FILE = "imap.json"  # mailbox watermark: meta (last_uid) + seen_uids
REPLAY_FILE = "replay.json"  # ciphertext replay cache: {body_hash: seen_at}
CODE_FILE = "code.json"  # one-time password usage state
SESSIONS_FILE = "sessions.jsonl"  # session lifecycle events (append-only)
ATTEMPTS_FILE = "attempts.jsonl"  # audit of every email attempt (append-only)

MAX_SEEN_UIDS = 1000
MAX_REPLAY_ENTRIES = 50_000


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def hash_body(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Plain-file state: one JSON per logical state + append-only JSONL logs.

    All files live in ``data_dir`` and are readable outside the container.
    JSON state files are rewritten atomically (tmp + rename) on every change.
    """

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._imap: dict[str, Any] = self._load_json(IMAP_FILE, {"meta": {}, "seen_uids": {}})
        self._replay: dict[str, str] = self._load_json(REPLAY_FILE, {})
        self._code: dict[str, Any] = self._load_json(CODE_FILE, {"armed": None, "used": []})

    # -- plumbing -------------------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    def _load_json(self, name: str, default: Any) -> Any:
        path = self._path(name)
        if not os.path.exists(path):
            return default
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            backup = f"{path}.corrupt-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            os.replace(path, backup)
            print(
                f"WARNING: corrupt state file {path} ({e}); moved to {backup}",
                file=sys.stderr,
            )
            return default

    def _save_json(self, name: str, data: Any) -> None:
        path = self._path(name)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _append_jsonl(path: str, obj: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _read_jsonl(self, name: str) -> list[dict[str, Any]]:
        path = self._path(name)
        if not os.path.exists(path):
            return []
        rows: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # tolerate a torn trailing line
        return rows

    def _rewrite_jsonl(self, name: str, rows: list[dict[str, Any]]) -> None:
        path = self._path(name)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def close(self) -> None:
        pass  # kept for API compatibility

    # -- mailbox watermark ----------------------------------------------------

    def email_seen(self, uid: str) -> bool:
        return uid in self._imap["seen_uids"]

    def mark_email_seen(self, uid: str) -> None:
        seen = self._imap["seen_uids"]
        seen[uid] = _now()
        while len(seen) > MAX_SEEN_UIDS:
            del seen[next(iter(seen))]  # oldest inserted
        self._save_json(IMAP_FILE, self._imap)

    def get_meta(self, key: str) -> str | None:
        return self._imap["meta"].get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._imap["meta"][key] = value
        self._save_json(IMAP_FILE, self._imap)

    def del_meta(self, key: str) -> None:
        if key in self._imap["meta"]:
            del self._imap["meta"][key]
            self._save_json(IMAP_FILE, self._imap)

    # -- replay cache -----------------------------------------------------------

    def replay_seen(self, body_hash: str) -> bool:
        return body_hash in self._replay

    def replay_add(self, body_hash: str) -> None:
        if body_hash in self._replay:
            return
        self._replay[body_hash] = _now()
        while len(self._replay) > MAX_REPLAY_ENTRIES:
            del self._replay[next(iter(self._replay))]  # oldest inserted
        self._save_json(REPLAY_FILE, self._replay)

    # -- one-time plaintext code ------------------------------------------------

    def armed_code_hash(self) -> str | None:
        armed = self._code["armed"]
        return armed["hash"] if armed else None

    def arm_code(self, code: str) -> str:
        """Arm a new one-time code; any previously armed (unused) code dies."""
        h = hash_code(code)
        armed = self._code["armed"]
        if armed and armed["hash"] != h:
            self._code["used"].append({**armed, "used_at": _now()})
        self._code["used"] = [u for u in self._code["used"] if u["hash"] != h]
        self._code["armed"] = {"hash": h, "set_at": _now()}
        self._save_json(CODE_FILE, self._code)
        return h

    def burn_code(self, code_hash: str) -> None:
        armed = self._code["armed"]
        if armed and armed["hash"] == code_hash:
            self._code["used"].append({**armed, "used_at": _now()})
            self._code["armed"] = None
            self._save_json(CODE_FILE, self._code)

    def code_used(self, code_hash: str) -> bool:
        return any(u["hash"] == code_hash for u in self._code["used"])

    def clear_codes(self) -> int:
        count = (1 if self._code["armed"] else 0) + len(self._code["used"])
        self._code["armed"] = None
        self._code["used"] = []
        self._save_json(CODE_FILE, self._code)
        return count

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
        entry = {
            "ts": _now(),
            "imap_uid": imap_uid,
            "sender": sender,
            "mode": mode,
            "outcome": outcome,
            "reason": reason,
            "session_id": session_id,
        }
        self._append_jsonl(self._path(ATTEMPTS_FILE), entry)

    def recent_attempts(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._read_jsonl(ATTEMPTS_FILE)
        return list(reversed(rows[-limit:]))  # newest first

    # -- sessions (event stream) ---------------------------------------------

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
        self._append_jsonl(
            self._path(SESSIONS_FILE),
            {
                "ts": _now(),
                "event": "created",
                "session_id": session_id,
                "host": host,
                "port": port,
                "ssh_user": ssh_user,
                "forwards": forwards,
                "ttl": ttl,
                "status": status,
            },
        )

    def update_session(self, session_id: str, status: str) -> None:
        self._append_jsonl(
            self._path(SESSIONS_FILE),
            {"ts": _now(), "event": "status", "session_id": session_id, "status": status},
        )

    def add_session_event(
        self, session_id: str, event: str, detail: str | None = None
    ) -> None:
        self._append_jsonl(
            self._path(SESSIONS_FILE),
            {"ts": _now(), "session_id": session_id, "event": event, "detail": detail},
        )

    def session_events(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        rows = [
            {"ts": r.get("ts"), "event": r["event"], "detail": r.get("detail")}
            for r in self._read_jsonl(SESSIONS_FILE)
            if r.get("session_id") == session_id
            and r["event"] not in ("created", "status")  # lifecycle bookkeeping
        ]
        return list(reversed(rows[-limit:]))  # newest first

    # -- retention ------------------------------------------------------------

    def prune(self, retention_days: int) -> None:
        if retention_days <= 0:
            return
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        for name in (ATTEMPTS_FILE, SESSIONS_FILE):
            rows = [r for r in self._read_jsonl(name) if r.get("ts", "") >= cutoff]
            self._rewrite_jsonl(name, rows)
