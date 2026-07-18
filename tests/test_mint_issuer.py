# tests/test_mint_issuer.py
# First mainnet mint failed 5/5 with tecNO_PERMISSION: run_mint_session passed
# issuer=config.TOKEN_ISSUER_ADDRESS (the LFGO *IOU* issuer) to mint_nft. On
# testnet the IOU issuer and the NFT collection issuer are the same account, so
# mint_nft's `if issuer != SIGNING_ACCOUNT` branch never added an Issuer field;
# on mainnet they differ (rBETMo… vs rLfgoMint…), turning every mint into an
# unauthorized mint-on-behalf. The collection issuer of record — what every
# other mint path (swap remint, economy) already uses — is SWAP_ISSUER_ADDRESS.
#
# Env-guard preamble (verbatim from tests/test_seasons.py lines 1-18): importing
# lfg_core.config freezes its constants (e.g. IMG_PROXY_ALLOWED_BASES,
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
from typing import Any  # noqa: E402

from lfg_core import config, mint_flow  # noqa: E402


def test_mint_session_mints_as_collection_issuer(monkeypatch):
    """The NFTokenMint must be issued by the NFT collection issuer
    (SWAP_ISSUER_ADDRESS), NOT the LFGO token issuer — on mainnet those are
    different accounts and the latter turns the tx into an unauthorized
    mint-on-behalf (tecNO_PERMISSION)."""
    captured: dict[str, Any] = {}

    async def fake_wait_for_payment(**kwargs):
        return True

    async def fake_mint_nft(**kwargs):
        captured.update(kwargs)
        return None  # stop the flow right after the mint step

    async def fake_allocate():
        return 4001

    async def fake_select(store):
        return "male", [{"trait_type": "Body", "value": "Straight"}]

    async def fake_compose(attributes, body, store, basename):
        return "/tmp/out.png", False

    async def fake_upload_output(path, is_video, upload_fn, basename, keep_still=None):
        return "https://cdn.example/x.png", None

    # Force the collection issuer and token issuer to visibly differ, like
    # the deployed mainnet env (rLfgoMint… vs rBETMo…).
    monkeypatch.setattr(config, "SWAP_ISSUER_ADDRESS", "rCOLLECTIONISSUERxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rTOKENISSUERxxxxxxxxxxxxxxxxxxxxxx")

    async def fake_buy_and_burn(*a, **k):
        return "BURNHASH"

    monkeypatch.setattr(mint_flow.xrpl_ops, "buy_and_burn", fake_buy_and_burn)
    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", fake_wait_for_payment)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", fake_allocate)
    monkeypatch.setattr(mint_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", fake_select)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow.swap_compose, "upload_output", fake_upload_output)

    async def fake_upload_bunny(name, data, ctype):
        return f"https://cdn.example/{name}"

    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload_bunny)

    session = mint_flow.MintSession(discord_id="1", wallet_address="rUser")
    session.payment_uuid = "PAYUUID"  # #262: a real XUMM payload exists
    # Own loop, independent of suite order (another test may have closed the
    # default loop) — same pattern as the economy flow tests' _run helper.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mint_flow.run_mint_session(session))
    finally:
        loop.close()

    assert captured, "mint_nft was never reached"
    assert captured["issuer"] == "rCOLLECTIONISSUERxxxxxxxxxxxxxxxxx"
