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
import json  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

from lfg_core import config, mint_flow, swap_meta, xrpl_ops  # noqa: E402


def _expected_traits(**overrides):
    """LFG-naming traits dict after the #268 normalize step: every TRAIT_ORDER
    slot is present ('None'-filled), with the layer tree's Head renamed to the
    LFG table's Hat column."""
    traits = dict.fromkeys(swap_meta.TRAIT_ORDER, "None")
    traits["Body"] = "Straight"
    traits.update(overrides)
    traits["Hat"] = traits.pop("Head")
    return traits


def _async_return(value):
    async def _f(*args, **kwargs):
        return value

    return _f


@pytest.fixture
def _mint_mocks(monkeypatch, tmp_path):
    """Model on the mocking approach used by tests/test_mint_cdn_paths.py and
    tests/test_mint_issuer.py: stub every network/CDN/XRPL boundary so the
    pipeline runs entirely in-process.

    Also isolates CWD to tmp_path: mint_flow._save_recovery_record writes a
    CWD-relative failed_db_records/ path (#466), and every test using this
    fixture is now able to reach it (a permanently-failing create_nft_offer
    fake exhausts ensure_offer's retries) -- without this, that write lands
    in the actual checkout (the #509 conftest guard catches it)."""
    monkeypatch.chdir(tmp_path)
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

    async def fake_get_nft_sell_offers(*args, **kwargs):
        # offer_delivery.ensure_offer's adopt check (#466): no pre-existing
        # live offer for a freshly minted token.
        return []

    async def fake_nft_info(*args, **kwargs):
        # offer_delivery.ensure_offer's delivered check (#466): unknown ->
        # fails closed, falls through to create (never a real RPC in tests).
        return None

    async def _no_sleep(*args, **kwargs):
        # ensure_offer's inter-attempt backoff (#466, default base_delay=2.0)
        # would otherwise add real wall-clock seconds to every test whose
        # create_nft_offer fake fails at least once.
        return None

    async def fake_create_accept_offer_payload(*args, **kwargs):
        captured["accept_kwargs"] = kwargs
        return {"qr_url": "q", "xumm_url": "x", "uuid": "u"}

    def fake_record_nft_mint(**kwargs):
        return True

    monkeypatch.setattr(mint_flow.layer_store, "get_layer_store", lambda: object())
    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", fake_select)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow.swap_compose, "upload_output", fake_upload_output)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload_bunny)

    async def fake_destination_preflight(address):
        # XLS-52 folding gate: default to a wallet the ledger PROVES can take
        # delivery, so the fixture exercises the folded path.
        return xrpl_ops.DestinationPreflight(
            exists=True, blocks_nft_offers=False, balance_drops=50_000_000, owner_count=0
        )

    monkeypatch.setattr(mint_flow.xrpl_ops, "destination_preflight", fake_destination_preflight)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", fake_create_nft_offer)
    monkeypatch.setattr(mint_flow.xrpl_ops, "get_nft_sell_offers", fake_get_nft_sell_offers)
    monkeypatch.setattr(mint_flow.xrpl_ops, "nft_info", fake_nft_info)
    monkeypatch.setattr(mint_flow.asyncio, "sleep", _no_sleep)
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
    assert res.traits == _expected_traits()
    assert res.body_type == "male"
    # The delivery offer is locked to this wallet on-ledger; pin the payload
    # to it too, so Xaman refuses a wrong-account signature up front instead
    # of letting it fail as tecNO_PERMISSION.
    assert _mint_mocks["accept_kwargs"]["account"] == "rUSER"


