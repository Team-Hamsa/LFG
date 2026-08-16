# Task PR1-T2 (#41 X integration): MintSession.to_dict() must carry the
# minted edition's `traits` (LFG-naming dict, e.g. Head -> Hat) and
# `body_type` so the later X poster can compose tweet copy (and rank the
# rarest slot, which is body-scoped) without a second DB lookup. Additive
# only: pre-fulfillment sessions emit the same None-safe shape image_url
# already uses, and every existing to_dict() key is unchanged.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 8-16):
# importing lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
# LAYER_SOURCE) at import time; set the same defaults test_smoke.py uses so
# collection order can't strand them.
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

from lfg_core import mint_flow, swap_meta  # noqa: E402


def test_to_dict_traits_and_body_type_none_before_fulfillment():
    """A freshly-created (pre-payment) session has no minted traits yet --
    to_dict() must emit None for both new keys, matching the existing
    None-handling style of image_url/nft_id, and every pre-existing key must
    still be present and correct."""
    session = mint_flow.MintSession(discord_id="1", wallet_address="rUser")
    d = session.to_dict()

    assert d["traits"] is None
    assert d["body_type"] is None
    # Existing keys unaffected by the additive change.
    assert d["platform"] == "discord"
    assert d["state"] == mint_flow.AWAITING_PAYMENT
    assert d["image_url"] is None
    assert d["nft_id"] is None
    assert d["nft_number"] is None


def test_run_mint_session_populates_traits_and_body_type(monkeypatch):
    """End-to-end (mocked network/CDN/XRPL boundaries, same pattern as
    tests/test_mint_cdn_paths.py): once a mint completes, session.to_dict()
    carries the LFG-naming traits dict (Head -> Hat, per lfg_core/rarity.py's
    documented mapping) and the body_type used to pick them."""

    async def fake_wait_for_payment(**kwargs):
        return True

    async def fake_buy_and_burn(*a, **k):
        return "BURNHASH"

    async def fake_allocate():
        return 4100

    async def fake_select(store):
        return "milady", [
            {"trait_type": "Head", "value": "Wizard Hat"},
            {"trait_type": "Body", "value": "Blue"},
        ]

    async def fake_compose(attributes, body, store, basename):
        return "/tmp/out.png", False

    async def fake_upload_output(path, is_video, upload_fn, basename, keep_still=None):
        return "https://cdn.example/4100/4100_0.png", None

    async def fake_upload_bunny(name, data, ctype):
        return f"https://cdn.example/{name}"

    async def fake_mint_nft(**kwargs):
        return "NFTID100"

    async def fake_create_nft_offer(*args, **kwargs):
        return "OFFER100"

    async def fake_create_accept_offer_payload(*args, **kwargs):
        return {"qr_url": "q", "xumm_url": "x", "uuid": "u"}

    def fake_record_nft_mint(**kwargs):
        return True

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", fake_wait_for_payment)
    monkeypatch.setattr(mint_flow.xrpl_ops, "buy_and_burn", fake_buy_and_burn)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", fake_allocate)
    monkeypatch.setattr(mint_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", fake_select)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow.swap_compose, "upload_output", fake_upload_output)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload_bunny)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", fake_create_nft_offer)
    monkeypatch.setattr(
        mint_flow.xumm_ops, "create_accept_offer_payload", fake_create_accept_offer_payload
    )
    monkeypatch.setattr(mint_flow, "record_nft_mint", fake_record_nft_mint)
    monkeypatch.setattr(mint_flow.image_archive, "promote_still", lambda *a, **k: None)
    monkeypatch.setattr(mint_flow.image_archive, "discard_still", lambda *a, **k: None)
    monkeypatch.setattr(
        mint_flow.image_archive, "pending_still_path", lambda *a, **k: "/tmp/pending.png"
    )

    session = mint_flow.MintSession(discord_id="1", wallet_address="rUser")
    session.payment_uuid = "PAYUUID"  # #262: a real XUMM payload exists
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mint_flow.run_mint_session(session))
    finally:
        loop.close()

    assert session.state == mint_flow.OFFER_READY
    d = session.to_dict()
    # Head -> Hat: the LFG table's headwear column is named Hat even though
    # the layer tree / metadata attributes use Head (lfg_core/rarity.py).
    # #268: the normalize step 'None'-fills every remaining canonical slot.
    expected = {t: "None" for t in swap_meta.TRAIT_ORDER if t != "Head"}
    expected.update({"Hat": "Wizard Hat", "Body": "Blue"})
    assert d["traits"] == expected
    assert d["body_type"] == "milady"
    # Existing keys still correct -- additive change, no regressions.
    assert d["nft_id"] == "NFTID100"
    assert d["image_url"] == "https://cdn.example/4100/4100_0.png"
    assert d["platform"] == "discord"


def test_run_mint_session_fails_fast_without_payment_uuid(monkeypatch):
    """#262 defense-in-depth: a session whose XUMM payment payload was never
    created (429 backoff / outage — payment_uuid None, only the static detect
    link set) must fail immediately instead of entering the 300s payment wait
    the user cannot possibly satisfy (Xaman cannot parse the detect link as a
    sign request — the prod incident's 5-minute dead screen)."""

    async def must_not_wait(**kwargs):
        raise AssertionError("wait_for_payment must not be entered without a sign request")

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", must_not_wait)

    session = mint_flow.MintSession(discord_id="1", wallet_address="rUser")
    assert session.payment_uuid is None
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mint_flow.run_mint_session(session))
    finally:
        loop.close()

    assert session.state == mint_flow.FAILED
    assert session.error == "signing service is busy — please try again shortly"
    # ensure_payment_fallback still ran first: the pay_with defaulting is
    # required by _payment_params for any path that legitimately has a payload.
    assert session.pay_with == "XRP"
    assert session.payment_link  # static link kept for the error screen
