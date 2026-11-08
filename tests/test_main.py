import email.message
import sys

import pytest

from ondemandportforward import gpg
from ondemandportforward.main import make_handler
from ondemandportforward.sessions import SessionManager
from ondemandportforward.store import Store
from ondemandportforward.validator import Validator

SECRET = "test-secret"
ALIVE = [sys.executable, "-c", "import time; time.sleep(600)"]


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send_success(self, to, session):
        self.sent.append(("success", to, session.id))

    async def send_failure(self, to, reason, log_tail=""):
        self.sent.append(("failure", to, reason))


@pytest.fixture
def harness(cfg, tmp_path):
    store = Store(str(tmp_path / "data"))
    validator = Validator(cfg, store)
    manager = SessionManager(
        cfg, store, command_factory=lambda cmd: (ALIVE, {"SSHPASS": cmd.ssh_password})
    )
    notifier = FakeNotifier()
    handler = make_handler(cfg, store, validator, manager, notifier)
    return handler, store, manager, notifier


def make_email(from_addr: str, body: str) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = "open tunnel"
    msg.set_content(body)
    return msg


async def test_encrypted_email_end_to_end(cfg, harness):
    handler, store, manager, notifier = harness
    body = gpg.encrypt("host: 1.2.3.4\nuser: root\npassword: pw\nttl: 300\n", SECRET)
    await handler("10", make_email("user@example.com", body))

    assert notifier.sent and notifier.sent[0][0] == "success"
    assert store.email_seen("10")
    assert store.recent_attempts()[0]["outcome"] == "accepted"
    await manager.shutdown()


async def test_bad_sender_is_silent(cfg, harness):
    handler, store, manager, notifier = harness
    body = gpg.encrypt("host: 1.2.3.4\nuser: root\npassword: pw\n", SECRET)
    await handler("10", make_email("attacker@evil.com", body))

    assert notifier.sent == []
    assert store.recent_attempts()[0]["outcome"] == "ignored"
    await manager.shutdown()


async def test_uid_dedup(cfg, harness):
    handler, store, manager, notifier = harness
    body = gpg.encrypt("host: 1.2.3.4\nuser: root\npassword: pw\nttl: 300\n", SECRET)
    await handler("10", make_email("user@example.com", body))
    await handler("10", make_email("user@example.com", body))  # same uid again

    assert len(notifier.sent) == 1
    await manager.shutdown()


async def test_plaintext_code_burn_across_handler(cfg, harness):
    handler, store, manager, notifier = harness
    store.arm_code("abc123")
    body = "host: 1.2.3.4\nuser: root\npassword: pw\nttl: 300\ncode: abc123\n"
    await handler("7", make_email("user@example.com", body))
    assert notifier.sent[0][0] == "success"

    # resend with the same (now dead) code -> alarm
    await handler("8", make_email("user@example.com", body))
    assert notifier.sent[1][0] == "failure"
    assert notifier.sent[1][2] == "code_used"
    await manager.shutdown()
