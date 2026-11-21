import asyncio
import sys

import pytest

from ondemandportforward.parser import Command
from ondemandportforward.sessions import (
    STATUS_DIED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_STOPPED,
    STATUS_UP,
    SessionManager,
)

ALIVE = [sys.executable, "-c", "import time; time.sleep(600)"]
DEAD = [
    sys.executable,
    "-c",
    "import sys; print('ssh: connect refused', file=sys.stderr); sys.exit(255)",
]


def make_manager(cfg, store, argv_list) -> SessionManager:
    return SessionManager(
        cfg, store, command_factory=lambda cmd: (argv_list, {"SSHPASS": cmd.ssh_password})
    )


def make_cmd(**kw) -> Command:
    base = dict(host="h.example", ssh_user="u", ssh_password="p", ttl=3,
                forwards=[(2222, "localhost", 22)])
    base.update(kw)
    return Command(**base)


async def test_success_and_ttl_expiry(cfg, store):
    manager = make_manager(cfg, store, ALIVE)
    session = manager.start(make_cmd(ttl=2))
    await asyncio.wait_for(session.established.wait(), timeout=cfg.connect_grace + 5)
    assert session.status == STATUS_UP

    await asyncio.wait_for(session._task, timeout=10)
    assert session.status == STATUS_EXPIRED
    assert store.session_events(session.id)


async def test_fast_fail_retries_then_failed(cfg, store):
    manager = make_manager(cfg, store, DEAD)
    session = manager.start(make_cmd())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(session.established.wait(), timeout=cfg.connect_grace * 2 + 5)
    await asyncio.wait_for(session._task, timeout=10)
    assert session.status == STATUS_FAILED
    assert "connect refused" in manager.error_log(session)


async def test_retry_after_fail_succeeds(cfg, store):
    attempts = {"n": 0}

    def factory(cmd):
        attempts["n"] += 1
        return (DEAD if attempts["n"] == 1 else ALIVE, {})

    manager = SessionManager(cfg, store, command_factory=factory)
    session = manager.start(make_cmd(ttl=10))
    await asyncio.wait_for(session.established.wait(), timeout=cfg.connect_grace * 2 + 5)
    assert session.status == STATUS_UP
    assert attempts["n"] == 2
    await manager.shutdown()
    assert session.status == STATUS_DIED or session._task.cancelled()


async def test_shutdown_kills_sessions(cfg, store):
    manager = make_manager(cfg, store, ALIVE)
    session = manager.start(make_cmd(ttl=600))
    await asyncio.wait_for(session.established.wait(), timeout=cfg.connect_grace + 5)
    await manager.shutdown()
    assert session._task.done()


async def test_stop_session_before_ttl(cfg, store):
    spawns = {"n": 0}

    def factory(cmd):
        spawns["n"] += 1
        return (ALIVE, {"SSHPASS": cmd.ssh_password})

    manager = SessionManager(cfg, store, command_factory=factory)
    session = manager.start(make_cmd(ttl=600))
    await asyncio.wait_for(session.established.wait(), timeout=cfg.connect_grace + 5)
    assert session.status == STATUS_UP

    stopped = await manager.stop_session(session.id)
    assert stopped is session
    assert session.status == STATUS_STOPPED
    assert session._task.done()
    assert session._proc.returncode is not None
    assert spawns["n"] == 1  # cancelled first: no retry respawn

    events = [e["event"] for e in store.session_events(session.id)]
    assert "stopped:manual" in events


async def test_stop_session_unknown_or_ended(cfg, store):
    manager = make_manager(cfg, store, ALIVE)
    assert await manager.stop_session("deadbeef") is None

    session = manager.start(make_cmd(ttl=1))
    await asyncio.wait_for(session._task, timeout=10)  # expires on its own
    assert session.status == STATUS_EXPIRED
    assert await manager.stop_session(session.id) is None


async def test_stop_all(cfg, store):
    manager = make_manager(cfg, store, ALIVE)
    first = manager.start(make_cmd(ttl=600))
    second = manager.start(make_cmd(ttl=600))
    await asyncio.wait_for(first.established.wait(), timeout=cfg.connect_grace + 5)
    await asyncio.wait_for(second.established.wait(), timeout=cfg.connect_grace + 5)

    stopped = await manager.stop_all()
    assert {s.id for s in stopped} == {first.id, second.id}
    assert first.status == STATUS_STOPPED and second.status == STATUS_STOPPED
    assert first._task.done() and second._task.done()