@pytest.mark.parametrize("legacy_caller", ["paid", "bulk"])
def test_paid_and_bulk_paths_share_one_folded_mint_signature(
    monkeypatch, _mint_mocks, legacy_caller
):
    """Paid single mint and bulk fulfillment must keep calling mint_nft with
    ONE signature. XLS-52 changed that signature -- the delivery offer is now
    folded into the mint, so `destination` and `return_details` are part of the
    contract both paths use -- and a strict double pins it against drift.
    """
    seen = {}

    async def strict_mint_nft(
        *, metadata_cdn_url, taxon, issuer, platform, destination, return_details
    ):
        seen.update(
            metadata_cdn_url=metadata_cdn_url,
            taxon=taxon,
            issuer=issuer,
            platform=platform,
            destination=destination,
            return_details=return_details,
        )
        return "NFTID1"

    async def bulk_record(_nft_number, _nft_id, _image_url):
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", strict_mint_nft)
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4008,
            session_tag=f"{legacy_caller}:0",
            on_mint=bulk_record if legacy_caller == "bulk" else None,
        )
    )

    assert res.error is None
    assert res.nft_id == "NFTID1"
    assert seen["issuer"] == mint_flow.config.SWAP_ISSUER_ADDRESS
    assert seen["destination"] == "rUSER"
    assert seen["return_details"] is True
    # A double still returning the pre-XLS-52 bare-string contract yields no
    # folded offer id, so delivery falls through to ensure_offer unchanged.
    assert res.offer_id == "OFFER1"


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
    assert res.traits == _expected_traits()
    assert res.body_type == "male"


def test_mint_one_unit_offer_permanently_fails_lands_recovery_state(
    monkeypatch, _mint_mocks, tmp_path
):
    """#466: a single mint whose NFTokenCreateOffer fails on every retry must
    not dead-end with a generic "contact an administrator" message and zero
    trace of the problem -- the NFT is safely on-chain, only delivery is
    stuck. ensure_offer's exhausted-attempts path must land a recovery
    record an administrator can act on, and the user-facing message must
    say the mint succeeded, not that something needs a human to fix it.
    (_mint_mocks already chdir'd CWD to this same tmp_path, so
    failed_db_records/ lands here rather than in the checkout.)"""
    create_calls = {"n": 0}

    async def _always_fail(nft_id, destination, **kwargs):
        create_calls["n"] += 1
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", _always_fail)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4021,
            session_tag="job1:21",
        )
    )

    assert res.nft_id == "NFTID1"  # minted on-chain regardless of the offer
    assert res.offer_id is None
    assert res.accept is None
    assert create_calls["n"] == 3  # ensure_offer's default attempts, exhausted

    assert res.error is not None
    lowered = res.error.lower()
    assert "contact an administrator" not in lowered
    assert "minted" in lowered
    # #571 review (Greptile P1 "Message Promises Missing Automation"): this
    # path only ever writes a recovery record for a HUMAN to act on -- no
    # automatic re-offer sweep exists (that is #518, explicitly deferred).
    # The message must never claim otherwise, and must say the NFT is safe
    # (held by the issuer, not lost).
    assert "automatically" not in lowered
    assert "no action needed" not in lowered
    assert "issuer" in lowered
    assert "not been lost" in lowered or "not lost" in lowered

    record_path = tmp_path / "failed_db_records" / "nft_4021_minted_no_offer.json"
    assert record_path.exists(), "expected a minted_no_offer recovery record on disk"
    record = json.loads(record_path.read_text())
    assert record["state"] == "minted_no_offer"
    assert record["nft_id"] == "NFTID1"
    assert record["wallet"] == "rUSER"


def test_mint_one_unit_post_mint_exception_preserves_traits_and_body_type(monkeypatch, _mint_mocks):
    """PR #245 review (CodeRabbit, outside-diff): an exception from on_mint,
    offer creation, or payload creation after a confirmed mint used to reach
    the catch-all with traits/body_type reset to None, even though they were
    already computed. This drives that path via a raising `on_mint` callback
    and asserts the exception UnitResult retains both."""

    async def _boom(nft_number, nft_id, image_url):
        raise RuntimeError("on_mint boom")

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4006,
            session_tag="job1:6",
            on_mint=_boom,
        )
    )
    assert res.nft_id == "NFTID1"  # mint landed before on_mint raised
    assert res.error is not None
    assert res.traits == _expected_traits()
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


