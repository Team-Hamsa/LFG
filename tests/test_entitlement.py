import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import pytest

from lfg_core import entitlement


def test_payment_entitlement_roundtrip():
    e = entitlement.PaymentEntitlement(quantity=5)
    assert e.source == "payment"
    assert e.cap_exempt is False
    assert entitlement.from_dict(e.to_dict()) == e


def test_burn_entitlement_is_cap_exempt_and_roundtrips():
    e = entitlement.BurnEntitlement(quantity=3, burn_nft_ids=["a", "b", "c"])
    assert e.source == "burn"
    assert e.cap_exempt is True
    assert entitlement.from_dict(e.to_dict()) == e


def test_build_burn_entitlement_is_stub():
    with pytest.raises(NotImplementedError):
        entitlement.build_burn_entitlement(quantity=1, burn_nft_ids=["a"])
