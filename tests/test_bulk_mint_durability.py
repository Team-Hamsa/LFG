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
