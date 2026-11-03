from __future__ import annotations

import tempfile
from pathlib import Path

import gnupg

BEGIN = "-----BEGIN PGP MESSAGE-----"
END = "-----END PGP MESSAGE-----"


class GpgError(Exception):
    pass


def extract_pgp_block(text: str) -> str | None:
    """Return the first armored PGP MESSAGE block found in text, if any."""
    i = text.find(BEGIN)
    if i == -1:
        return None
    j = text.find(END, i)
    if j == -1:
        return None
    return text[i : j + len(END)].strip()


def _make_gpg(home: Path) -> gnupg.GPG:
    home.mkdir(parents=True, exist_ok=True)
    return gnupg.GPG(gnupghome=str(home))


def decrypt(block: str, passphrase: str) -> str:
    """Decrypt an armored symmetric PGP message; raise GpgError on failure."""
    with tempfile.TemporaryDirectory(prefix="ondemandportforward-gpg-") as tmp:
        gpg = _make_gpg(Path(tmp))
        result = gpg.decrypt(block, passphrase=passphrase)
        if not result.ok:
            raise GpgError(result.status or "decryption failed")
        return str(result)


def encrypt(plaintext: str, passphrase: str) -> str:
    """Produce an armored symmetric AES256 PGP message (sender-side helper)."""
    with tempfile.TemporaryDirectory(prefix="ondemandportforward-gpg-") as tmp:
        gpg = _make_gpg(Path(tmp))
        result = gpg.encrypt(
            plaintext,
            recipients=None,
            symmetric=True,
            passphrase=passphrase,
            armor=True,
            extra_args=["--cipher-algo", "AES256"],
        )
        if not result.ok:
            raise GpgError(result.status or "encryption failed")
        return str(result)
