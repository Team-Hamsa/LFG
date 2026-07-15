# Env guard: set before lfg_core imports so frozen config constants are sane
# when this file runs first (see test-env-guard convention).
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from lfg_core import bulk_mint_flow, config  # noqa: E402


def test_config_defaults():
    assert config.MAX_COLLECTION_SIZE == 10000
    assert config.BULK_MINT_MAX == 10


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def _job(qty):
    return bulk_mint_flow.BulkMintJob(
        discord_id="u1", wallet_address="rUSER", requested_qty=qty, platform="discord"
    )


def test_clamp_within_headroom_keeps_quantity(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(5)
    j.clamp_to_headroom()
    assert j.quantity == 5
    assert len(j.units) == 5


def test_clamp_respects_bulk_max(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(50)
    j.clamp_to_headroom()
    assert j.quantity == 10


def test_clamp_to_headroom_when_low(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 3)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(8)
    j.clamp_to_headroom()
    assert j.quantity == 3


def test_clamp_collection_full_raises(monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 0)
    j = _job(5)
    with pytest.raises(bulk_mint_flow.CollectionFull):
        j.clamp_to_headroom()


def test_prepare_payment_multiplies_price_xrp(monkeypatch):
    import asyncio

    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    monkeypatch.setattr(config, "MINT_PRICE_XRP", "10")
    monkeypatch.setattr(
        bulk_mint_flow.xrpl_ops, "get_trustline_balance", _async_return(None)
    )  # no LFGO -> XRP path
    monkeypatch.setattr(
        bulk_mint_flow.xumm_ops,
        "create_payment_payload",
        _async_return({"xumm_url": "x", "uuid": "u"}),
    )
    j = _job(4)
    j.clamp_to_headroom()
    asyncio.run(j.prepare_payment())
    assert j.pay_with == "XRP"
    assert j.pay_amount == "40"  # 4 x 10
