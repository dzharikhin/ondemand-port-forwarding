from __future__ import annotations

import pytest

from ondemandportforward.config import Config
from ondemandportforward.store import Store


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        imap_host="imap.example.com",
        imap_user="user@example.com",
        imap_password="imap-pass",
        smtp_host="smtp.example.com",
        secret="test-secret",
        data_dir=str(tmp_path),
        connect_grace=1,
        retries=1,
    )


@pytest.fixture
def store(cfg) -> Store:
    s = Store(cfg.data_dir)
    yield s
    s.close()
