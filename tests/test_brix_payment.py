# tests/test_brix_payment.py — shared BRIX-vs-XRP payment-path detection
# (#238), extracted from swap_flow.detect_swap_payment so the Trait Shop can
# reuse it. All-fake xrpl_ops, no network.
#
# Env-guard preamble (verbatim from tests/test_shop_flow.py): importing
# lfg_core.config freezes its constants at import time; set the same defaults
# test_smoke.py uses so collection order can't strand them.
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

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from lfg_core import brix_payment, config, swap_flow  # noqa: E402

WALLET = "rWALLET111111111111111111111"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_balance(value):
    async def fake(address, currency, issuer):
        fake.calls.append((address, currency, issuer))
        return value

    fake.calls = []
    return fake


def _fake_amm(value):
    async def fake(currency, issuer, amount):
        fake.calls.append((currency, issuer, amount))
        return value

    fake.calls = []
    return fake


def test_brix_path_when_balance_sufficient(monkeypatch):
    bal = _fake_balance(Decimal("100"))
    amm = _fake_amm(Decimal("1"))
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", bal)
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_amm_xrp_cost", amm)

    result = _run(brix_payment.detect_payment_path(WALLET, "25"))

    assert result == ("BRIX", "25")
    assert bal.calls == [(WALLET, config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER)]
    assert amm.calls == []  # never quoted on the BRIX path


def test_brix_path_exact_balance(monkeypatch):
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", _fake_balance(Decimal(25)))
    result = _run(brix_payment.detect_payment_path(WALLET, "25"))
    assert result == ("BRIX", "25")


@pytest.mark.parametrize("balance", [None, Decimal("24.999")])
def test_xrp_path_applies_buffer_and_rounds_up(monkeypatch, balance):
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", _fake_balance(balance))
    amm = _fake_amm(Decimal("0.1"))
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_amm_xrp_cost", amm)

    pay_with, amount = _run(brix_payment.detect_payment_path(WALLET, "25", buffer="1.05"))

    assert pay_with == "XRP"
    assert amount == "0.105000"
    assert amm.calls == [(config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER, Decimal("25"))]


def test_xrp_path_rounds_up_not_half_even(monkeypatch):
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", _fake_balance(None))
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_amm_xrp_cost", _fake_amm(Decimal("0.0000001")))
    pay_with, amount = _run(brix_payment.detect_payment_path(WALLET, "1", buffer="1.0"))
    assert pay_with == "XRP"
    assert amount == "0.000001"  # ROUND_UP: never quantizes a nonzero cost to zero


def test_quote_none_raises(monkeypatch):
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", _fake_balance(None))
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_amm_xrp_cost", _fake_amm(None))
    with pytest.raises(RuntimeError):
        _run(brix_payment.detect_payment_path(WALLET, "25"))


def test_currency_issuer_overrides(monkeypatch):
    bal = _fake_balance(Decimal("100"))
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", bal)
    _run(brix_payment.detect_payment_path(WALLET, "5", currency="ABC", issuer="rISSUER"))
    assert bal.calls == [(WALLET, "ABC", "rISSUER")]


def test_swap_wrapper_delegates(monkeypatch):
    """swap_flow.detect_swap_payment must be a thin wrapper over the shared
    helper with identical behavior (config currency/issuer/buffer)."""
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", _fake_balance(None))
    monkeypatch.setattr(brix_payment.xrpl_ops, "get_amm_xrp_cost", _fake_amm(Decimal("2")))
    expected_xrp = str(
        (Decimal("2") * Decimal(config.SWAP_XRP_FEE_BUFFER)).quantize(Decimal("0.000001"))
    )
    assert _run(swap_flow.detect_swap_payment(WALLET, "10")) == ("XRP", expected_xrp)

    monkeypatch.setattr(brix_payment.xrpl_ops, "get_trustline_balance", _fake_balance(Decimal(10)))
    assert _run(swap_flow.detect_swap_payment(WALLET, "10")) == ("BRIX", "10")
