import pytest

from ondemandportforward import gpg
from ondemandportforward.validator import Validator

SECRET = "test-secret"

GOOD_BODY = "host: 1.2.3.4\nuser: root\npassword: pw\nforwards: 2222:22\nttl: 300\n"


def _encrypted(body: str, secret: str = SECRET) -> str:
    return gpg.encrypt(body, secret)


async def test_encrypted_accepted(cfg, store):
    v = Validator(cfg, store)
    verdict = await v.check(_encrypted(GOOD_BODY))
    assert verdict.accepted and verdict.mode == "encrypted"
    assert verdict.command.host == "1.2.3.4"
    assert verdict.command.ttl == 300


async def test_encrypted_wrong_secret(cfg, store):
    v = Validator(cfg, store)
    verdict = await v.check(_encrypted(GOOD_BODY, secret="other"))
    assert not verdict.accepted and verdict.reason == "decrypt_failed"


async def test_encrypted_replay(cfg, store):
    v = Validator(cfg, store)
    block = _encrypted(GOOD_BODY)
    assert (await v.check(block)).accepted
    verdict = await v.check(block)
    assert not verdict.accepted and verdict.reason == "replay"


async def test_any_sender_accepted(cfg, store):
    v = Validator(cfg, store)
    verdict = await v.check(_encrypted(GOOD_BODY))
    assert verdict.accepted


async def test_garbage_body(cfg, store):
    v = Validator(cfg, store)
    verdict = await v.check("hello there")
    assert not verdict.accepted and verdict.reason == "plaintext_disabled"


async def test_plaintext_flow(cfg, store):
    v = Validator(cfg, store)
    store.arm_code("one-time-1")
    body = GOOD_BODY + "code: one-time-1\n"
    verdict = await v.check(body)
    assert verdict.accepted and verdict.mode == "plaintext"
    # burned: exact resend
    verdict = await v.check(body)
    assert not verdict.accepted and verdict.reason == "code_used"
    # modified resend with same code
    verdict = await v.check(GOOD_BODY + "ttl: 400\ncode: one-time-1\n")
    assert not verdict.accepted and verdict.reason == "code_used"


async def test_plaintext_bad_code(cfg, store):
    v = Validator(cfg, store)
    store.arm_code("right")
    verdict = await v.check(GOOD_BODY + "code: wrong\n")
    assert not verdict.accepted and verdict.reason == "bad_code"


async def test_plaintext_missing_code(cfg, store):
    v = Validator(cfg, store)
    store.arm_code("right")
    verdict = await v.check(GOOD_BODY)
    assert not verdict.accepted and verdict.reason.startswith("bad_code")


async def test_plaintext_burns_before_tunnel(cfg, store):
    """Code must burn at acceptance, even if the payload is later unusable."""
    v = Validator(cfg, store)
    store.arm_code("abc")
    verdict = await v.check(
        "host: h\nuser: u\npassword: p\ncode: abc\nttl: 300\n"
    )
    assert verdict.accepted
    assert store.armed_code_hash() is None  # burned


async def test_strict_headers(cfg, store):
    cfg.strict_headers = True
    v = Validator(cfg, store)
    verdict = await v.check(
        _encrypted(GOOD_BODY),
        auth_results="example.com; spf=pass dkim=pass",
    )
    assert verdict.accepted
    verdict = await v.check(
        _encrypted(GOOD_BODY + "ttl: 301\n"),
        auth_results="example.com; spf=fail dkim=none",
    )
    assert not verdict.accepted and verdict.reason == "bad_headers"


@pytest.mark.parametrize(
    "junk", ["", "no pgp here at all", "-----BEGIN PGP MESSAGE-----\ntruncated"]
)
async def test_non_pgp_garbage_is_plaintext_path(cfg, store, junk):
    v = Validator(cfg, store)
    verdict = await v.check(junk)
    assert not verdict.accepted
