# Task 5 (#215): mint_one_unit extracts the compose→upload→mint→record→offer
# body of run_mint_session into a standalone reusable unit so the upcoming
# bulk-mint loop and the existing single mint share one code path.
#
# Env-guard preamble (verbatim pattern from tests/test_mint_issuer.py /
# tests/test_mint_cancel.py): importing lfg_core.config freezes its constants
# at import time; set the same defaults test_smoke.py uses so collection
# order can't strand them.
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

import pytest  # noqa: E402

from lfg_core import mint_flow  # noqa: E402


def _async_return(value):
    async def _f(*args, **kwargs):
        return value

    return _f


@pytest.fixture
def _mint_mocks(monkeypatch):
    """Model on the mocking approach used by tests/test_mint_cdn_paths.py and
    tests/test_mint_issuer.py: stub every network/CDN/XRPL boundary so the
    pipeline runs entirely in-process."""
    captured: dict[str, Any] = {}

    async def fake_select(store):
        return "male", [{"trait_type": "Body", "value": "Straight"}]

    async def fake_compose(attributes, body, store, basename):
        return "/tmp/out.png", False

    async def fake_upload_output(path, is_video, upload_fn, basename, keep_still=None):
        return f"https://cdn.example/{basename}.png", None

    async def fake_upload_bunny(name, data, ctype):
        return f"https://cdn.example/{name}"

    async def fake_mint_nft(**kwargs):
        captured["mint_nft_kwargs"] = kwargs
        return "NFTID1"

    async def fake_create_nft_offer(*args, **kwargs):
        return "OFFER1"

    async def fake_create_accept_offer_payload(*args, **kwargs):
        return {"qr_url": "q", "xumm_url": "x", "uuid": "u"}

    def fake_record_nft_mint(**kwargs):
        return True

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

    # image_archive.promote_still/discard_still touch the filesystem; no-op
    # them so the test doesn't depend on a real archive directory.
    monkeypatch.setattr(mint_flow.image_archive, "promote_still", lambda *a, **k: None)
    monkeypatch.setattr(mint_flow.image_archive, "discard_still", lambda *a, **k: None)
    monkeypatch.setattr(
        mint_flow.image_archive, "pending_still_path", lambda *a, **k: "/tmp/pending.png"
    )

    return captured


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_mint_one_unit_happy_path(monkeypatch, _mint_mocks):
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4000,
            session_tag="job1:0",
        )
    )
    assert res.nft_id == "NFTID1"
    assert res.offer_id == "OFFER1"
    assert res.accept is not None
    assert res.accept["uuid"] == "u"
    assert res.error is None
    assert res.nft_number == 4000
    assert res.image_url is not None
    # #41: traits (LFG-naming) + body_type are threaded through so a caller
    # can store them on the session / bulk unit for downstream consumers.
    assert res.traits == {"Body": "Straight"}
    assert res.body_type == "male"


def test_mint_one_unit_offer_fail_reports_nft_id(monkeypatch, _mint_mocks):
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", _async_return(None))
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4001,
            session_tag="job1:1",
        )
    )
    assert res.nft_id == "NFTID1"  # minted
    assert res.offer_id is None  # offer failed
    assert res.error is not None
    # traits/body_type are known as soon as the mint lands, even if the
    # subsequent offer step fails.
    assert res.traits == {"Body": "Straight"}
    assert res.body_type == "male"


def test_mint_one_unit_mint_fail_reports_no_nft_id(monkeypatch, _mint_mocks):
    async def fake_mint_nft(**kwargs):
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4002,
            session_tag="job1:2",
        )
    )
    assert res.nft_id is None
    assert res.offer_id is None
    assert res.error is not None
    # The mint never landed, so traits/body_type were never computed --
    # None-safe defaults, not stale/partial data.
    assert res.traits is None
    assert res.body_type is None


def test_bulk_unit_offer_has_no_expiration(monkeypatch, _mint_mocks):
    # Task 11 (#215): bulk minting drives mint_one_unit exactly like the
    # single-mint path, which routes through the already-SourceTag-stamped
    # xrpl_ops builders (see tests/test_xrpl_source_tag.py). Pin two
    # hackathon/provenance invariants at this boundary: offers never carry an
    # Expiration, and mint/offer both receive a provenance `platform` kwarg
    # (memos.platform_for_surface(...)) so the on-chain memo is never omitted.
    seen: dict[str, Any] = {}

    async def _spy_offer(nft_id, destination, **kw):
        seen.update(kw)
        seen["nft_id"] = nft_id
        return "OFFER1"

    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", _spy_offer)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4200,
            session_tag="job1:bulk0",
        )
    )
    assert res.error is None
    assert "expiration" not in seen and "Expiration" not in seen
    assert seen.get("platform") == mint_flow.memos.platform_for_surface("discord")

    # mint_nft (captured by the _mint_mocks fixture) must also carry the
    # provenance platform kwarg, never omitted.
    assert _mint_mocks["mint_nft_kwargs"]["platform"] == mint_flow.memos.platform_for_surface(
        "discord"
    )


def test_mint_one_unit_calls_on_state_in_order(monkeypatch, _mint_mocks):
    states: list[str] = []
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4003,
            session_tag="job1:3",
            on_state=states.append,
        )
    )
    assert res.error is None
    assert states == [mint_flow.MINTING, mint_flow.CREATING_OFFER]


def test_mint_one_unit_calls_on_mint_before_creating_offer_state(monkeypatch, _mint_mocks):
    calls: list[tuple[int, str, str | None]] = []
    order: list[str] = []

    async def _on_mint(nft_number, nft_id, image_url):
        calls.append((nft_number, nft_id, image_url))
        order.append("on_mint")

    def _on_state(state):
        order.append(state)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4004,
            session_tag="job1:4",
            on_state=_on_state,
            on_mint=_on_mint,
        )
    )
    assert res.error is None
    assert len(calls) == 1
    nft_number, nft_id, image_url = calls[0]
    assert nft_number == 4004
    assert nft_id == res.nft_id == "NFTID1"
    assert image_url == res.image_url
    # on_mint must fire before the CREATING_OFFER state -- i.e. the unit is
    # persisted as MINTED before any offer/XUMM steps run.
    assert order == [mint_flow.MINTING, "on_mint", mint_flow.CREATING_OFFER]
