"""CPU sieve: pubkey -> address -> prefix match."""
import hashlib
from typing import NamedTuple

from Crypto.Hash import RIPEMD160

from vanity import encoding

PUBKEY_LEN = 33
SEED_LEN = 16


class Match(NamedTuple):
    seed_b58: str
    address: str
    attempt: int


def _ripemd160(data: bytes) -> bytes:
    h = RIPEMD160.new()
    h.update(data)
    return h.digest()


def address_from_pubkey(pubkey33: bytes) -> str:
    """Compute the XRPL classic address from a 33-byte Ed25519 pubkey."""
    if len(pubkey33) != PUBKEY_LEN:
        raise ValueError(f"pubkey must be 33 bytes, got {len(pubkey33)}")
    sha = hashlib.sha256(pubkey33).digest()
    account_id = _ripemd160(sha)
    return encoding.address_encode(account_id)


def match(address: str, pattern: str, case_sensitive: bool) -> bool:
    """Check whether address[1:1+len(pattern)] equals pattern."""
    if len(address) < 1 + len(pattern):
        return False
    region = address[1 : 1 + len(pattern)]
    if case_sensitive:
        return region == pattern
    return region.lower() == pattern.lower()


def sieve_batch(
    pubkeys: bytes,
    seeds: bytes,
    pattern: str,
    case_sensitive: bool,
    first_attempt_index: int,
) -> list[Match]:
    """Sweep a batch and return all matches.

    pubkeys is B*33 bytes; seeds is B*16 bytes.
    """
    if len(pubkeys) % PUBKEY_LEN != 0:
        raise ValueError("pubkeys length must be a multiple of 33")
    if len(seeds) % SEED_LEN != 0:
        raise ValueError("seeds length must be a multiple of 16")
    b_pub = len(pubkeys) // PUBKEY_LEN
    b_seed = len(seeds) // SEED_LEN
    if b_pub != b_seed:
        raise ValueError(f"pubkey batch size {b_pub} != seed batch size {b_seed}")

    hits: list[Match] = []
    needle = pattern if case_sensitive else pattern.lower()
    n = len(pattern)

    for i in range(b_pub):
        pub = pubkeys[i * PUBKEY_LEN : (i + 1) * PUBKEY_LEN]
        addr = address_from_pubkey(pub)
        region = addr[1 : 1 + n]
        if not case_sensitive:
            region = region.lower()
        if region == needle:
            seed = seeds[i * SEED_LEN : (i + 1) * SEED_LEN]
            hits.append(
                Match(
                    seed_b58=encoding.family_seed_encode(seed),
                    address=addr,
                    attempt=first_attempt_index + i,
                )
            )
    return hits
