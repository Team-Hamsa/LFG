# tests/test_telegram_initdata.py
# Unit tests for the Telegram Mini App initData HMAC validator (#89, Part A).
#
# A self-signing helper builds a VALID initData string the same way Telegram
# would (sorted data-check-string, the "WebAppData"-keyed HMAC). The validator
# and the signer must therefore agree on the exact algorithm — that mutual
# agreement is what these vectors pin.
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from lfg_service.telegram_auth import validate_init_data

DUMMY_TOKEN = "123456:TEST-FAKE-TOKEN"


def _sign(fields: dict, bot_token: str) -> str:
    """Produce a valid initData query string for `fields` signed with `bot_token`.

    Mirrors Telegram's scheme: data-check-string = sorted "key=value" lines
    joined by \\n; secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token);
    hash = hex(HMAC_SHA256(key=secret_key, msg=dcs)).
    """
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _valid_fields(now: int, **extra) -> dict:
    base = {
        "auth_date": str(now),
        "query_id": "AAEUR",
        "user": json.dumps({"id": 55, "username": "alice"}),
    }
    base.update(extra)
    return base


def test_valid_initdata_accepted():
    now = int(time.time())
    init_data = _sign(_valid_fields(now), DUMMY_TOKEN)
    result = validate_init_data(init_data, DUMMY_TOKEN, max_age=3600, now=now)
    assert result is not None
    assert result["user"]["id"] == 55
    assert result["user"]["username"] == "alice"


def test_tampered_hash_rejected():
    now = int(time.time())
    init_data = _sign(_valid_fields(now), DUMMY_TOKEN)
    # Flip the last char of the hash.
    last = "0" if init_data[-1] != "0" else "1"
    tampered = init_data[:-1] + last
    assert validate_init_data(tampered, DUMMY_TOKEN, max_age=3600, now=now) is None


def test_tampered_field_rejected():
    now = int(time.time())
    fields = _valid_fields(now)
    init_data = _sign(fields, DUMMY_TOKEN)
    # Mutate the user field AFTER signing — the dcs no longer matches the hash.
    tampered = init_data.replace(
        urlencode({"user": fields["user"]}),
        urlencode({"user": json.dumps({"id": 99, "username": "mallory"})}),
    )
    assert validate_init_data(tampered, DUMMY_TOKEN, max_age=3600, now=now) is None


def test_wrong_bot_token_rejected():
    now = int(time.time())
    init_data = _sign(_valid_fields(now), DUMMY_TOKEN)
    assert validate_init_data(init_data, "999999:OTHER-TOKEN", max_age=3600, now=now) is None


def test_stale_auth_date_rejected():
    now = int(time.time())
    init_data = _sign(_valid_fields(now - 7200), DUMMY_TOKEN)
    assert validate_init_data(init_data, DUMMY_TOKEN, max_age=3600, now=now) is None


def test_fresh_auth_date_accepted():
    now = int(time.time())
    init_data = _sign(_valid_fields(now - 60), DUMMY_TOKEN)
    assert validate_init_data(init_data, DUMMY_TOKEN, max_age=3600, now=now) is not None


def test_missing_hash_rejected():
    now = int(time.time())
    init_data = urlencode(_valid_fields(now))  # no hash field
    assert validate_init_data(init_data, DUMMY_TOKEN, max_age=3600, now=now) is None


def test_missing_user_handled():
    now = int(time.time())
    fields = {"auth_date": str(now), "query_id": "AAEUR"}  # no user
    init_data = _sign(fields, DUMMY_TOKEN)
    # Must not raise; a payload with no user is unusable for identity → None.
    assert validate_init_data(init_data, DUMMY_TOKEN, max_age=3600, now=now) is None


def test_empty_initdata_returns_none():
    assert validate_init_data("", DUMMY_TOKEN, max_age=3600, now=int(time.time())) is None


def test_signature_field_signed_and_stripped():
    # Current Telegram clients include the Ed25519 `signature` field IN the
    # HMAC data-check-string (verified against a live iOS launch, 2026-07-17).
    # A payload signed WITH signature in the dcs must validate, and the opaque
    # signature must be stripped from the returned fields.
    now = int(time.time())
    fields = _valid_fields(now, signature="opaque-ed25519-sig")
    init_data = _sign(fields, DUMMY_TOKEN)
    result = validate_init_data(init_data, DUMMY_TOKEN, max_age=3600, now=now)
    assert result is not None
    assert result["user"]["id"] == 55
    assert "signature" not in result


def test_signature_appended_outside_dcs_rejected():
    # A `signature` bolted on AFTER signing (i.e. not covered by the hash) must
    # be rejected — anything in the payload that the hash does not cover would
    # otherwise be attacker-controlled.
    now = int(time.time())
    init_data = _sign(_valid_fields(now), DUMMY_TOKEN)
    with_sig = init_data + "&" + urlencode({"signature": "junk-ed25519-sig"})
    assert validate_init_data(with_sig, DUMMY_TOKEN, max_age=3600, now=now) is None


def test_near_miss_hash_rejected():
    # Smoke for constant-time compare: a hash differing only in the last byte
    # is rejected (validator must compare the full digest, not a prefix).
    now = int(time.time())
    init_data = _sign(_valid_fields(now), DUMMY_TOKEN)
    # Recompute with the last hex nibble flipped.
    hash_idx = init_data.rfind("hash=") + len("hash=")
    good = init_data[hash_idx:]
    flipped = good[:-1] + ("a" if good[-1] != "a" else "b")
    tampered = init_data[:hash_idx] + flipped
    assert validate_init_data(tampered, DUMMY_TOKEN, max_age=3600, now=now) is None
