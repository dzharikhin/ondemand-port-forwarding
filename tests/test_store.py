import json
import os

from ondemandportforward import store as store_module
from ondemandportforward.store import (
    ATTEMPTS_FILE,
    CODE_FILE,
    IMAP_FILE,
    REPLAY_FILE,
    Store,
    hash_code,
)


def test_files_created_under_data_dir(store: Store):
    store.arm_code("x")
    assert os.path.exists(os.path.join(store.data_dir, CODE_FILE))


def test_code_lifecycle(store: Store):
    h = store.arm_code("secret-code")
    assert store.armed_code_hash() == h
    store.burn_code(h)
    assert store.armed_code_hash() is None
    assert store.code_used(h)
    # re-arm same code resurrects it (removed from used history)
    store.arm_code("secret-code")
    assert store.armed_code_hash() == h
    assert not store.code_used(h)


def test_armed_code_supersedes_previous(store: Store):
    h1 = store.arm_code("a")
    h2 = store.arm_code("b")
    assert store.armed_code_hash() == h2
    assert not store.code_used(h2)
    assert store.code_used(h1)  # previous unused code died


def test_code_state_persisted(store: Store, cfg):
    h = store.arm_code("persistent")
    again = Store(cfg.data_dir)  # fresh instance reads the same files
    assert again.armed_code_hash() == h
    again.burn_code(h)
    third = Store(cfg.data_dir)
    assert third.armed_code_hash() is None
    assert third.code_used(h)


def test_clear_codes(store: Store):
    store.arm_code("a")
    store.burn_code(store.armed_code_hash())
    store.arm_code("b")
    n = store.clear_codes()
    assert n == 2  # one burned + one armed
    assert store.armed_code_hash() is None
    assert not store.code_used(hash_code("a"))  # history wiped


def test_replay_cache(store: Store):
    assert not store.replay_seen("x")
    store.replay_add("x")
    assert store.replay_seen("x")
    store.replay_add("x")  # idempotent
    assert store.replay_seen("x")


def test_replay_cache_persisted(store: Store, cfg):
    store.replay_add("x")
    again = Store(cfg.data_dir)
    assert again.replay_seen("x")


def test_email_dedup(store: Store):
    assert not store.email_seen("42")
    store.mark_email_seen("42")
    assert store.email_seen("42")
    store.mark_email_seen("42")  # idempotent
    assert store.email_seen("42")


def test_meta_watermark(store: Store):
    assert store.get_meta("last_uid") is None
    store.set_meta("last_uid", "42")
    assert store.get_meta("last_uid") == "42"
    again = Store(store.data_dir)
    assert again.get_meta("last_uid") == "42"


def test_bounds_seen_uids(store: Store, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_SEEN_UIDS", 20)
    for i in range(30):
        store.mark_email_seen(str(i))
    seen = store._imap["seen_uids"]
    assert len(seen) == 20
    assert "0" not in seen  # oldest dropped
    assert "29" in seen  # newest kept


def test_bounds_replay(store: Store, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_REPLAY_ENTRIES", 50)
    for i in range(60):
        store.replay_add(f"hash-{i}")
    assert len(store._replay) == 50
    assert "hash-0" not in store._replay
    assert "hash-59" in store._replay


def test_attempts_and_events(store: Store):
    store.log_attempt(imap_uid="1", sender="a@b.c", mode="encrypted",
                      outcome="rejected", reason="decrypt_failed")
    store.log_attempt(imap_uid="2", sender="a@b.c", mode="plaintext",
                      outcome="accepted", session_id="s9")
    rows = store.recent_attempts()
    assert len(rows) == 2
    assert rows[0]["imap_uid"] == "2"  # newest first
    assert rows[-1]["reason"] == "decrypt_failed"

    store.create_session(session_id="s1", host="h", port=22, ssh_user="u",
                         forwards="2222:22", ttl=60, status="starting")
    store.add_session_event("s1", "up")
    store.add_session_event("s1", "stopped:ttl")
    store.update_session("s1", "expired")
    events = store.session_events("s1")
    assert [e["event"] for e in reversed(events)] == ["up", "stopped:ttl"]
    assert all("detail" in e for e in events)


def test_jsonl_readable(store: Store):
    store.log_attempt(imap_uid="1", sender="a", mode="m", outcome="o")
    path = os.path.join(store.data_dir, ATTEMPTS_FILE)
    with open(path, encoding="utf-8") as f:
        line = json.loads(f.readline())
    assert line["imap_uid"] == "1"


def test_state_files_readable(store: Store):
    store.mark_email_seen("7")
    store.arm_code("abc")
    with open(os.path.join(store.data_dir, CODE_FILE), encoding="utf-8") as f:
        data = json.load(f)
    assert data["armed"]["hash"] == hash_code("abc")
    with open(os.path.join(store.data_dir, IMAP_FILE), encoding="utf-8") as f:
        imap_state = json.load(f)
    assert "seen_uids" in imap_state


def test_prune(store: Store):
    store.log_attempt(imap_uid="1", sender="a", mode="m", outcome="o")
    store.prune(90)
    assert store.recent_attempts()  # recent rows survive
    store.prune(-1)  # <= 0 means keep everything
    assert store.recent_attempts()
    store.prune(0)


def test_corrupt_state_file_recovers(store: Store, cfg):
    path = os.path.join(cfg.data_dir, REPLAY_FILE)
    with open(path, "w") as f:
        f.write("{not json")
    again = Store(cfg.data_dir)  # must not raise; starts fresh
    assert not again.replay_seen("x")
    backups = [f for f in os.listdir(cfg.data_dir) if ".corrupt-" in f]
    assert backups


def test_hash_code_stable():
    assert hash_code("x") == hash_code("x") and hash_code("x") != hash_code("y")
