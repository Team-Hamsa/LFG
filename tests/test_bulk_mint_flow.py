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

import asyncio  # noqa: E402

import pytest  # noqa: E402

from lfg_core import bulk_mint_flow, config, headroom, mint_credits, mint_flow, supply  # noqa: E402


@pytest.fixture(autouse=True)
def _headroom_env(tmp_path, monkeypatch):
    """#226: clamp_to_headroom now takes a real reservation in the per-network
    app DB — isolate it (and mint_credits/job records) to tmp and pin the
    index-backed supply at 0 so grant math never touches repo DBs. Tests
    override current_supply where the scenario needs a fuller collection."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(
        bulk_mint_flow.db_path, "app_db_path", lambda net=None: str(tmp_path / "app.db")
    )
    monkeypatch.setattr(supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(headroom.nft_index, "index_db_path", lambda net: str(tmp_path / "idx.db"))


def test_config_defaults():
    # Assert the *shipped* defaults, not the frozen constants: config's bare
    # load_dotenv() walks up from CWD, so an ambient .env (or a worktree under
    # a checkout that has one) can override either knob on a developer box.
    assert config.MAX_COLLECTION_SIZE_DEFAULT == 10000
    assert config.BULK_MINT_MAX_DEFAULT == 10


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def _job(qty):
    return bulk_mint_flow.BulkMintJob(
        discord_id="u1", wallet_address="rUSER", requested_qty=qty, platform="discord"
    )


def test_clamp_within_headroom_keeps_quantity(monkeypatch):
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(5)
    j.clamp_to_headroom()
    assert j.quantity == 5
    assert len(j.units) == 5


def test_clamp_respects_bulk_max(monkeypatch):
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    j = _job(50)
    j.clamp_to_headroom()
    assert j.quantity == 10


def test_clamp_to_headroom_when_low(monkeypatch):
    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    monkeypatch.setattr(supply, "current_supply", lambda net: config.MAX_COLLECTION_SIZE - 3)
    j = _job(8)
    j.clamp_to_headroom()
    assert j.quantity == 3


def test_clamp_collection_full_raises(monkeypatch):
    monkeypatch.setattr(supply, "current_supply", lambda net: config.MAX_COLLECTION_SIZE)
    j = _job(5)
    with pytest.raises(bulk_mint_flow.CollectionFull):
        j.clamp_to_headroom()


def _async_counter(start=0):
    counter = {"n": start}

    async def _inner(*args, **kwargs):
        n = counter["n"]
        counter["n"] += 1
        return n

    return _inner


def _fake_mint_ok():
    async def _inner(*, nft_number, **kwargs):
        return mint_flow.UnitResult(
            nft_number=nft_number,
            nft_id=f"nft-{nft_number}",
            image_url="https://cdn.example/img.png",
            offer_id=f"offer-{nft_number}",
            accept={"xumm_url": "x"},
            error=None,
        )

    return _inner


def _fake_mint_offer_fail():
    async def _inner(*, nft_number, **kwargs):
        return mint_flow.UnitResult(
            nft_number=nft_number,
            nft_id=f"nft-{nft_number}",
            image_url="https://cdn.example/img.png",
            offer_id=None,
            accept=None,
            error="offer creation failed",
        )

    return _inner


def test_fulfillment_all_units_offered(monkeypatch, tmp_path):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=4000)
    )
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _fake_mint_ok())
    j = _job(3)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    assert j.state == bulk_mint_flow.DONE
    assert all(u.state == bulk_mint_flow.OFFERED for u in j.units)


def test_offer_permanently_failing_leaves_job_fulfilling_not_done(monkeypatch, tmp_path):
    """A unit that mints but whose offer permanently fails (including the
    final re-offer pass in run_bulk_mint_job) must NOT let the job reach the
    terminal DONE state -- that would strand a minted-but-never-offered NFT
    forever (DONE is not resumed by load_all_resumable). The job must stay
    FULFILLING (resumable) with the unit still MINTED, and mint_one_unit must
    never be called again for that unit (no re-mint)."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=4000)
    )
    mint_spy = _fake_mint_offer_fail()
    mint_calls = {"n": 0}

    async def _spy(*a, **kw):
        mint_calls["n"] += 1
        return await mint_spy(*a, **kw)

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _spy)
    monkeypatch.setattr(bulk_mint_flow.db_path, "app_db_path", lambda net: str(tmp_path / "app.db"))

    async def _always_fail_offer(*a, **kw):
        return None

    async def _no_offers(*a, **kw):
        return []  # nothing to adopt (#227)

    async def _owner_unknown(*a, **kw):
        return None  # indeterminate owner -> fail closed, keep retrying create

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _always_fail_offer)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_nft_sell_offers", _no_offers)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "nft_info", _owner_unknown)

    j = _job(2)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))

    assert j.state == bulk_mint_flow.FULFILLING  # NOT DONE -- resumable
    assert all(u.nft_id is not None for u in j.units)
    assert all(u.state == bulk_mint_flow.MINTED for u in j.units)
    # Each unit minted exactly once -- never re-minted while offer-retrying.
    assert mint_calls["n"] == len(j.units)
    # NFT was delivered (minted) even though offer failed -> no credit should be created
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", j.network) == 0


def test_offer_fail_then_succeed_ends_done_with_unit_offered(monkeypatch, tmp_path):
    """A unit whose offer fails during the main loop but succeeds on the
    final re-offer pass ends the job DONE with the unit OFFERED."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=4000)
    )
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _fake_mint_offer_fail())
    monkeypatch.setattr(bulk_mint_flow.db_path, "app_db_path", lambda net: str(tmp_path / "app.db"))

    offer_calls = {"n": 0}

    async def _fail_once_then_succeed(*a, **kw):
        offer_calls["n"] += 1
        if offer_calls["n"] <= 1:
            return None
        return "OFFER-RETRY"

    async def _no_offers(*a, **kw):
        return []  # nothing to adopt (#227)

    async def _owner_unknown(*a, **kw):
        return None  # indeterminate owner -> fail closed, fall through to create

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _fail_once_then_succeed)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_nft_sell_offers", _no_offers)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "nft_info", _owner_unknown)

    j = _job(1)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))

    assert j.state == bulk_mint_flow.DONE
    assert j.units[0].state == bulk_mint_flow.OFFERED
    assert j.units[0].offer_id == "OFFER-RETRY"


def test_prepare_payment_multiplies_price_xrp(monkeypatch):
    import asyncio

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


def test_prepare_payment_pins_signing_account(monkeypatch):
    """Same invariant as single mint: only the job's own wallet may sign the
    payment, so a second Xaman account cannot pay into a wait that will never
    match it."""
    import asyncio

    captured = {}

    async def fake_payload(destination, **kw):
        captured.update(kw)
        return {"xumm_url": "x", "uuid": "u"}

    monkeypatch.setattr(config, "BULK_MINT_MAX", 10)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_trustline_balance", _async_return(None))
    monkeypatch.setattr(bulk_mint_flow.xumm_ops, "create_payment_payload", fake_payload)
    j = _job(2)
    j.clamp_to_headroom()
    asyncio.run(j.prepare_payment())
    assert captured["account"] == "rUSER"
