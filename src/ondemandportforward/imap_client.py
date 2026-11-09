from __future__ import annotations

import asyncio
import email
import email.policy
import logging
import re
from collections.abc import Awaitable, Callable
from email.message import EmailMessage

import aioimaplib

from .config import Config
from .store import Store

log = logging.getLogger("ondemandportforward.imap")

IDLE_REISSUE_SECONDS = 25 * 60  # re-issue IDLE well before server-side 29 min cap

Handler = Callable[[str, EmailMessage], Awaitable[None]]


def extract_body(msg: EmailMessage) -> str:
    """Return the first non-attachment text/plain part, decoded."""
    if not msg.is_multipart():
        part = msg
        if part.get_content_type() == "text/plain":
            return part.get_content()
        return msg.get_payload(decode=True).decode(
            part.get_content_charset() or "utf-8", errors="replace"
        )

    candidates: list[str] = []
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            candidates.append(part.get_content())
    if candidates:
        return candidates[0]
    raw = msg.get_payload(decode=True)
    if raw:
        return raw.decode("utf-8", errors="replace")
    return ""


class ImapError(Exception):
    pass


def _server_error(resp) -> str:
    """Human-readable text from an aioimaplib Response (result is not OK)."""
    text = " ".join(
        line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        for line in resp.lines
    ).strip()
    return text or resp.result


def _response_text(resp) -> str:
    return " ".join(
        line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        for line in resp.lines
    )


def _uidvalidity_from_response(resp) -> int | None:
    m = re.search(r"UIDVALIDITY (\d+)", _response_text(resp))
    return int(m.group(1)) if m else None


