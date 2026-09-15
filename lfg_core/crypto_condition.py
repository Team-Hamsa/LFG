"""PREIMAGE-SHA-256 crypto-conditions for Closet Market bid escrows (#443).

An XRPL escrow created with a Condition can only be finished by a tx carrying
the matching Fulfillment. The backend generates the preimage per bid, hands
the Condition to the bidder's EscrowCreate, and keeps the Fulfillment sealed
(Fernet, CLOSET_MARKET_ENC_KEY) so only it can finish the escrow.

Encoding (RFC draft-thomas-crypto-conditions, DER, short-form lengths only):
  condition   = A0 len { 80 20 sha256(preimage)  81 len cost }   cost = len(preimage)
  fulfillment = A0 len { 80 len preimage }
"""

from __future__ import annotations

import binascii
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

from lfg_core import config

PREIMAGE_BYTES = 32


class ConditionKeyError(RuntimeError):
    """CLOSET_MARKET_ENC_KEY is missing or cannot open this blob."""


def new_preimage() -> bytes:
    return secrets.token_bytes(PREIMAGE_BYTES)


def _der_len(n: int) -> bytes:
    if n >= 128:
        raise ValueError("long-form DER lengths are not needed for PREIMAGE-SHA-256")
    return bytes([n])


def _uint(n: int) -> bytes:
    raw = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    return b"\x00" + raw if raw[0] & 0x80 else raw


def condition_hex(preimage: bytes) -> str:
    cost = _uint(len(preimage))
    body = b"\x80\x20" + hashlib.sha256(preimage).digest() + b"\x81" + _der_len(len(cost)) + cost
    return (b"\xa0" + _der_len(len(body)) + body).hex().upper()


def fulfillment_hex(preimage: bytes) -> str:
    body = b"\x80" + _der_len(len(preimage)) + preimage
    return (b"\xa0" + _der_len(len(body)) + body).hex().upper()


def _fernet() -> Fernet:
    key = config.CLOSET_MARKET_ENC_KEY
    if not key:
        raise ConditionKeyError("CLOSET_MARKET_ENC_KEY is not configured")
    try:
        return Fernet(key.encode())
    except (ValueError, binascii.Error) as exc:
        raise ConditionKeyError("CLOSET_MARKET_ENC_KEY is not a valid Fernet key") from exc


def seal(fulfillment: str) -> str:
    return _fernet().encrypt(fulfillment.encode()).decode()


def unseal(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, ValueError, TypeError, binascii.Error) as exc:
        raise ConditionKeyError("stored fulfillment cannot be decrypted") from exc
