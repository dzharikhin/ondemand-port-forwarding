from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .parser import Command
from .store import Store

log = logging.getLogger("ondemandportforward.sessions")

STATUS_STARTING = "starting"
STATUS_UP = "up"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"
STATUS_DIED = "died"

ERROR_LOG_LINES = 40

CommandFactory = Callable[[Command], tuple[list[str], dict[str, str]]]


def default_command_factory(cmd: Command) -> tuple[list[str], dict[str, str]]:
    argv = ["sshpass", "-e", "ssh", "-N"]
    argv += ["-p", str(cmd.port)]
    argv += [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
    ]
    for remote, host, local in cmd.forwards:
        argv += ["-R", f"{remote}:{host}:{local}"]
    argv += [f"{cmd.ssh_user}@{cmd.host}"]
    env = {"SSHPASS": cmd.ssh_password}
    return argv, env


@dataclass
class Session:
    id: str
    command: Command
    status: str = STATUS_STARTING
    established: asyncio.Event = field(default_factory=asyncio.Event)
    log: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def forwards_desc(self) -> str:
        return ", ".join(
            f"{remote}:{host}:{local}" for remote, host, local in self.command.forwards
        )


class SessionManager:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        command_factory: CommandFactory = default_command_factory,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.command_factory = command_factory
        self.sessions: dict[str, Session] = {}

    def start(self, cmd: Command) -> Session:
        session_id = secrets.token_hex(4)
        session = Session(id=session_id, command=cmd)
        self.sessions[session_id] = session
        self.store.create_session(
            session_id=session_id,
            host=cmd.host,
            port=cmd.port,
            ssh_user=cmd.ssh_user,
            forwards=",".join(
                f"{remote}:{host}:{local}" for remote, host, local in cmd.forwards
            ),
            ttl=cmd.ttl,
            status=STATUS_STARTING,
        )
        session._task = asyncio.create_task(
            self._run(session), name=f"ondemandportforward-session-{session_id}"
        )
        return session

    async def _run(self, session: Session) -> None:
        cmd = session.command
        deadline = time.monotonic() + cmd.ttl
        expires_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + cmd.ttl))
        log.info(
            "session %s started: tunnel to %s@%s:%s, forwards=[%s], ttl=%ss (expires %s)",
            session.id,
            cmd.ssh_user,
            cmd.host,
            cmd.port,
            session.forwards_desc,
            cmd.ttl,
            expires_at,
        )
        attempts_left = self.cfg.retries + 1

        while attempts_left > 0 and time.monotonic() < deadline:
            attempts_left -= 1
            argv, extra_env = self.command_factory(cmd)
            self.store.add_session_event(session.id, "start", " ".join(argv))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, **extra_env},
                )
            except Exception as e:  # noqa: BLE001 - spawn failure is a session failure
                session.log.append(f"spawn error: {e}")
                self.store.add_session_event(session.id, "spawn_error", str(e))
                continue

            reader = asyncio.create_task(self._pump_stderr(proc, session))
            session._proc = proc

            exit_task = asyncio.create_task(proc.wait())
            done, _ = await asyncio.wait({exit_task}, timeout=self.cfg.connect_grace)

            if exit_task not in done:
                # survived the grace period -> considered established
                session.established.set()
                session.status = STATUS_UP
                self.store.update_session(session.id, STATUS_UP)
                self.store.add_session_event(session.id, "up")
                log.info("session %s up, closes at %s", session.id, expires_at)

                remaining = deadline - time.monotonic()
                try:
                    await asyncio.wait_for(asyncio.shield(exit_task), timeout=remaining)
                except TimeoutError:
                    log.info(
                        "session %s closed by TTL (was up for %ss)",
                        session.id,
                        cmd.ttl,
                    )
                    await self._stop(proc, session, STATUS_EXPIRED, "ttl")
                    await reader
                    return
                else:
                    await reader
                    session.status = STATUS_DIED
                    session.established.clear()
                    log.warning(
                        "session %s died after establishment (exit=%s)",
                        session.id,
                        proc.returncode,
                    )
                    self.store.add_session_event(
                        session.id,
                        "died",
                        f"exit={proc.returncode} after establishment; "
                        f"{'retrying' if attempts_left > 0 else 'giving up'}",
                    )
                continue

            # exited during grace period -> failed attempt
            await exit_task
            await reader
            session.log.append(f"ssh exited with code {proc.returncode}")
            self.store.add_session_event(
                session.id,
                "attempt_failed",
                f"exit={proc.returncode}; "
                f"{'retrying' if attempts_left > 0 else 'giving up'}",
            )

        if session.status == STATUS_DIED:
            self.store.update_session(session.id, STATUS_DIED)
        elif not session.established.is_set():
            session.status = STATUS_FAILED
            self.store.update_session(session.id, STATUS_FAILED)
            tail = "\n".join(list(session.log)[-ERROR_LOG_LINES:])
            self.store.add_session_event(session.id, "failed", tail)
            log.error("session %s failed to establish", session.id)

    async def _pump_stderr(self, proc: asyncio.subprocess.Process, session: Session) -> None:
        assert proc.stderr is not None
        password = session.command.ssh_password
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if password:
                line = line.replace(password, "***")
            if line:
                session.log.append(line)

    async def _stop(
        self,
        proc: asyncio.subprocess.Process,
        session: Session,
        status: str,
        reason: str,
    ) -> None:
        session.status = status
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        self.store.add_session_event(session.id, f"stopped:{reason}")

    def error_log(self, session: Session) -> str:
        return "\n".join(list(session.log)[-ERROR_LOG_LINES:])

    async def shutdown(self) -> None:
        for session in self.sessions.values():
            proc = session._proc
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        for session in self.sessions.values():
            if session._task and not session._task.done():
                session._task.cancel()
        for session in self.sessions.values():
            if session._task:
                try:
                    await session._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    def describe(self, session: Session) -> dict[str, Any]:
        cmd = session.command
        return {
            "id": session.id,
            "status": session.status,
            "host": cmd.host,
            "port": cmd.port,
            "user": cmd.ssh_user,
            "forwards": session.forwards_desc,
            "ttl": cmd.ttl,
        }
