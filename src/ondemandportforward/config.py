from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass
class Config:
    imap_host: str
    imap_user: str
    imap_password: str
    smtp_host: str
    secret: str

    imap_port: int = 993
    imap_tls: bool = True
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""

    plaintext_code: str = ""
    ttl_default: int = 1800
    retries: int = 3
    log_retention_days: int = 90
    strict_headers: bool = False
    poll_interval: int = 60
    connect_grace: int = 10
    data_dir: str = "./data"

    def __post_init__(self) -> None:
        self.smtp_user = self.smtp_user or self.imap_user
        self.smtp_password = self.smtp_password or self.imap_password
        self.mail_from = self.mail_from or self.smtp_user
        self.imap_user_lower = self.imap_user.strip().lower()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = env if env is not None else os.environ

        def req(name: str) -> str:
            v = e.get(name, "").strip()
            if not v:
                raise ConfigError(f"missing required env {name}")
            return v

        def opt_int(name: str, default: int) -> int:
            v = e.get(name, "").strip()
            try:
                return int(v) if v else default
            except ValueError:
                raise ConfigError(f"env {name} must be an integer, got {v!r}") from None

        def opt_bool(name: str) -> bool:
            return e.get(name, "").strip().lower() in ("1", "true", "yes")

        return cls(
            imap_host=req("IMAP_HOST"),
            imap_user=req("IMAP_USER"),
            imap_password=req("IMAP_PASSWORD"),
            smtp_host=req("SMTP_HOST"),
            secret=req("SECRET"),
            imap_port=opt_int("IMAP_PORT", 993),
            imap_tls=not opt_bool("IMAP_TLS_OFF"),
            smtp_port=opt_int("SMTP_PORT", 465),
            smtp_user=e.get("SMTP_USER", "").strip(),
            smtp_password=e.get("SMTP_PASSWORD", ""),
            mail_from=e.get("MAIL_FROM", "").strip(),
            plaintext_code=e.get("PLAINTEXT_CODE", ""),
            ttl_default=opt_int("TTL_DEFAULT", 1800),
            retries=opt_int("RETRIES", 3),
            log_retention_days=opt_int("LOG_RETENTION_DAYS", 90),
            strict_headers=opt_bool("STRICT_HEADERS"),
            poll_interval=opt_int("POLL_INTERVAL", 60),
            connect_grace=opt_int("CONNECT_GRACE", 10),
            data_dir=e.get("DATA_DIR", "./data").strip() or "./data",
        )


def mask_secret(value: str, keep: int = 2) -> str:
    if len(value) <= keep:
        return "***"
    return value[:keep] + "***"
