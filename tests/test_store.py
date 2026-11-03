
from ondemandportforward.store import Store, hash_code


def test_code_lifecycle(store: Store):
    h = store.arm_code("secret-code")
    assert store.armed_code_hash() == h
    store.burn_code(h)
    assert store.armed_code_hash() is None
    assert store.code_used(h)
    # re-arm same code resets used_at
    store.arm_code("secret-code")
    assert store.armed_code_hash() == h
    assert not store.code_used(h)


def test_seed_only_when_unarmed(store: Store):
    store.arm_code("a")
    h2 = store.arm_code("b")
    assert store.armed_code_hash() == h2


def test_replay_cache(store: Store):
    assert not store.replay_seen("x")
    store.replay_add("x")
    assert store.replay_seen("x")
    store.replay_add("x")  # idempotent
    assert store.replay_seen("x")


def test_email_dedup(store: Store):
    assert not store.email_seen("42")
    store.mark_email_seen("42")
    assert store.email_seen("42")


def test_attempts_and_events(store: Store):
    store.log_attempt(imap_uid="1", sender="a@b.c", mode="encrypted",
                      outcome="rejected", reason="decrypt_failed")
    rows = store.recent_attempts()
    assert len(rows) == 1 and rows[0]["reason"] == "decrypt_failed"

    store.create_session(session_id="s1", host="h", port=22, ssh_user="u",
                         forwards="2222:22", ttl=60, status="starting")
    store.add_session_event("s1", "up")
    store.add_session_event("s1", "stopped:ttl")
    events = store.session_events("s1")
    assert [e["event"] for e in reversed(events)] == ["up", "stopped:ttl"]
    store.update_session("s1", "expired")
    assert store.session_events("s1", limit=10)


def test_prune(store: Store):
    store.log_attempt(imap_uid="1", sender="a", mode="m", outcome="o")
    store.prune(90)
    assert store.recent_attempts()  # recent rows survive
    store.prune(-1)  # <= 0 means keep everything
    assert store.recent_attempts()


def test_hash_code_stable():
    assert hash_code("x") == hash_code("x") and hash_code("x") != hash_code("y")
