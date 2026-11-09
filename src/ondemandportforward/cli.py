from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

from . import __version__
from .config import mask_secret
from .store import Store


def _store_for_tools() -> Store:
    data_dir = os.environ.get("DATA_DIR", "./data").strip() or "./data"
    return Store(data_dir)


def cmd_encrypt(args: argparse.Namespace) -> int:
    from . import gpg
    from .parser import parse_command

    print("Compose the command letter (Enter accepts [defaults]):")
    host = input("Host: ").strip()
    port = input("SSH port [22]: ").strip() or "22"
    user = input("SSH user: ").strip()
    password = getpass.getpass("SSH password: ")
    forwards = (
        input("Forwards remote:local or remote:host:local [2222:22]: ").strip()
        or "2222:22"
    )
    ttl = input("TTL seconds [1800]: ").strip() or "1800"

    body = (
        f"host: {host}\nport: {port}\nuser: {user}\n"
        f"password: {password}\nforwards: {forwards}\nttl: {ttl}\n"
    )
    # sanity check before encrypting
    parse_command(body)

    passphrase = getpass.getpass("GPG passphrase (SECRET): ")
    block = gpg.encrypt(body, passphrase)
    print("\nPaste this as the email body (plain text, not HTML):\n")
    print(block)
    return 0


def cmd_set_code(args: argparse.Namespace) -> int:
    store = _store_for_tools()
    if args.clear:
        n = store.clear_codes()
        print(f"cleared {n} code(s); plaintext mode is now DISABLED")
        return 0
    code = getpass.getpass("New one-time code: ")
    confirm = getpass.getpass("Confirm: ")
    if not code or code != confirm:
        print("codes do not match or empty, aborting", file=sys.stderr)
        return 1
    store.arm_code(code)
    print(f"armed new one-time code {mask_secret(code)}; previous codes are dead")
    print("include it as a 'code:' line in a plaintext letter; it burns on first use")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    store = _store_for_tools()
    attempts = store.recent_attempts(limit=1)
    print("ok: store reachable, schema present" + (", last attempt logged" if attempts else ""))
    store.close()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .main import run_daemon

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_daemon())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ondemandportforward", description="on-demand port forwarding via email"
    )
    p.add_argument("--version", action="version", version=f"ondemandportforward {__version__}")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("daemon", help="watch the mailbox (default)")
    sub.add_parser("encrypt", help="compose and encrypt a command letter")
    sp = sub.add_parser("set-code", help="arm a new one-time plaintext code")
    sp.add_argument("--clear", action="store_true", help="disable plaintext mode")
    sub.add_parser("health", help="check the state store")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else None)
    if args.command in (None, "daemon"):
        return cmd_daemon(args)
    return {
        "encrypt": cmd_encrypt,
        "set-code": cmd_set_code,
        "health": cmd_health,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
