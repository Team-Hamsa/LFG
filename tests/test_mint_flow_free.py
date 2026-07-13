import asyncio
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

import lfg_core.free_mint as free_mint  # noqa: E402
import lfg_core.mint_flow as mint_flow  # noqa: E402


def _run(coro):
    # A dedicated loop (not asyncio.run) so we never call set_event_loop(None),
    # which would poison later tests that use the deprecated get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _session(monkeypatch, eligible, reserve=True):
    monkeypatch.setattr(free_mint, "is_eligible", lambda *a, **k: eligible)
    monkeypatch.setattr(free_mint, "reserve_claim", lambda *a, **k: reserve)
    s = mint_flow.MintSession(
        discord_id="u1", wallet_address="rA", platform="discord", network="testnet"
    )
    return s


def test_eligible_session_goes_free_and_skips_payment(monkeypatch):
    called = {"payload": False}

    async def _fake_payload(*a, **k):
        called["payload"] = True
        return None

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", _fake_payload)
    # LFGO balance check must not decide the path when free
    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_balance", lambda *a, **k: None)
    s = _session(monkeypatch, eligible=True)
    _run(s.prepare_payment())
    assert s.free is True
    assert s.to_dict()["free"] is True
    assert called["payload"] is False  # no XUMM payment payload built


def test_ineligible_session_uses_paid_path(monkeypatch):
    async def _bal(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_balance", _bal)

    async def _payload(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", _payload)
    s = _session(monkeypatch, eligible=False)
    _run(s.prepare_payment())
    assert s.free is False
    assert s.pay_with == "XRP"


def test_lost_reserve_race_falls_back_to_paid(monkeypatch):
    async def _bal(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_balance", _bal)

    async def _payload(*a, **k):
        return None

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", _payload)
    s = _session(monkeypatch, eligible=True, reserve=False)  # lost the race
    _run(s.prepare_payment())
    assert s.free is False
    assert s.pay_with == "XRP"
