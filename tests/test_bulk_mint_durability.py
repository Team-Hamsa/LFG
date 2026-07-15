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

from lfg_core import bulk_mint_flow  # noqa: E402


def _async_counter(start=0):
    counter = {"n": start}

    async def _inner(*args, **kwargs):
        n = counter["n"]
        counter["n"] += 1
        return n

    return _inner


def _paid_job(tmp_path, monkeypatch, state):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 3, platform="discord")
    j.entitlement = bulk_mint_flow.entitlement.PaymentEntitlement(quantity=3)
    j.quantity = 3
    j.units = [bulk_mint_flow.Unit(index=i) for i in range(3)]
    j.pay_with, j.pay_amount, j.unit_price = "XRP", "30", "10"
    j.state = state
    return j


def test_persist_and_reload_roundtrip(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    j.units[0].state = bulk_mint_flow.OFFERED
    j.units[0].nft_id = "N0"
    bulk_mint_flow.persist(j)
    reloaded = bulk_mint_flow.load_all_resumable()
    assert len(reloaded) == 1
    r = reloaded[0]
    assert r.id == j.id
    assert r.wallet_address == "rUSER"
    assert r.units[0].state == bulk_mint_flow.OFFERED
    assert r.units[0].nft_id == "N0"
    assert r.entitlement.quantity == 3


def test_terminal_jobs_not_resumable(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.DONE)
    bulk_mint_flow.persist(j)
    assert bulk_mint_flow.load_all_resumable() == []


def test_delete_record(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    bulk_mint_flow.persist(j)
    bulk_mint_flow.delete_record(j.id)
    assert bulk_mint_flow.load_all_resumable() == []


def test_resume_skips_done_units_no_double_mint(tmp_path, monkeypatch):
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)

    calls = {"mint": 0, "wait": 0}

    async def _count_mint(**kw):
        calls["mint"] += 1
        from lfg_core.mint_flow import UnitResult

        return UnitResult(
            nft_number=kw["nft_number"],
            nft_id=f"N{kw['nft_number']}",
            image_url="i",
            offer_id="O",
            accept={"qr_url": "q", "xumm_url": "x", "uuid": "u"},
            error=None,
        )

    async def _count_wait(**kw):
        calls["wait"] += 1
        return True

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=5000)
    )
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _count_wait)

    # A job already fulfilling with 2 of 3 done, persisted to disk.
    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 3, platform="discord")
    j.clamp_to_headroom()
    j.pay_amount = "30"
    j.state = bulk_mint_flow.FULFILLING
    j.units[0].state = bulk_mint_flow.OFFERED
    j.units[1].state = bulk_mint_flow.OFFERED
    bulk_mint_flow.persist(j)

    resumed = bulk_mint_flow.load_all_resumable()[0]
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert resumed.state == bulk_mint_flow.DONE
    assert calls["mint"] == 1  # only the 1 remaining pending unit minted
    assert calls["wait"] == 0  # payment never re-waited on resume


def test_resume_minted_unit_is_reoffered_not_reminted(tmp_path, monkeypatch):
    """A unit persisted in MINTED state (nft_id set, no offer — the crash
    window between the on-chain mint and offer creation) must NEVER be
    re-minted on resume. Resume should only re-attempt the offer."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)

    calls = {"mint": 0, "offer": 0}

    async def _count_mint(**kw):
        calls["mint"] += 1
        from lfg_core.mint_flow import UnitResult

        return UnitResult(
            nft_number=kw["nft_number"],
            nft_id=f"N{kw['nft_number']}",
            image_url="i",
            offer_id="O",
            accept={"qr_url": "q", "xumm_url": "x", "uuid": "u"},
            error=None,
        )

    async def _count_offer(nft_id, destination, **kw):
        calls["offer"] += 1
        return "OFFER123"

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=5000)
    )
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _count_offer)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    j.clamp_to_headroom()
    j.pay_amount = "10"
    j.state = bulk_mint_flow.FULFILLING
    j.units[0].state = bulk_mint_flow.MINTED
    j.units[0].nft_id = "EXISTING_NFT"
    j.units[0].nft_number = 4242
    bulk_mint_flow.persist(j)

    resumed = bulk_mint_flow.load_all_resumable()[0]
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert calls["mint"] == 0  # NEVER re-mint a unit that already has an nft_id
    assert calls["offer"] == 1
    assert resumed.units[0].state == bulk_mint_flow.OFFERED
    assert resumed.units[0].offer_id == "OFFER123"
    assert resumed.units[0].nft_id == "EXISTING_NFT"  # unchanged
    assert resumed.state == bulk_mint_flow.DONE


def test_on_mint_callback_persists_minted_before_offer_step(tmp_path, monkeypatch):
    """If the offer step fails/crashes AFTER the mint landed on-chain, the
    persisted record must already show MINTED with the real nft_id — never
    PENDING — so a crash-restart can't re-mint a second edition."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)

    persisted_states = []

    async def _mint_calls_on_mint_then_fails(**kw):
        on_mint = kw.get("on_mint")
        if on_mint is not None:
            await on_mint(kw["nft_number"], f"N{kw['nft_number']}", "img_url")
        raise RuntimeError("offer step exploded after mint landed")

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _mint_calls_on_mint_then_fails)
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=9000)
    )

    orig_persist = bulk_mint_flow.persist

    def _spy_persist(job):
        orig_persist(job)
        persisted_states.append((job.units[0].state, job.units[0].nft_id))

    monkeypatch.setattr(bulk_mint_flow, "persist", _spy_persist)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    j.clamp_to_headroom()
    j.pay_amount = "10"
    j.state = bulk_mint_flow.FULFILLING

    # run_bulk_mint_job's top-level except catches the RuntimeError and marks
    # the job FAILED, but the unit-level on_mint persist must have already
    # landed MINTED with the nft_id before that happens.
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))

    minted_persists = [s for s in persisted_states if s[0] == bulk_mint_flow.MINTED]
    assert minted_persists, f"expected a MINTED persist, got {persisted_states}"
    assert minted_persists[0][1] == "N9000"
