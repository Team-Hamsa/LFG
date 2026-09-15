import hashlib

import pytest
from cryptography.fernet import Fernet
from xrpl.models.transactions import EscrowFinish

from lfg_core import config
from lfg_core import crypto_condition as cc

# The canonical empty-preimage vector from the XRPL escrow docs.
EMPTY_CONDITION = "A0258020E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855810100"
EMPTY_FULFILLMENT = "A0028000"


def test_empty_preimage_matches_published_vector():
    assert cc.condition_hex(b"") == EMPTY_CONDITION
    assert cc.fulfillment_hex(b"") == EMPTY_FULFILLMENT


def test_32_byte_preimage_shape():
    pre = bytes(range(32))
    digest = hashlib.sha256(pre).hexdigest().upper()
    assert cc.condition_hex(pre) == "A0258020" + digest + "810120"
    assert cc.fulfillment_hex(pre) == "A0228020" + pre.hex().upper()


def test_new_preimage_is_32_random_bytes():
    a, b = cc.new_preimage(), cc.new_preimage()
    assert len(a) == 32 and a != b


def test_escrow_finish_model_accepts_the_pair():
    pre = cc.new_preimage()
    EscrowFinish(
        account="rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        owner="rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        offer_sequence=7,
        condition=cc.condition_hex(pre),
        fulfillment=cc.fulfillment_hex(pre),
    ).validate()


def test_seal_roundtrip(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())
    blob = cc.seal("A0228020" + "00" * 32)
    assert "A022" not in blob
    assert cc.unseal(blob) == "A0228020" + "00" * 32


def test_unseal_without_key_raises(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", "")
    with pytest.raises(cc.ConditionKeyError):
        cc.unseal("x")


def test_unseal_with_wrong_key_raises(monkeypatch):
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())
    blob = cc.seal("AA")
    monkeypatch.setattr(config, "CLOSET_MARKET_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(cc.ConditionKeyError):
        cc.unseal(blob)
