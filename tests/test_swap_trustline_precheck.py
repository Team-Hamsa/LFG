# tests/test_swap_trustline_precheck.py
# #166: a burn-remint swap whose fee path is BRIX prices its replacement
# offers as IOU amounts (swap_offer_amount) — under XLS-20 an IOU-priced
# NFTokenCreateOffer on a TransferFee token requires the NFT ISSUER to hold a
# trustline for that IOU (the royalty pays out in it). On mainnet the NFT
# issuer and the BRIX issuer are separate accounts, so a missing issuer
# trustline turned into tecNO_LINE *after* the originals were burned.
# The fix: precheck the issuer trustline BEFORE any destructive step and
# gracefully fall back to the (trustline-safe) XRP fee path, logging a LOUD
# ops-facing error.
#
# Env-guard preamble: importing lfg_core.config freezes its constants at import
# time; set the same defaults test_smoke.py uses so collection order can't
# strand them. (Copy the block verbatim from tests/test_swap_offer_recovery.py.)
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
import sys  # noqa: E402
from decimal import Decimal  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from lfg_core import swap_flow, xrpl_ops  # noqa: E402


def _run(coro):
    # Repo convention (see tests/test_signing_account.py): a fresh loop that is
    # never set as the thread's current loop, so it doesn't strand loop state
    # for later tests the way asyncio.run() does.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _amock(return_value=None, calls=None):
    async def fn(*a, **k):
        if calls is not None:
            calls.append((a, k))
        return return_value

    return fn


class _Stop(Exception):
    """Sentinel raised right after the precheck to short-circuit the flow."""


def _nft(mutable: bool) -> dict:
    return {
        "name": "Let's Effing Go! #1",
        "number": 1,
        "nft_id": "00" * 32,
        "gender": "male",
        "mutable": mutable,
        "attributes": [],
    }


def _make_session(mutable1=False, mutable2=False) -> swap_flow.SwapSession:
    return swap_flow.SwapSession(
        discord_id="d",
        wallet_address="rUSERxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        nft1=_nft(mutable1),
        nft2=_nft(mutable2),
        traits_to_swap=["Accessory"],
    )


def _stub_flow_until_precheck(monkeypatch, *, pay_with="BRIX", total="20"):
    """Stub everything run_swap_session touches up to (and past) the precheck,
    stopping the flow at compose/upload with the _Stop sentinel."""
    monkeypatch.setattr(swap_flow.swap_meta, "swap_traits", lambda a1, a2, t: (a1, a2))
    monkeypatch.setattr(swap_flow.layer_store, "get_layer_store", lambda: None)
    monkeypatch.setattr(swap_flow.traits, "fill_missing_face_traits", _amock())
    monkeypatch.setattr(swap_flow.swap_compose, "missing_layers", _amock([]))
    monkeypatch.setattr(swap_flow, "detect_swap_payment", _amock((pay_with, total)))
    monkeypatch.setattr(xrpl_ops, "nft_exists", _amock(True))

    async def stop(*a, **k):
        raise _Stop("stopped at compose")

    monkeypatch.setattr(swap_flow, "_build_and_upload", stop)


def test_helper_true_when_issuer_has_trustline(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(Decimal("5")))
    assert _run(swap_flow._issuer_holds_offer_trustline()) is True


def test_helper_false_when_issuer_missing_trustline(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(None))
    assert _run(swap_flow._issuer_holds_offer_trustline()) is False


def test_issuer_has_trustline_keeps_brix(monkeypatch):
    _stub_flow_until_precheck(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(Decimal("5")))
    burns = []
    monkeypatch.setattr(xrpl_ops, "burn_nft", _amock("H", burns))

    s = _make_session()
    _run(swap_flow.run_swap_session(s))

    assert s.pay_with == "BRIX"
    assert s.fee_per_nft == Decimal("10")
    assert burns == []


def test_missing_trustline_falls_back_to_xrp(monkeypatch, caplog):
    _stub_flow_until_precheck(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(None))
    monkeypatch.setattr(xrpl_ops, "get_amm_xrp_cost", _amock(Decimal("0.5")))
    burns = []
    mints = []
    monkeypatch.setattr(xrpl_ops, "burn_nft", _amock("H", burns))
    monkeypatch.setattr(xrpl_ops, "mint_nft", _amock("ID", mints))

    s = _make_session()
    with caplog.at_level("ERROR"):
        _run(swap_flow.run_swap_session(s))

    # Flipped onto the trustline-safe XRP path before anything destructive.
    assert s.pay_with == "XRP"
    # 0.5 quote * 1.05 buffer = 0.525 total -> 0.2625 per NFT, 6dp ROUND_UP
    assert s.fee_per_nft == Decimal("0.262500")
    # The XRP offer amount is native drops (a str), not an IOU dict.
    assert isinstance(swap_flow._offer_amount(s), str)
    assert burns == [] and mints == []
    assert any("trustline" in r.message.lower() for r in caplog.records)


def test_missing_trustline_and_no_amm_quote_fails_pre_burn(monkeypatch):
    _stub_flow_until_precheck(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(None))
    monkeypatch.setattr(xrpl_ops, "get_amm_xrp_cost", _amock(None))
    burns = []
    mints = []
    monkeypatch.setattr(xrpl_ops, "burn_nft", _amock("H", burns))
    monkeypatch.setattr(xrpl_ops, "mint_nft", _amock("ID", mints))

    s = _make_session()
    _run(swap_flow.run_swap_session(s))

    assert s.state == swap_flow.FAILED
    assert s.error
    # Failed BEFORE compose (the _Stop sentinel was never raised) and before
    # any destructive step.
    assert "stopped at compose" not in (s.error or "")
    assert burns == [] and mints == []


def test_modify_only_session_skips_precheck(monkeypatch):
    _stub_flow_until_precheck(monkeypatch)
    calls = []
    monkeypatch.setattr(xrpl_ops, "get_trustline_balance", _amock(None, calls))

    s = _make_session(mutable1=True, mutable2=True)
    _run(swap_flow.run_swap_session(s))

    # No burn items -> no BRIX-priced offer -> no precheck, no fallback.
    assert s.pay_with == "BRIX"
    assert calls == []
