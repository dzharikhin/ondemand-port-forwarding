from __future__ import annotations

import email.message
import logging

import aiosmtplib

from .config import Config
from .sessions import ERROR_LOG_LINES, Session

log = logging.getLogger("ondemandportforward.notifier")


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def _send(self, to: str, subject: str, body: str) -> None:
        if not self.cfg.smtp_host:
            # SMTP disabled: the daemon log is the feedback channel.
            log.info("reply (smtp disabled) to %s: %s\n%s", to, subject, body)
            return
        msg = email.message.EmailMessage()
        msg["From"] = self.cfg.mail_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        smtp = aiosmtplib.SMTP(
            hostname=self.cfg.smtp_host,
            port=self.cfg.smtp_port,
            use_tls=self.cfg.smtp_port == 465,
        )
        async with smtp:
            await smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
            await smtp.send_message(msg)
        log.info("reply sent to %s: %s", to, subject)

    async def send_success(self, to: str, session: Session) -> None:
        cmd = session.command
        lines = [
            f"Tunnel is UP: {cmd.ssh_user}@{cmd.host}:{cmd.port}  (session {session.id})",
            f"Valid for {cmd.ttl}s from start.",
            "",
            "Forwarded ports (on the remote host):",
        ]
        for remote, host, local in cmd.forwards:
            hint = f"  localhost:{remote} -> {host}:{local}"
            if local == 22:
                hint += f"   (ssh: `ssh -p {remote} <user>@localhost` on {cmd.host})"
            lines.append(hint)
        lines += ["", "The tunnel closes automatically when the TTL expires."]
        await self._send(
            to,
            f"[ondemandportforward] tunnel UP {cmd.ssh_user}@{cmd.host} ({session.id})",
            "\n".join(lines),
        )

    async def send_failure(self, to: str, reason: str, log_tail: str = "") -> None:
        body = f"Command rejected or failed.\n\nReason: {reason}"
        if log_tail:
            body += f"\n\nLast log lines:\n{log_tail[-ERROR_LOG_LINES * 120:]}"
        if reason == "code_used":
            body += (
                "\n\nThe one-time code was already used. "
                "Set a new one: docker compose run --rm ondemandportforward set-code"
            )
        await self._send(to, "[ondemandportforward] FAILED", body)

    async def send_stopped(self, to: str, stopped_ids: list[str]) -> None:
        ids = ", ".join(stopped_ids)
        body = (
            f"Stopped {len(stopped_ids)} session(s): {ids}\n\n"
            "The tunnel(s) are closed; the forwards on the mediator are gone."
        )
        await self._send(to, f"[ondemandportforward] tunnel STOPPED ({ids})", body)
