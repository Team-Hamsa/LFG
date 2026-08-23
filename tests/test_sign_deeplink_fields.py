# tests/test_sign_deeplink_fields.py
# #142 — every sign-request surface must carry a tappable Xaman deep link
# alongside the QR. This is the server-side contract half: the shared payload
# builder maps XUMM's `next.always` universal link to `xumm_url`, and every
# flow session's public dict exposes a deep-link field the surfaces can render
# ("Open in Xaman"). QR data stays for desktop cross-device scanning; the deep
# link is the mobile path.
#
# Env-guard preamble: importing lfg_core.config freezes its constants at import
# time; set the same defaults test_smoke.py uses so collection order can't
# strand them. (Copy the block verbatim from tests/test_market_ops.py.)
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

from lfg_core import market_flow, memos, mint_flow, shop_flow, swap_flow, xumm_ops  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _CreateResp:
    def json(self):
        return {
            "refs": {"qr_png": "https://xumm.app/sign/u_q.png"},
            "next": {"always": "https://xumm.app/sign/u"},
            "uuid": "u",
            "pushed": False,
        }


def test_post_xumm_payload_surfaces_deeplink(monkeypatch):
    """The shared builder must return the universal `next.always` link as
    xumm_url next to the QR — the deep link every surface renders."""

    def fake_post(url, json, headers, timeout):
        return _CreateResp()

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    # A real (non-SignIn) transaction must carry provenance memos (#54/#399) —
    # every production builder supplies them, and _create_xumm_payload now
    # refuses one that does not.
    result = _run(
        xumm_ops._create_xumm_payload(
            {"TransactionType": "Payment"},
            memos_json=memos.build_memos_json(
                memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_PAYMENT
            ),
        )
    )
    assert result is not None
    assert result["xumm_url"] == "https://xumm.app/sign/u"
    assert result["qr_url"]  # QR stays alongside, never replaced


def test_mint_session_dict_carries_deeplinks():
    s = mint_flow.MintSession("d1", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
    d = s.to_dict()
    assert "payment_link" in d
    assert "accept_deeplink" in d


def test_swap_session_dict_carries_payment_deeplink():
    nft = {"name": "LFGO #1", "image": "https://cdn.example/1.png"}
    s = swap_flow.SwapSession("d1", "rrrrrrrrrrrrrrrrrrrrrhoLvTp", nft, dict(nft), ["Hat"])
    assert "payment_link" in s.to_dict()


def test_market_sessions_dicts_carry_deeplink():
    wallet = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"
    sessions = [
        market_flow.ListSession("d1", wallet, "A" * 64, "character", amount_drops=1),
        market_flow.CancelSession("d1", wallet, "B" * 64, "testnet"),
        market_flow.BuySession("d1", wallet, "B" * 64, "A" * 64, "character", "testnet"),
    ]
    for s in sessions:
        assert "xumm_url" in s.to_dict(), type(s).__name__


def test_trait_sell_session_dict_carries_both_deeplinks():
    s = market_flow.TraitSellSession(
        "d1", "rrrrrrrrrrrrrrrrrrrrrhoLvTp", "Hat", "Wizard Hat", "5", extract_session=None
    )
    d = s.to_dict()
    assert "extract_xumm_url" in d
    assert "list_xumm_url" in d


def test_shop_session_dict_carries_accept_payload():
    s = shop_flow.ShopBuySession(
        buyer="rrrrrrrrrrrrrrrrrrrrrhoLvTp", slot="Hat", value="Wizard Hat", price_brix=5
    )
    d = s.to_dict()
    # The accept payload dict (qr_url/xumm_url/push) rides whole; the client
    # reads s.accept.xumm_url as the deep link.
    assert "accept" in d
