import pytest

from ondemandportforward import gpg


def test_roundtrip():
    block = gpg.encrypt("host: 1.2.3.4\n", "pass123")
    assert block.startswith("-----BEGIN PGP MESSAGE-----")
    assert gpg.decrypt(block, "pass123") == "host: 1.2.3.4\n"


def test_wrong_passphrase():
    block = gpg.encrypt("secret body", "right")
    with pytest.raises(gpg.GpgError):
        gpg.decrypt(block, "wrong")


def test_each_encryption_differs():
    a = gpg.encrypt("same text", "p")
    b = gpg.encrypt("same text", "p")
    assert a != b


def test_extract_block():
    text = f"noise\n{gpg.encrypt('x', 'p')}\ntrailing"
    block = gpg.extract_pgp_block(text)
    assert block is not None and block.startswith("-----BEGIN PGP MESSAGE-----")
    assert gpg.extract_pgp_block("no block") is None
    assert gpg.extract_pgp_block("-----BEGIN PGP MESSAGE-----\nno end") is None


def test_tampered_ciphertext_fails():
    block = gpg.encrypt("data", "p")
    lines = block.splitlines()
    mid = len(lines) // 2
    lines[mid] = lines[mid][:-2] + ("AA" if not lines[mid].endswith("AA") else "BB")
    with pytest.raises(gpg.GpgError):
        gpg.decrypt("\n".join(lines), "p")
