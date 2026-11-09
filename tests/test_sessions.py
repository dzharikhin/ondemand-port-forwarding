import asyncio
import sys

import pytest

from ondemandportforward.parser import Command
from ondemandportforward.sessions import (
    STATUS_DIED,
    STATUS_EXPIRED,
    STATUS_FAILED,
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
