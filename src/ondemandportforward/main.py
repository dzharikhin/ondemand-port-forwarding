from __future__ import annotations

import asyncio
import logging
import signal
from email.message import EmailMessage
from email.utils import parseaddr

from .config import Config
from .imap_client import ImapClient, extract_body
from .notifier import Notifier
from .sessions import SessionManager
from .store import Store
from .validator import Validator

log = logging.getLogger("ondemandportforward.main")


def make_handler(
    cfg: Config, store: Store, validator: Validator, manager: SessionManager, notifier: Notifier
):
    async def on_email(uid: str, msg: EmailMessage) -> None:
        if store.email_seen(uid):
            return
        store.mark_email_seen(uid)

        sender = str(msg.get("From", ""))
        sender_addr = parseaddr(sender)[1] or "unknown"

        # Never react to our own replies (would create a reply loop).
        subject = str(msg.get("Subject", ""))
        if subject.replace("Re:", "").lstrip().startswith("[ondemandportforward]"):
            log.info("UID %s: own reply, ignored", uid)
            store.log_attempt(
                imap_uid=uid, sender=sender_addr, mode="unknown",
                outcome="ignored", reason="own_reply",
            )
            return

        auth_results = str(msg.get("Authentication-Results") or "")
        body = extract_body(msg)
        log.info("processing UID %s from %s (%d chars)", uid, sender_addr, len(body))

        # Sender is not validated; status replies always go to the owner.
        to = cfg.imap_user

        verdict = await validator.check(body, auth_results)

        if not verdict.accepted:
            log.warning("UID %s rejected: %s", uid, verdict.reason)
            store.log_attempt(
                imap_uid=uid, sender=sender_addr, mode=verdict.mode,
                outcome="rejected", reason=verdict.reason,
            )
            try:
                await notifier.send_failure(to, verdict.reason)
            except Exception:  # noqa: BLE001
                log.exception("failed to send rejection reply")
            return

        session = manager.start(verdict.command)
        log.info("UID %s accepted -> session %s", uid, session.id)
        store.log_attempt(
            imap_uid=uid, sender=sender_addr, mode=verdict.mode,
            outcome="accepted", session_id=session.id,
        )

        wait_total = cfg.connect_grace * (cfg.retries + 1) + 5
        established = False
        try:
            await asyncio.wait_for(session.established.wait(), timeout=wait_total)
            established = True
        except TimeoutError:
            pass

        try:
            if established:
                await notifier.send_success(to, session)
            else:
                await notifier.send_failure(
                    to, f"session {session.id} did not come up", manager.error_log(session)
                )
        except Exception:  # noqa: BLE001
            log.exception("failed to send reply for UID %s", uid)

    return on_email


async def run_daemon() -> None:
    cfg = Config.from_env()
    store = Store(cfg.data_dir)
    store.prune(cfg.log_retention_days)

    if cfg.plaintext_code and store.armed_code_hash() is None:
        store.arm_code(cfg.plaintext_code)
        log.info("seeded one-time plaintext code from PLAINTEXT_CODE env")

    validator = Validator(cfg, store)
    manager = SessionManager(cfg, store)
    notifier = Notifier(cfg)

    handler = make_handler(cfg, store, validator, manager, notifier)
    clients = [ImapClient(cfg, store, mb) for mb in cfg.imap_mailboxes]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    watchers = [asyncio.create_task(c.run(handler), name=f"imap-{c.mailbox}") for c in clients]
    stop_waiter = asyncio.create_task(stop.wait())

    done, _ = await asyncio.wait(
        [*watchers, stop_waiter], return_when=asyncio.FIRST_COMPLETED
    )
    crashed = [t for t in watchers if t.done() and t.exception() is not None]
    for t in crashed:
        log.error("imap loop crashed: %r", t.exception())
    if not crashed:
        log.info("shutdown requested")

    for t in watchers:
        t.cancel()
    for c in clients:
        await c.stop()
    await manager.shutdown()
    store.close()