def test_mint_one_unit_detailed_callback_receives_validated_mint_hash(monkeypatch, _mint_mocks):
    observed = []

    async def fake_mint_nft(**kwargs):
        assert kwargs["return_details"] is True
        return mint_flow.xrpl_ops.MintNFTResult(nft_id="NFTID1", tx_hash="MINTTX1")

    async def on_mint_confirmed(nft_number, nft_id, mint_tx_hash, image_url):
        observed.append((nft_number, nft_id, mint_tx_hash, image_url))

    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4007,
            session_tag="job1:7",
            on_mint_confirmed=on_mint_confirmed,
        )
    )

    assert res.error is None
    assert res.mint_tx_hash == "MINTTX1"
    assert observed == [(4007, "NFTID1", "MINTTX1", res.image_url)]


def test_mint_one_unit_video_carries_video_url(monkeypatch, _mint_mocks):
    # Animated composition: compose returns an mp4, upload returns both the
    # PNG poster and the MP4 — the UnitResult must carry the video URL so
    # surfaces can show the animation instead of the still (Telegram bug).
    async def fake_compose(attributes, body, store, basename):
        return "/tmp/out.mp4", True

    async def fake_upload_output(path, is_video, upload_fn, basename, keep_still=None):
        return (
            f"https://cdn.example/{basename}.png",
            f"https://cdn.example/{basename}.mp4",
        )

    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow.swap_compose, "upload_output", fake_upload_output)
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4010,
            session_tag="job1:9",
        )
    )
    assert res.error is None
    assert res.image_url == "https://cdn.example/4010/4010_0.png"
    assert res.video_url == "https://cdn.example/4010/4010_0.mp4"


def test_mint_one_unit_still_has_no_video_url(monkeypatch, _mint_mocks):
    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4011,
            session_tag="job1:10",
        )
    )
    assert res.error is None
    assert res.video_url is None


def test_mint_one_unit_normalizes_roll_for_compose_and_metadata(monkeypatch, _mint_mocks):
    """#268 (NFT #4039): a rolled legacy Accessory duplicate of a Back value
    must be canonicalized ONCE — compose and the uploaded metadata JSON must
    both see the relocated view (Accessory -> None, Back set), identically."""
    captured: dict[str, Any] = {}

    async def fake_select(store):
        return "male", [
            {"trait_type": "Body", "value": "Straight"},
            {"trait_type": "Back", "value": "Angel Wings Open"},
            {"trait_type": "Accessory", "value": "Angel Wings Open"},
        ]

    async def fake_compose(attributes, body, store, basename):
        captured["compose_attrs"] = [dict(a) for a in attributes]
        return "/tmp/out.png", False

    async def fake_upload_bunny(name, data, ctype):
        if name.endswith(".json"):
            captured["metadata"] = json.loads(data)
        return f"https://cdn.example/{name}"

    monkeypatch.setattr(mint_flow.traits, "select_random_attributes", fake_select)
    monkeypatch.setattr(mint_flow.swap_compose, "compose_nft", fake_compose)
    monkeypatch.setattr(mint_flow, "_upload_to_bunny", fake_upload_bunny)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4039,
            session_tag="job1:268",
        )
    )
    assert res.error is None
    meta_attrs = captured["metadata"]["attributes"]
    # Compose and metadata saw the IDENTICAL canonical list.
    assert captured["compose_attrs"] == meta_attrs
    # Canonical: the Accessory duplicate relocated to Back, Accessory emptied.
    assert swap_meta.get_attr(meta_attrs, "Back") == "Angel Wings Open"
    assert swap_meta.get_attr(meta_attrs, "Accessory") == "None"
    assert res.traits == _expected_traits(Back="Angel Wings Open")


def test_mint_session_to_dict_carries_video_url():
    session = mint_flow.MintSession("u1", "rUSER")
    session.video_url = "https://cdn.example/1/1_0.mp4"
    assert session.to_dict()["video_url"] == "https://cdn.example/1/1_0.mp4"


# --- XRP funding check before the payment request (#514) --------------------
#
# A Payment the wallet cannot fund can only fail on-ledger (tecUNFUNDED_PAYMENT)
# after the user was already asked to approve it: 0 of 45 Joey requests signed
# in a week. prepare_payment refuses a DEFINITE shortfall from the account_info
# snapshot the mint pre-flight already holds:
#   spendable = balance - (base_reserve + owner_reserve * owner_count)
# and never refuses on anything it does not know.


def _stub_trustline(monkeypatch, state, balance=None):
    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_state", _async_return((state, balance)))


