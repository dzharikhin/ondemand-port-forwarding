import pytest

from ondemandportforward.config import Config, ConfigError


def test_missing_required():
    with pytest.raises(ConfigError, match="IMAP_HOST"):
        Config.from_env({})


def test_defaults():
    cfg = Config.from_env({
        "IMAP_HOST": "i", "IMAP_USER": "me@example.com", "IMAP_PASSWORD": "p",
        "SMTP_HOST": "s", "SECRET": "x",
    })
    assert cfg.imap_port == 993 and cfg.smtp_port == 465
    assert cfg.smtp_user == "me@example.com"  # defaults to IMAP creds
    assert cfg.smtp_password == "p"
    assert cfg.mail_from == "me@example.com"
    assert cfg.ttl_default == 1800 and cfg.retries == 3
    assert cfg.forwards_default == "2222:22"
    assert cfg.plaintext_code == ""
    assert cfg.data_dir == "./data"
    assert cfg.imap_mailboxes == ["INBOX"]


def test_mailbox_list():
    cfg = Config.from_env({
        "IMAP_HOST": "i", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
        "SMTP_HOST": "s", "SECRET": "x",
        "IMAP_MAILBOX": "INBOX, INBOX/ToMyself ,INBOX",
    })
    assert cfg.imap_mailboxes == ["INBOX", "INBOX/ToMyself"]


def test_mailbox_override():
    cfg = Config.from_env({
        "IMAP_HOST": "i", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
        "SMTP_HOST": "s", "SECRET": "x", "IMAP_MAILBOX": "INBOX/ToMyself",
    })
    assert cfg.imap_mailboxes == ["INBOX/ToMyself"]


def test_bad_int():
    env = {
        "IMAP_HOST": "i", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
        "SMTP_HOST": "s", "SECRET": "x", "TTL_DEFAULT": "abc",
    }
    with pytest.raises(ConfigError, match="TTL_DEFAULT"):
        Config.from_env(env)


def test_forwards_default():
    base = {
        "IMAP_HOST": "i", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
        "SMTP_HOST": "s", "SECRET": "x",
    }
    cfg = Config.from_env({**base, "FORWARDS_DEFAULT": "2222:192.168.1.20:22"})
    assert cfg.forwards_default == "2222:192.168.1.20:22"
    cfg = Config.from_env({**base, "FORWARDS_DEFAULT": "2200:22, 8080:nas.lan:8080"})
    assert cfg.forwards_default == "2200:22, 8080:nas.lan:8080"
    cfg = Config.from_env({**base, "FORWARDS_DEFAULT": "  "})
    assert cfg.forwards_default == "2222:22"  # blank falls back
    for bad in ("22", "a:b", "1:2:3:4", "2222:bad host:22"):
        with pytest.raises(ConfigError, match="FORWARDS_DEFAULT"):
            Config.from_env({**base, "FORWARDS_DEFAULT": bad})
