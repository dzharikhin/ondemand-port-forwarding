from __future__ import annotations

import re
from dataclasses import dataclass, field

LINE_RE = re.compile(r"^\s*([A-Za-z_]+)\s*:\s*(.*?)\s*$")

MIN_TTL = 60
MAX_TTL = 86400
MAX_FORWARDS = 8


class ParseError(Exception):
    pass


@dataclass
class Command:
    host: str
    ssh_user: str
    ssh_password: str
    port: int = 22
    forwards: list[tuple[int, int]] = field(default_factory=lambda: [(2222, 22)])
    ttl: int = 1800
    code: str = ""


def _parse_port(value: str, what: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise ParseError(f"{what} must be an integer, got {value!r}") from None
    if not 1 <= port <= 65535:
        raise ParseError(f"{what} out of range: {port}")
    return port


def _parse_forwards(value: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 2:
            raise ParseError(f"bad forward {chunk!r}, expected 'remote:local'")
        result.append(
            (
                _parse_port(parts[0].strip(), "remote port"),
                _parse_port(parts[1].strip(), "local port"),
            )
        )
    if not result:
        raise ParseError("no valid forwards given")
    if len(result) > MAX_FORWARDS:
        raise ParseError(f"too many forwards (max {MAX_FORWARDS})")
    if len({r for r, _ in result}) != len(result):
        raise ParseError("duplicate remote ports in forwards")
    return result


def parse_command(body: str, ttl_default: int = 1800) -> Command:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1).lower()
        if key not in fields:
            fields[key] = m.group(2)

    host = fields.get("host", "")
    if not host:
        raise ParseError("missing required field 'host'")
    if len(host) > 253 or "/" in host or " " in host:
        raise ParseError(f"bad host {host!r}")

    ssh_user = fields.get("user", "")
    if not ssh_user:
        raise ParseError("missing required field 'user'")

    ssh_password = fields.get("password", "")
    if not ssh_password:
        raise ParseError("missing required field 'password'")

    port = _parse_port(fields.get("port", "22"), "port")

    forwards = (
        _parse_forwards(fields["forwards"])
        if fields.get("forwards")
        else [(2222, 22)]
    )

    ttl = ttl_default
    if fields.get("ttl"):
        try:
            ttl = int(fields["ttl"])
        except ValueError:
            raise ParseError(f"ttl must be an integer, got {fields['ttl']!r}") from None
        if not MIN_TTL <= ttl <= MAX_TTL:
            raise ParseError(f"ttl out of range [{MIN_TTL}..{MAX_TTL}]: {ttl}")

    return Command(
        host=host,
        ssh_user=ssh_user,
        ssh_password=ssh_password,
        port=port,
        forwards=forwards,
        ttl=ttl,
        code=fields.get("code", ""),
    )