class ImapClient:
    def __init__(self, cfg: Config, store: Store | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.client: aioimaplib.IMAP4 | None = None
        self.last_uid = 0
        self.uidvalidity: int | None = None

    async def run(self, handler: Handler) -> None:
        while True:
            try:
                await self._ensure_connected()
                await self._bootstrap_last_uid()
                await self._process_new(handler)
                await self._wait_for_mail()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on any mail failure
                log.exception("imap error, reconnecting in %ss", self.cfg.poll_interval)
                await self._close()
                await asyncio.sleep(self.cfg.poll_interval)

    # -- connection ---------------------------------------------------------

    async def _ensure_connected(self) -> None:
        if self.client is not None:
            return
        if self.cfg.imap_tls:
            c = aioimaplib.IMAP4_SSL(
                host=self.cfg.imap_host, port=self.cfg.imap_port, timeout=60
            )
        else:
            c = aioimaplib.IMAP4(host=self.cfg.imap_host, port=self.cfg.imap_port, timeout=60)
        await c.wait_hello_from_server()
        resp = await c.login(self.cfg.imap_user, self.cfg.imap_password)
        if resp.result != "OK":
            await self._quiet_logout(c)
            raise ImapError(
                f"IMAP login rejected for {self.cfg.imap_user} on {self.cfg.imap_host}: "
                f"{_server_error(resp)}"
            )
        resp = await c.select(self.cfg.imap_mailbox)
        if resp.result != "OK":
            await self._quiet_logout(c)
            raise ImapError(
                f"IMAP SELECT {self.cfg.imap_mailbox} failed on {self.cfg.imap_host}: "
                f"{_server_error(resp)}"
            )
        self.client = c
        self.uidvalidity = _uidvalidity_from_response(resp)
        log.info(
            "connected to %s as %s (mailbox %s)",
            self.cfg.imap_host,
            self.cfg.imap_user,
            self.cfg.imap_mailbox,
        )

    async def _quiet_logout(self, c: aioimaplib.IMAP4) -> None:
        try:
            await c.logout()
        except Exception:  # noqa: BLE001
            pass

    async def _close(self) -> None:
        if self.client is not None:
            try:
                await self.client.logout()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

    # -- uid bookkeeping ------------------------------------------------------

    async def _bootstrap_last_uid(self) -> None:
        """Skip mail that predates the first start; persist the watermark."""
        if self.last_uid > 0:
            return
        if self.store is not None:
            saved = self.store.get_meta("last_uid")
            saved_uv = self.store.get_meta("uidvalidity")
            saved_mode = self.store.get_meta("uid_mode")
            if (
                saved is not None
                and saved_mode == "1"
                and self._watermark_valid(saved_uv)
            ):
                self.last_uid = int(saved)
                log.info("restored watermark from store: UID %s", self.last_uid)
                return
            if saved is not None:
                log.warning(
                    "stored watermark UID %s is not valid for this mailbox "
                    "(uidvalidity stored=%s current=%s, uid_mode=%s); "
                    "re-bootstrapping",
                    saved,
                    saved_uv,
                    self.uidvalidity,
                    saved_mode,
                )
        uids = await self._search_uids("1:*")
        self.last_uid = max((int(u) for u in uids), default=0)
        log.info("bootstrapped, starting from UID %s", self.last_uid + 1)
        if self.store is not None:
            self.store.set_meta("last_uid", str(self.last_uid))
            self.store.set_meta("uid_mode", "1")
            if self.uidvalidity is not None:
                self.store.set_meta("uidvalidity", str(self.uidvalidity))

    def _watermark_valid(self, saved_uv: str | None) -> bool:
        """A saved watermark is only valid within the same UIDVALIDITY."""
        if self.uidvalidity is None:
            return True  # server did not report UIDVALIDITY: keep old behaviour
        if saved_uv is None:
            return False
        try:
            return int(saved_uv) == self.uidvalidity
        except ValueError:
            return False

    async def _search_uids(self, *criteria: str) -> list[str]:
        # UID SEARCH is required: some servers (e.g. mail.ru) answer a plain
        # SEARCH with sequence numbers even when a UID criterion is given.
        assert self.client is not None
        resp = await self.client.uid_search(*criteria)
        text = b" ".join(
            line for line in resp.lines if isinstance(line, bytes)
        ).decode(errors="replace")
        return re.findall(r"\d+", text)

    async def _fetch(self, uid: str) -> EmailMessage | None:
        assert self.client is not None
        resp = await self.client.uid("FETCH", uid, "(BODY.PEEK[])")
        best: bytes | None = None
        for line in resp.lines:
            # aioimaplib returns the message body as a bytearray
            if isinstance(line, (bytes, bytearray)):
                raw = bytes(line)
                if best is None or len(raw) > len(best):
                    best = raw
        if not best:
            return None
        msg = email.message_from_bytes(best, policy=email.policy.default)
        return msg if isinstance(msg, EmailMessage) else None

    # -- processing ------------------------------------------------------------

    async def _process_new(self, handler: Handler) -> None:
        uids = await self._search_uids(f"{self.last_uid + 1}:*")
        for uid in sorted(int(u) for u in uids):
            if uid <= self.last_uid:
                continue
            self.last_uid = uid
            try:
                msg = await self._fetch(str(uid))
            except Exception:  # noqa: BLE001
                log.exception("failed to fetch UID %s, skipping", uid)
                continue
            if msg is None:
                continue
            try:
                await handler(str(uid), msg)
            except Exception:  # noqa: BLE001
                log.exception("handler failed for UID %s", uid)
            if self.store is not None:
                self.store.set_meta("last_uid", str(uid))
            try:
                assert self.client is not None
                await self.client.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
            except Exception:  # noqa: BLE001
                log.warning("could not mark UID %s as seen", uid)

    async def _wait_for_mail(self) -> None:
        """IDLE with poll fallback on providers that don't support it."""
        assert self.client is not None
        try:
            await self.client.idle_start(timeout=IDLE_REISSUE_SECONDS)
            await self.client.wait_server_push(timeout=IDLE_REISSUE_SECONDS)
        except TimeoutError:
            pass  # no mail within the window: re-issue IDLE (loop continues)
        finally:
            try:
                self.client.idle_done()
            except Exception:  # noqa: BLE001 - force reconnect on IDLE problems
                log.warning("IDLE teardown failed, reconnecting")
                await self._close()
                await asyncio.sleep(self.cfg.poll_interval)

    async def stop(self) -> None:
        await self._close()