@pytest.fixture
def _xrp_path(monkeypatch):
    """Price 9 XRP, reserves 1 XRP + 0.2 XRP per owned object, a 12-drop fee,
    and a wallet with no LFGO trustline. Returns the payment payloads built."""
    monkeypatch.setattr(config, "MINT_PRICE_XRP", "9")
    monkeypatch.setattr(config, "XRPL_RESERVE_BASE_DROPS", 1_000_000)
    monkeypatch.setattr(config, "XRPL_RESERVE_INC_DROPS", 200_000)
    monkeypatch.setattr(mint_flow, "PAYMENT_FEE_DROPS", 12)
    _stub_trustline(monkeypatch, xrpl_ops.TrustlineState.ABSENT)
    built: list[dict[str, Any]] = []

    async def _payload(destination, **kwargs):
        built.append({"destination": destination, **kwargs})
        return {"qr_url": None, "xumm_url": "lfg-wc://wc-pay", "uuid": "wc-pay"}

    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", _payload)
    return built


def _snapshot(balance_drops, owner_count):
    return xrpl_ops.DestinationPreflight(
        exists=True, blocks_nft_offers=False, balance_drops=balance_drops, owner_count=owner_count
    )


_UNKNOWN_SNAPSHOTS = pytest.mark.parametrize(
    "snapshot",
    [
        None,
        xrpl_ops.DestinationPreflight(None, None, None, None),
        xrpl_ops.DestinationPreflight(True, False, None, 4),
        xrpl_ops.DestinationPreflight(True, False, 0, None),
    ],
    ids=["no-snapshot", "lookup-failed", "no-balance", "no-owner-count"],
)


def test_xrp_path_definite_shortfall_refused(_xrp_path):
    session = mint_flow.MintSession("u1", "rUSER")
    # 10 XRP - (1 + 4 x 0.2) XRP reserve = 8.2 XRP spendable < 9 XRP + 12 drops
    with pytest.raises(mint_flow.InsufficientXrp) as refused:
        _run(session.prepare_payment(_snapshot(10_000_000, 4)))
    assert refused.value.needed_drops == 9_000_012
    assert refused.value.spendable_drops == 8_200_000
    assert _xrp_path == []  # no sign request was ever built


def test_xrp_path_exactly_enough_proceeds(_xrp_path):
    session = mint_flow.MintSession("u1", "rUSER")
    # 10_800_012 - 1_800_000 reserve = 9_000_012 = price + fee, to the drop
    _run(session.prepare_payment(_snapshot(10_800_012, 4)))
    assert session.pay_with == "XRP"
    assert [p["value"] for p in _xrp_path] == ["9"]
    assert session.payment_uuid == "wc-pay"


@_UNKNOWN_SNAPSHOTS
def test_xrp_path_snapshot_missing_fails_open(_xrp_path, snapshot):
    session = mint_flow.MintSession("u1", "rUSER")
    _run(session.prepare_payment(snapshot))
    assert session.pay_with == "XRP"
    assert len(_xrp_path) == 1


def test_lfgo_path_unaffected(_xrp_path, monkeypatch):
    """LFGO holders pay in LFGO: the XRP funding check never applies to them."""
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "1")
    _stub_trustline(monkeypatch, xrpl_ops.TrustlineState.PRESENT, Decimal("5"))
    session = mint_flow.MintSession("u1", "rUSER")
    _run(session.prepare_payment(_snapshot(1_000_000, 4)))  # 0 XRP spendable
    assert session.pay_with == "LFGO"
    assert len(_xrp_path) == 1


def test_xrp_path_not_refused_when_the_trustline_lookup_failed(_xrp_path, monkeypatch):
    """A trustline lookup answers None for BOTH "no line" and "the lookup
    failed", and it runs on a single websocket with no failover. A blip must
    never refuse an LFGO holder for XRP they do not need: an UNKNOWN line
    state falls through to the XRP path without the funding check."""
    _stub_trustline(monkeypatch, xrpl_ops.TrustlineState.UNKNOWN)
    session = mint_flow.MintSession("u1", "rUSER")
    _run(session.prepare_payment(_snapshot(10_000_000, 4)))  # a known line would refuse
    assert session.pay_with == "XRP"
    assert len(_xrp_path) == 1


