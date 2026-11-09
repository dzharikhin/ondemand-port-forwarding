from __future__ import annotations

import aioimaplib
import pytest

from ondemandportforward.imap_client import ImapClient, ImapError


class FakeResponse:
    def __init__(self, result: str, lines: list[bytes] | None = None) -> None:
        self.result = result
        self.lines = lines or []


class FakeImap4:
    instances: list[FakeImap4] = []
    login_result: str = "OK"
    select_result: str = "OK"

    def __init__(self, **kwargs) -> None:
        self.login_result = FakeImap4.login_result
        self.select_result = FakeImap4.select_result
        self.logged_out = False
        FakeImap4.instances.append(self)

    async def wait_hello_from_server(self) -> None:
        return None

    async def login(self, user: str, password: str) -> FakeResponse:
        return FakeResponse(
            self.login_result,
            [f"{self.login_result} [AUTHENTICATIONFAILED] Invalid credentials".encode()],
        )

    async def select(self, mailbox: str) -> FakeResponse:
        return FakeResponse(self.select_result)

    async def logout(self) -> FakeResponse:
        self.logged_out = True
        return FakeResponse("OK")


@pytest.fixture
def fake_imap(monkeypatch):
    FakeImap4.instances = []
    FakeImap4.login_result = "OK"
    FakeImap4.select_result = "OK"
    monkeypatch.setattr(aioimaplib, "IMAP4_SSL", FakeImap4)
    return FakeImap4


async def test_rejected_login_raises_with_server_message(cfg, fake_imap):
    fake_imap.login_result = "NO"
    client = ImapClient(cfg)

    with pytest.raises(ImapError, match="AUTHENTICATIONFAILED.*Invalid credentials"):
        await client._ensure_connected()

    assert client.client is None
    assert fake_imap.instances[-1].logged_out


async def test_select_failure_raises(cfg, fake_imap):
    fake_imap.select_result = "NO"
    client = ImapClient(cfg)

    with pytest.raises(ImapError, match="SELECT INBOX failed"):
        await client._ensure_connected()

    assert client.client is None
    assert fake_imap.instances[-1].logged_out


async def test_successful_connect(cfg, fake_imap):
    client = ImapClient(cfg)

    await client._ensure_connected()

    assert client.client is not None
    assert not fake_imap.instances[-1].logged_out
