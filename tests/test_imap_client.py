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
    selected_mailbox: list[str] = []
    uidvalidity: int | None = 111
    search_uids: bytes = b"* SEARCH 139851 139852"

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
        FakeImap4.selected_mailbox.append(mailbox)
        lines = [f"* OK [UIDVALIDITY {FakeImap4.uidvalidity}] UIDs valid".encode()]
        return FakeResponse(self.select_result, lines)

    async def uid_search(self, *criteria: str) -> FakeResponse:
        return FakeResponse("OK", [FakeImap4.search_uids])

    async def logout(self) -> FakeResponse:
        self.logged_out = True
        return FakeResponse("OK")


@pytest.fixture
def fake_imap(monkeypatch):
    FakeImap4.instances = []
    FakeImap4.login_result = "OK"
    FakeImap4.select_result = "OK"
    FakeImap4.selected_mailbox = []
    FakeImap4.uidvalidity = 111
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


async def test_configured_mailbox_is_selected(cfg, fake_imap):
    client = ImapClient(cfg, mailbox="INBOX/ToMyself")

    await client._ensure_connected()

    assert fake_imap.selected_mailbox == ["INBOX/ToMyself"]
    assert client.mailbox == "INBOX/ToMyself"


async def test_defaults_to_first_configured_mailbox(cfg, fake_imap):
    cfg.imap_mailboxes = ["INBOX/ToMyself", "INBOX"]
    client = ImapClient(cfg, store=None)

    await client._ensure_connected()

    assert fake_imap.selected_mailbox == ["INBOX/ToMyself"]


async def test_bootstrap_restores_matching_watermark(cfg, fake_imap, store):
    store.set_meta("last_uid:INBOX", "139852")
    store.set_meta("uidvalidity:INBOX", "111")
    store.set_meta("uid_mode:INBOX", "1")
    client = ImapClient(cfg, store)

    await client._ensure_connected()
    await client._bootstrap_last_uid()

    assert client.last_uid == 139852


async def test_bootstrap_migrates_legacy_watermark(cfg, fake_imap, store):
    # single-mailbox build wrote unscoped keys
    store.set_meta("last_uid", "139852")
    store.set_meta("uidvalidity", "111")
    store.set_meta("uid_mode", "1")
    client = ImapClient(cfg, store)

    await client._ensure_connected()
    await client._bootstrap_last_uid()

    assert client.last_uid == 139852
    assert store.get_meta("last_uid:INBOX") == "139852"
    assert store.get_meta("uidvalidity:INBOX") == "111"
    # legacy keys are consumed: no other mailbox can adopt them
    assert store.get_meta("last_uid") is None
    assert store.get_meta("uidvalidity") is None
    assert store.get_meta("uid_mode") is None


async def test_watermarks_are_per_mailbox(cfg, fake_imap, store):
    store.set_meta("last_uid:INBOX", "139852")
    store.set_meta("uidvalidity:INBOX", "111")
    store.set_meta("uid_mode:INBOX", "1")
    client = ImapClient(cfg, store, mailbox="INBOX/ToMyself")

    await client._ensure_connected()
    await client._bootstrap_last_uid()

    # no scoped watermark for this mailbox: bootstraps fresh from search and
    # does not touch INBOX's keys
    assert client.last_uid == 139852
    assert store.get_meta("last_uid:INBOX") == "139852"
    assert store.get_meta("last_uid:INBOX/ToMyself") == "139852"


async def test_bootstrap_resets_stale_watermark(cfg, fake_imap, store):
    # legacy store: watermark saved without uidvalidity (or from another mailbox)
    store.set_meta("last_uid:INBOX", "16433")
    client = ImapClient(cfg, store)

    await client._ensure_connected()
    await client._bootstrap_last_uid()

    assert client.last_uid == 139852  # re-bootstrapped from search, not 16433
    assert store.get_meta("uidvalidity:INBOX") == "111"
    assert store.get_meta("uid_mode:INBOX") == "1"
    assert store.get_meta("last_uid:INBOX") == "139852"


async def test_bootstrap_resets_on_uidvalidity_change(cfg, fake_imap, store):
    store.set_meta("last_uid:INBOX", "100")
    store.set_meta("uidvalidity:INBOX", "999")  # server rebuilt the mailbox
    store.set_meta("uid_mode:INBOX", "1")
    client = ImapClient(cfg, store)

    await client._ensure_connected()
    await client._bootstrap_last_uid()

    assert client.last_uid == 139852


async def test_bootstrap_resets_sequence_number_watermark(cfg, fake_imap, store):
    # written by a build whose SEARCH returned sequence numbers: no uid_mode
    store.set_meta("last_uid:INBOX", "13017")
    store.set_meta("uidvalidity:INBOX", "111")  # matching uidvalidity is not enough
    client = ImapClient(cfg, store)

    await client._ensure_connected()
    await client._bootstrap_last_uid()

    assert client.last_uid == 139852


async def test_successful_connect(cfg, fake_imap):
    client = ImapClient(cfg)

    await client._ensure_connected()

    assert client.client is not None
    assert not fake_imap.instances[-1].logged_out
