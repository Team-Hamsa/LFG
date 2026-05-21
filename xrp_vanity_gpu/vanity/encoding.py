"""XRPL base58 + family seed + address encoders."""
import hashlib

XRPL_ALPHABET = b"rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"
assert len(XRPL_ALPHABET) == 58

_ED25519_SEED_PREFIX = bytes([0x01, 0xE1, 0x4B])
_ACCOUNT_ID_PREFIX = bytes([0x00])


def b58encode(data: bytes) -> str:
    """Encode raw bytes to XRPL-alphabet base58 (no checksum)."""
    n_leading_zeros = 0
    for b in data:
        if b == 0:
            n_leading_zeros += 1
        else:
            break

    n = int.from_bytes(data, "big")
    digits = []
    while n > 0:
        n, r = divmod(n, 58)
        digits.append(XRPL_ALPHABET[r])
    digits.reverse()

    return (chr(XRPL_ALPHABET[0]) * n_leading_zeros) + bytes(digits).decode("ascii")


def _double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def family_seed_encode(seed16: bytes) -> str:
    """Encode a 16-byte Ed25519 family seed as 'sEd...'."""
    if len(seed16) != 16:
        raise ValueError(f"seed must be 16 bytes, got {len(seed16)}")
    payload = _ED25519_SEED_PREFIX + seed16
    checksum = _double_sha256(payload)[:4]
    return b58encode(payload + checksum)


def address_encode(account_id20: bytes) -> str:
    """Encode a 20-byte account_id as 'r...'."""
    if len(account_id20) != 20:
        raise ValueError(f"account_id must be 20 bytes, got {len(account_id20)}")
    payload = _ACCOUNT_ID_PREFIX + account_id20
    checksum = _double_sha256(payload)[:4]
    return b58encode(payload + checksum)