def test_mint_one_unit_uses_the_folded_offer_and_skips_ensure_offer(monkeypatch, _mint_mocks):
    """XLS-52: when the NFTokenMint itself created the delivery offer, there is
    no second transaction to ensure -- the offer is on-ledger iff the mint is,
    which is what makes the minted-but-never-offered state unreachable."""

    async def fake_mint_nft(**kwargs):
        _mint_mocks["mint_nft_kwargs"] = kwargs
        return xrpl_ops.MintNFTResult(nft_id="NFTID1", tx_hash="D" * 64, offer_id="FOLDED1")

    async def boom(*a, **k):
        raise AssertionError("ensure_offer must not run when the mint already offered")

    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)
    monkeypatch.setattr(mint_flow.offer_delivery, "ensure_offer", boom)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4010,
            session_tag="job1:0",
        )
    )

    assert _mint_mocks["mint_nft_kwargs"]["destination"] == "rUSER"
    assert res.offer_id == "FOLDED1"
    assert res.error is None


def test_a_failed_folded_mint_is_never_retried(monkeypatch, _mint_mocks):
    """mint_nft returning None must NEVER trigger a second NFTokenMint.

    None is not proof the first mint failed: _validated_result returns None
    whenever meta.TransactionResult is not tesSUCCESS, INCLUDING a malformed or
    absent meta on a transaction that DID commit. Retrying there could mint a
    duplicate (CodeRabbit, PR #577), so the folded-vs-plain choice is a
    pre-mint gate and a failed mint stays failed.
    """
    calls: list[dict[str, Any]] = []

    async def fake_mint_nft(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4011,
            session_tag="job1:0",
        )
    )

    assert len(calls) == 1, "a failed mint must not be re-submitted"
    assert res.nft_id is None
    assert res.error is not None


@pytest.mark.parametrize(
    "preflight",
    [
        pytest.param((None, None), id="lookup_unresolved"),
        pytest.param((True, True), id="blocks_nft_offers"),
        pytest.param((False, None), id="wallet_unfunded"),
    ],
)
def test_the_mint_is_only_folded_when_delivery_is_proven_possible(
    monkeypatch, _mint_mocks, preflight
):
    """Folding stakes the MINT on the offer being creatable, so a destination
    the ledger cannot PROVE will accept it mints unfolded and lets
    offer_delivery.ensure_offer own delivery, exactly as before XLS-52."""
    exists, blocks = preflight
    calls: list[dict[str, Any]] = []

    async def fake_preflight(address):
        return xrpl_ops.DestinationPreflight(
            exists=exists, blocks_nft_offers=blocks, balance_drops=None, owner_count=None
        )

    async def fake_mint_nft(**kwargs):
        calls.append(kwargs)
        return "NFTID1"

    monkeypatch.setattr(mint_flow.xrpl_ops, "destination_preflight", fake_preflight)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4012,
            session_tag="job1:0",
        )
    )

    assert len(calls) == 1
    assert calls[0]["destination"] is None, "must not stake the mint on an unproven destination"
    assert res.offer_id == "OFFER1"  # the pre-XLS-52 delivery path
    assert res.error is None


def test_a_pre_flight_that_raises_does_not_fail_the_mint(monkeypatch, _mint_mocks):
    """An account_info blip must degrade to an unfolded mint, never take
    minting down (the #388/#408 fail-open posture)."""
    calls: list[dict[str, Any]] = []

    async def boom(address):
        raise RuntimeError("rpc down")

    async def fake_mint_nft(**kwargs):
        calls.append(kwargs)
        return "NFTID1"

    monkeypatch.setattr(mint_flow.xrpl_ops, "destination_preflight", boom)
    monkeypatch.setattr(mint_flow.xrpl_ops, "mint_nft", fake_mint_nft)

    res = _run(
        mint_flow.mint_one_unit(
            discord_id="u1",
            wallet_address="rUSER",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4013,
            session_tag="job1:0",
        )
    )

    assert calls[0]["destination"] is None
    assert res.nft_id == "NFTID1"
    assert res.error is None
