import json
import pathlib
import secrets

import pytest

from vanity import encoding, sieve


def _xrplpy_pubkey_and_addr(seed16: bytes) -> tuple[bytes, str]:
    """Oracle: returns (33B pubkey bytes, classic address) for a seed."""
    from xrpl.core.keypairs import derive_keypair, derive_classic_address
    s = encoding.family_seed_encode(seed16)
    pub_hex, _ = derive_keypair(s)
    addr = derive_classic_address(pub_hex)
    return bytes.fromhex(pub_hex), addr


def test_address_from_pubkey_matches_xrplpy():
    seed = secrets.token_bytes(16)
    pub_bytes, expected_addr = _xrplpy_pubkey_and_addr(seed)
    assert sieve.address_from_pubkey(pub_bytes) == expected_addr


def test_match_prefix_case_insensitive_hits():
    addr = "rDaimyoFooBar"
    assert sieve.match(addr, "daimyo", case_sensitive=False) is True
    assert sieve.match(addr, "DAIMYO", case_sensitive=False) is True
    assert sieve.match(addr, "Daimyo", case_sensitive=False) is True


def test_match_prefix_case_insensitive_misses():
    addr = "rXyzDaimyo"
    assert sieve.match(addr, "daimyo", case_sensitive=False) is False


def test_match_prefix_case_sensitive():
    addr = "rDaimyoFooBar"
    assert sieve.match(addr, "Daimyo", case_sensitive=True) is True
    assert sieve.match(addr, "daimyo", case_sensitive=True) is False


def test_sieve_batch_finds_known_hit():
    target_seed = target_pub = target_addr = None
    for _ in range(200):
        seed = secrets.token_bytes(16)
        pub, addr = _xrplpy_pubkey_and_addr(seed)
        if addr[1] == "D":
            target_seed, target_pub, target_addr = seed, pub, addr
            break
    assert target_seed is not None, "Could not find an 'rD...' address in 200 tries"

    other_seed = secrets.token_bytes(16)
    other_pub, other_addr = _xrplpy_pubkey_and_addr(other_seed)
    while other_addr[1] == "D":
        other_seed = secrets.token_bytes(16)
        other_pub, other_addr = _xrplpy_pubkey_and_addr(other_seed)

    pubkeys = target_pub + other_pub
    seeds = target_seed + other_seed
    hits = sieve.sieve_batch(
        pubkeys=pubkeys,
        seeds=seeds,
        pattern="D",
        case_sensitive=True,
        first_attempt_index=0,
    )
    assert len(hits) == 1
    assert hits[0].address == target_addr
    assert hits[0].seed_b58 == encoding.family_seed_encode(target_seed)


def test_parallel_sieve_matches_serial():
    pubkeys = secrets.token_bytes(2000 * 33)
    seeds = secrets.token_bytes(2000 * 16)
    serial = sieve.sieve_batch(pubkeys, seeds, "r", False, 0)
    ps = sieve.ParallelSieve(n_workers=4)
    try:
        parallel = ps.sieve_batch(pubkeys, seeds, "r", False, 0)
    finally:
        ps.close()
    assert sorted(parallel) == sorted(serial)


def test_vector_file_loads():
    p = pathlib.Path(__file__).parent / "data" / "ed25519_vectors.json"
    if not p.exists():
        pytest.skip("ed25519_vectors.json not generated yet (Task 5)")
    vecs = json.loads(p.read_text())
    assert len(vecs) == 1000
    assert len(bytes.fromhex(vecs[0]["seed_hex"])) == 16
    assert len(bytes.fromhex(vecs[0]["point_hex"])) == 32
