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
import sqlite3  # noqa: E402

import pytest  # noqa: E402

from lfg_core import bulk_mint_flow, config, headroom, mint_credits, mint_flow, supply  # noqa: E402


@pytest.fixture(autouse=True)
def _headroom_env(tmp_path, monkeypatch):
    """#226: isolate the reservation store / job records / credits to tmp and
    pin the index-backed supply at 0. Tests override current_supply to model
    a nearly-full collection."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    monkeypatch.setattr(
        bulk_mint_flow.db_path, "app_db_path", lambda net=None: str(tmp_path / "app.db")
    )
    monkeypatch.setattr(supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(headroom.nft_index, "index_db_path", lambda net: str(tmp_path / "idx.db"))


def _db(tmp_path):
    return str(tmp_path / "app.db")


def _job(qty=2):
    return bulk_mint_flow.BulkMintJob("u1", "rUSER", qty, platform="discord")


def test_lost_reservation_becomes_credit_never_a_mint(monkeypatch, tmp_path):
    """The per-unit gate is now reservation-aware (#226): a job whose
    reservation PROVABLY vanished (crash-rebuild dropped an orphan —
    reserved_for reads a successful 0) must convert every unfulfilled unit
    into a durable mint credit without ever attempting a mint. (A failed
    read is None, not 0 — see the tri-state tests below.)"""
    calls = {"mint": 0}

    async def _count_mint(**kw):
        calls["mint"] += 1
        raise AssertionError("mint_one_unit must not be called without a reservation")

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)

    j = _job(2)
    j.clamp_to_headroom()
    # Simulate the orphan-drop a startup rebuild performs for a record-less job.
    headroom.release(_db(tmp_path), f"bulk:{j.id}")
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    # no unit could mint; both converted to credit, none lost
    assert mint_credits.get_credits(_db(tmp_path), "u1", j.network) == 2
    assert all(u.state == bulk_mint_flow.UNIT_FAILED for u in j.units)
    assert calls["mint"] == 0


def test_unreadable_reservation_retries_then_mints(monkeypatch, tmp_path):
    """#226 review: reserved_for is tri-state — None (the store read itself
    failed, e.g. a transient app-DB lock) must NOT convert a paid unit to a
    credit the way a provable 0 does. The unit burns an attempt on a short
    backoff and re-checks; once the store reads again, the still-valid grant
    mints and delivers as normal."""
    reads = {"n": 0}
    real_reserved_for = headroom.reserved_for

    def flaky_reserved_for(db, claimant):
        reads["n"] += 1
        if reads["n"] == 1:
            return None  # transient lock on the first read
        return real_reserved_for(db, claimant)

    monkeypatch.setattr(bulk_mint_flow.headroom, "reserved_for", flaky_reserved_for)
    monkeypatch.setattr(bulk_mint_flow, "_RESERVATION_RETRY_DELAY_SECONDS", 0)

    counter = {"n": 0}

    async def _alloc():
        counter["n"] += 1
        return 4200 + counter["n"]

    async def _mint_ok(*, nft_number, on_mint=None, **kw):
        if on_mint:
            await on_mint(nft_number, f"nft-{nft_number}", None)
        return mint_flow.UnitResult(
            nft_number=nft_number,
            nft_id=f"nft-{nft_number}",
            image_url=None,
            offer_id=f"offer-{nft_number}",
            accept={"xumm_url": "x"},
            error=None,
        )

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number", _alloc)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _mint_ok)

    j = _job(1)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    assert j.units[0].state == bulk_mint_flow.OFFERED  # delivered, not credited
    assert mint_credits.get_credits(_db(tmp_path), "u1", j.network) == 0


def test_persistently_unreadable_reservation_never_mints(monkeypatch, tmp_path):
    """#226 review, fail-closed half of the tri-state: if the store stays
    unreadable across every attempt, the grant is never provable — no mint
    may run under it. The unit converts to a durable credit (money kept)."""
    monkeypatch.setattr(bulk_mint_flow.headroom, "reserved_for", lambda db, c: None)
    monkeypatch.setattr(bulk_mint_flow, "_RESERVATION_RETRY_DELAY_SECONDS", 0)
    calls = {"mint": 0}

    async def _never(**kw):
        calls["mint"] += 1
        raise AssertionError("must never mint under an unprovable grant")

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _never)

    j = _job(1)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    assert calls["mint"] == 0
    assert j.units[0].state == bulk_mint_flow.UNIT_FAILED
    assert mint_credits.get_credits(_db(tmp_path), "u1", j.network) == 1


def test_two_jobs_at_the_tail_cannot_overshoot(monkeypatch):
    """THE #226 defect scenario: supply 9995 under MAX 10000, two concurrent
    jobs each request 5. Grants must sum to exactly the 5 remaining slots —
    the first job takes them all, the second gets zero (CollectionFull) —
    where the old racy supply read gave 5 to each (overshoot to 10005)."""
    monkeypatch.setattr(supply, "current_supply", lambda net: 9995)
    a = _job(5)
    a.clamp_to_headroom()
    assert a.quantity == 5
    b = _job(5)
    with pytest.raises(bulk_mint_flow.CollectionFull):
        b.clamp_to_headroom()


def test_second_job_gets_partial_grant_at_the_tail(monkeypatch):
    monkeypatch.setattr(supply, "current_supply", lambda net: 9995)
    a = _job(3)
    a.clamp_to_headroom()
    assert a.quantity == 3
    b = _job(5)
    b.clamp_to_headroom()
    assert b.quantity == 2  # grants sum to the 5 remaining slots


def test_minted_units_keep_counting_until_indexed(monkeypatch, tmp_path):
    """Retire-at-mint moves a unit reserved->pending, NOT released: while the
    listener-lagged index still reads the old supply, a third job must still
    see zero availability (the conservative direction — brief double-count
    admissible, overshoot never)."""
    monkeypatch.setattr(supply, "current_supply", lambda net: 9995)
    a = _job(5)
    a.clamp_to_headroom()
    # Two of A's units mint on-chain (index hasn't seen them yet).
    headroom.retire_to_pending(_db(tmp_path), f"bulk:{a.id}", "NFT-1")
    headroom.retire_to_pending(_db(tmp_path), f"bulk:{a.id}", "NFT-2")
    assert headroom.reserved_for(_db(tmp_path), f"bulk:{a.id}") == 3
    assert headroom.outstanding(_db(tmp_path)) == 5  # 3 reserved + 2 pending
    c = _job(1)
    with pytest.raises(bulk_mint_flow.CollectionFull):
        c.clamp_to_headroom()


def test_cancel_releases_reservation(monkeypatch, tmp_path):
    monkeypatch.setattr(bulk_mint_flow.payment_ledger, "find_claimed", lambda c: False)
    j = _job(2)
    j.clamp_to_headroom()
    assert headroom.outstanding(_db(tmp_path)) == 2
    assert j.cancel() is True
    assert headroom.outstanding(_db(tmp_path)) == 0


def test_payment_timeout_releases_reservation(monkeypatch, tmp_path):
    async def _no_payment(**kw):
        return False

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _no_payment)
    monkeypatch.setattr(bulk_mint_flow.payment_ledger, "find_claimed", lambda c: False)
    j = _job(2)
    j.clamp_to_headroom()
    j.pay_amount = "20"
    j.created_at = 0  # window long expired -> grace-bounded wait, then timeout
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    assert j.state == bulk_mint_flow.PAYMENT_TIMEOUT
    assert headroom.outstanding(_db(tmp_path)) == 0


def test_job_failure_releases_reservation(monkeypatch, tmp_path):
    async def _boom():
        raise RuntimeError("allocator down")

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number", _boom)
    j = _job(2)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    assert j.state == bulk_mint_flow.FAILED
    assert headroom.outstanding(_db(tmp_path)) == 0


def test_done_job_holds_only_pending_rows(monkeypatch, tmp_path):
    """A completed job's reservation is fully retired: reserved side 0, and
    each minted unit sits in the pending set until the index catches up."""

    async def _mint_ok(*, nft_number, on_mint=None, **kw):
        if on_mint:
            await on_mint(nft_number, f"nft-{nft_number}", "https://cdn.example/i.png")
        return mint_flow.UnitResult(
            nft_number=nft_number,
            nft_id=f"nft-{nft_number}",
            image_url="https://cdn.example/i.png",
            offer_id=f"offer-{nft_number}",
            accept={"xumm_url": "x"},
            error=None,
        )

    counter = {"n": 0}

    async def _alloc():
        counter["n"] += 1
        return 4000 + counter["n"]

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number", _alloc)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _mint_ok)

    j = _job(3)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    assert j.state == bulk_mint_flow.DONE
    assert headroom.reserved_for(_db(tmp_path), f"bulk:{j.id}") == 0
    assert headroom.outstanding(_db(tmp_path)) == 3  # pending until indexed


def test_credit_failure_leaves_unit_retryable(tmp_path, monkeypatch):
    """#226 review (Critical): a unit is terminalized UNIT_FAILED only after
    its mint credit durably commits — a failed credit write leaves the unit
    PENDING (job stays fulfilling/resumable) instead of eating the payment."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MAX_COLLECTION_SIZE", 10000)
    monkeypatch.setattr(bulk_mint_flow.db_path, "app_db_path", lambda net: str(tmp_path / "a.db"))
    # Reservation provably gone -> the credit tail runs immediately.
    monkeypatch.setattr(bulk_mint_flow.headroom, "reserved_for", lambda db, c: 0)

    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bulk_mint_flow.mint_credits, "add_credit", _boom)

    job = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1)
    job.quantity = 1
    job.units = [bulk_mint_flow.Unit(index=0)]
    job.entitlement = bulk_mint_flow.entitlement.PaymentEntitlement(quantity=1)
    asyncio.run(bulk_mint_flow._fulfill_unit(job, job.units[0]))

    assert job.units[0].state == bulk_mint_flow.PENDING  # never terminalized
    assert "credit" in (job.units[0].error or "")


def test_paid_job_exception_stays_resumable(tmp_path, monkeypatch):
    """#226 review (Critical): an unexpected exception after payment must not
    terminalize the job FAILED (pruned, unresumable, paid units stranded) —
    it stays FULFILLING for the startup sweep."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.db_path, "app_db_path", lambda net: str(tmp_path / "a.db"))

    async def _explode(job, unit):
        raise RuntimeError("boom")

    monkeypatch.setattr(bulk_mint_flow, "_fulfill_unit", _explode)
    job = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1)
    job.quantity = 1
    job.units = [bulk_mint_flow.Unit(index=0)]
    job.state = bulk_mint_flow.PAID
    job.paid_at = 12345.0
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(job))

    assert job.state == bulk_mint_flow.FULFILLING  # resumable, not FAILED
    assert len(bulk_mint_flow.load_all_resumable()) == 1
