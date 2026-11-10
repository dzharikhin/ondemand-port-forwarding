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
    assert cfg.plaintext_code == ""
    assert cfg.data_dir == "./data"
    assert cfg.imap_mailbox == "INBOX"


def test_mailbox_override():
    cfg = Config.from_env({
        "IMAP_HOST": "i", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
        "SMTP_HOST": "s", "SECRET": "x", "IMAP_MAILBOX": "INBOX/ToMyself",
    })
    assert cfg.imap_mailbox == "INBOX/ToMyself"


def test_bad_int():
    env = {
        "IMAP_HOST": "i", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
        "SMTP_HOST": "s", "SECRET": "x", "TTL_DEFAULT": "abc",
    }
    with pytest.raises(ConfigError, match="TTL_DEFAULT"):
        Config.from_env(env)
