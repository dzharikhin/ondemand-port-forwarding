from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass
from email.utils import parseaddr

from . import gpg
from .config import Config
from .parser import Command, ParseError, parse_command
from .store import Store, hash_body, hash_code

MODE_ENCRYPTED = "encrypted"
MODE_PLAINTEXT = "plaintext"
MODE_UNKNOWN = "unknown"


@dataclass
class Verdict:
    accepted: bool
    mode: str
    reason: str
    command: Command | None = None


def _reject(mode: str, reason: str) -> Verdict:
    return Verdict(accepted=False, mode=mode, reason=reason)


def _sender_ok(cfg: Config, sender: str) -> bool:
    addr = parseaddr(sender)[1].strip().lower()
    return addr == cfg.imap_user_lower


def _headers_ok(auth_results: str | None) -> bool:
    if not auth_results:
        return False
    lower = auth_results.lower()
    return "spf=pass" in lower and "dkim=pass" in lower


class Validator:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store

    async def check(self, sender: str, body_text: str, auth_results: str | None = None) -> Verdict:
        if not _sender_ok(self.cfg, sender):
            return _reject(MODE_UNKNOWN, "bad_sender")

        if self.cfg.strict_headers and not _headers_ok(auth_results):
            return _reject(MODE_UNKNOWN, "bad_headers")

        block = gpg.extract_pgp_block(body_text)
        if block is not None:
            return await self._check_encrypted(block)
        return self._check_plaintext(body_text)

    async def _check_encrypted(self, block: str) -> Verdict:
        body_hash = hash_body(block)
        if self.store.replay_seen(body_hash):
            return _reject(MODE_ENCRYPTED, "replay")

        try:
            plaintext = await asyncio.to_thread(gpg.decrypt, block, self.cfg.secret)
        except gpg.GpgError:
            # Burn the block: repeated tries of the same ciphertext are pointless.
            self.store.replay_add(body_hash)
            return _reject(MODE_ENCRYPTED, "decrypt_failed")

        self.store.replay_add(body_hash)
        try:
            command = parse_command(plaintext, self.cfg.ttl_default)
        except ParseError as e:
            return _reject(MODE_ENCRYPTED, f"parse_error: {e}")
        return Verdict(accepted=True, mode=MODE_ENCRYPTED, reason="ok", command=command)

    def _check_plaintext(self, body_text: str) -> Verdict:
        armed = self.store.armed_code_hash()

        try:
            command = parse_command(body_text, self.cfg.ttl_default)
        except ParseError as e:
            return (
                _reject(MODE_PLAINTEXT, "plaintext_disabled")
                if armed is None
                else _reject(MODE_PLAINTEXT, f"parse_error: {e}")
            )

        if not command.code:
            return _reject(MODE_PLAINTEXT, "bad_code: missing 'code' field")

        code_hash = hash_code(command.code)
        if armed is not None and hmac.compare_digest(code_hash, armed):
            # Strict one-time: burn at the moment of acceptance.
            self.store.burn_code(armed)
            return Verdict(accepted=True, mode=MODE_PLAINTEXT, reason="ok", command=command)

        if self.store.code_used(code_hash):
            # Alarm: this exact code has already been burned.
            return _reject(MODE_PLAINTEXT, "code_used")

        return (
            _reject(MODE_PLAINTEXT, "plaintext_disabled")
            if armed is None
            else _reject(MODE_PLAINTEXT, "bad_code")
        )
