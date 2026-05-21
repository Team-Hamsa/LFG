import secrets

from vanity import encoding


ZERO_SEED_16 = bytes(16)
ZERO_SEED_S = "sEdSJHS4oiAdz7w2X2ni1gFiqtbJHqE"

FF_SEED_16 = b"\xff" * 16
FF_SEED_S = "sEdV19BLfeQeKdEXyYA4NhjPJe6XBfG"


def test_b58encode_empty():
    assert encoding.b58encode(b"") == ""


def test_b58encode_single_zero():
    assert encoding.b58encode(b"\x00") == "r"


def test_b58encode_single_one():
    assert encoding.b58encode(b"\x01") == "p"


def test_family_seed_encode_zero():
    assert encoding.family_seed_encode(ZERO_SEED_16) == ZERO_SEED_S


def test_family_seed_encode_ff():
    assert encoding.family_seed_encode(FF_SEED_16) == FF_SEED_S


def test_family_seed_encode_roundtrip_via_xrplpy():
    from xrpl.core.addresscodec import encode_seed
    from xrpl.constants import CryptoAlgorithm
    seed = secrets.token_bytes(16)
    assert encoding.family_seed_encode(seed) == encode_seed(seed, CryptoAlgorithm.ED25519)


def test_address_encode_matches_xrplpy():
    from xrpl.core.addresscodec import encode_classic_address
    account_id = secrets.token_bytes(20)
    assert encoding.address_encode(account_id) == encode_classic_address(account_id)
