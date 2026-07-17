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
import time  # noqa: E402

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
    # Hermetic consumed-payment ledger (#228): cancel()'s claimed-payment
    # guard and the resume reconciliation read sqlite via config.DB_PATH.
    monkeypatch.setattr(bulk_mint_flow.config, "DB_PATH", str(tmp_path / "app.db"))
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

    async def _no_offers(nft_id, **kw):
        return []  # nothing to adopt (#227) -> falls through to create

    async def _indeterminate_owner(nft_id, **kw):
        return None  # owner lookup failed -> fail closed, fall through to create

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=5000)
    )
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _count_offer)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_nft_sell_offers", _no_offers)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "nft_info", _indeterminate_owner)

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


def _gift_offer(owner, destination, amount="0", expiration=None, offer_index="ADOPTME"):
    return {
        "offer_index": offer_index,
        "amount": amount,
        "destination": destination,
        "flags": 1,
        "owner": owner,
        "expiration": expiration,
    }


def test_resume_minted_unit_adopts_existing_live_offer(tmp_path, monkeypatch):
    """#227: in the crash window where the original offer landed on-chain but
    OFFERED never persisted, resume must adopt that live offer instead of
    creating a second one."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    bot = bulk_mint_flow.xrpl_ops.bot_wallet_address()

    calls = {"mint": 0, "offer": 0}

    async def _count_mint(**kw):
        calls["mint"] += 1
        raise AssertionError("must never mint")

    async def _count_offer(nft_id, destination, **kw):
        calls["offer"] += 1
        return "SHOULD_NOT_BE_CREATED"

    async def _live_offers(nft_id, **kw):
        return [_gift_offer(bot, "rUSER")]

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _count_offer)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_nft_sell_offers", _live_offers)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    j.clamp_to_headroom()
    j.pay_amount = "10"
    j.state = bulk_mint_flow.FULFILLING
    j.units[0].state = bulk_mint_flow.MINTED
    j.units[0].nft_id = "EXISTING_NFT"
    bulk_mint_flow.persist(j)

    resumed = bulk_mint_flow.load_all_resumable()[0]
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert calls["mint"] == 0
    assert calls["offer"] == 0  # adopted, never created a duplicate
    assert resumed.units[0].offer_id == "ADOPTME"
    assert resumed.units[0].state == bulk_mint_flow.OFFERED
    assert resumed.state == bulk_mint_flow.DONE


def test_ensure_offer_ignores_non_matching_offers(monkeypatch):
    """Adoption (#227) requires the exact gift-offer shape create_nft_offer
    emits; foreign owners, wrong destinations, priced offers, and
    expiration-carrying offers must all fall through to a fresh create."""
    bot = bulk_mint_flow.xrpl_ops.bot_wallet_address()

    calls = {"offer": 0}

    async def _count_offer(nft_id, destination, **kw):
        calls["offer"] += 1
        return "FRESH"

    async def _foreign_offers(nft_id, **kw):
        return [
            _gift_offer("rSOMEONEELSE", "rUSER", offer_index="F1"),  # foreign owner
            _gift_offer(bot, "rOTHER", offer_index="F2"),  # wrong destination
            _gift_offer(bot, "rUSER", amount="5000000", offer_index="F3"),  # priced
            _gift_offer(bot, "rUSER", expiration=777, offer_index="F4"),  # expires
        ]

    async def _bot_still_owns(nft_id, **kw):
        return {"nft_id": nft_id, "owner": bot}  # not delivered -> create

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _count_offer)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_nft_sell_offers", _foreign_offers)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "nft_info", _bot_still_owns)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    u = bulk_mint_flow.Unit(index=0, state=bulk_mint_flow.MINTED, nft_id="X")
    asyncio.run(bulk_mint_flow._ensure_offer(j, u))

    assert calls["offer"] == 1
    assert u.offer_id == "FRESH"
    assert u.state == bulk_mint_flow.OFFERED


def test_resume_minted_unit_accepted_offer_marks_delivered(tmp_path, monkeypatch):
    """The other half of the #227 window: the offer landed AND the user
    accepted it while the service was down. The accept consumed the offer
    object (nothing to adopt), and a create can only tec-fail (we no longer
    own the token) — so owner == buyer must be treated as delivered, not
    retried forever (which would wedge the job FULFILLING and 409-lock the
    user's bulk slot on every restart)."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))

    calls = {"mint": 0, "offer": 0}

    async def _count_mint(**kw):
        calls["mint"] += 1
        raise AssertionError("must never mint")

    async def _count_offer(nft_id, destination, **kw):
        calls["offer"] += 1
        return None  # tec: we no longer own the token

    async def _no_offers(nft_id, **kw):
        return []  # accept consumed the offer object

    async def _buyer_owns(nft_id, **kw):
        return {"nft_id": nft_id, "owner": "rUSER"}

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _count_offer)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_nft_sell_offers", _no_offers)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "nft_info", _buyer_owns)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    j.clamp_to_headroom()
    j.pay_amount = "10"
    j.state = bulk_mint_flow.FULFILLING
    j.units[0].state = bulk_mint_flow.MINTED
    j.units[0].nft_id = "EXISTING_NFT"
    bulk_mint_flow.persist(j)

    resumed = bulk_mint_flow.load_all_resumable()[0]
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert calls["mint"] == 0
    assert calls["offer"] == 0  # never retries a doomed create
    assert resumed.units[0].state == bulk_mint_flow.OFFERED
    assert resumed.units[0].error is None
    assert resumed.state == bulk_mint_flow.DONE  # slot frees, job terminal


def test_persist_failure_returns_false_flags_degraded_then_recovers(tmp_path, monkeypatch):
    """#228: persist never raises; a failure flags the job degraded
    (persist_failed, surfaced in to_dict) and the next successful full-record
    write clears it."""
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    real_replace = os.replace
    broken = {"on": True}

    def _replace(src, dst):
        if broken["on"]:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(bulk_mint_flow.os, "replace", _replace)

    assert bulk_mint_flow.persist(j) is False
    assert j.persist_failed is True
    assert j.to_dict()["persist_failed"] is True
    assert bulk_mint_flow.load_all_resumable() == []  # nothing reached disk

    broken["on"] = False
    assert bulk_mint_flow.persist(j) is True
    assert j.persist_failed is False
    assert j.to_dict()["persist_failed"] is False
    assert len(bulk_mint_flow.load_all_resumable()) == 1


def test_final_persist_failure_keeps_job_fulfilling_not_done(tmp_path, monkeypatch):
    """#228: DONE is terminal (pruned, never resumed) — a job whose final
    persist failed must stay FULFILLING so the startup sweep retries the
    write, and the failure must never trigger a mint."""
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)
    for u in j.units:
        u.state = bulk_mint_flow.OFFERED

    async def _no_mint(**kw):
        raise AssertionError("must never mint")

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _no_mint)

    def _replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(bulk_mint_flow.os, "replace", _replace)

    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))

    assert j.state == bulk_mint_flow.FULFILLING  # NOT terminalized
    assert j.persist_failed is True


def test_persist_failure_during_fulfillment_never_remints_or_aborts(tmp_path, monkeypatch):
    """#228: a broken persist during fulfillment degrades durability but the
    job keeps delivering on in-memory state — exactly one mint per pending
    unit, no exception, no terminal DONE while the record can't be written."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow.supply, "remaining_headroom", lambda net: 100)

    calls = {"mint": 0}

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

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=7000)
    )

    def _replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(bulk_mint_flow.os, "replace", _replace)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    j.clamp_to_headroom()
    j.pay_amount = "10"
    j.state = bulk_mint_flow.FULFILLING

    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))

    assert calls["mint"] == 1  # persist failure never re-mints
    assert j.units[0].state == bulk_mint_flow.OFFERED  # delivery completed in-memory
    assert j.state == bulk_mint_flow.FULFILLING  # degraded, not DONE
    assert j.persist_failed is True


def test_awaiting_payment_record_is_resumable(tmp_path, monkeypatch):
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    bulk_mint_flow.persist(j)
    resumable = bulk_mint_flow.load_all_resumable()
    assert [r.id for r in resumable] == [j.id]
    assert resumable[0].state == bulk_mint_flow.AWAITING_PAYMENT


def test_resumed_awaiting_payment_rewatches_without_rebuilding_payload(tmp_path, monkeypatch):
    """#228: resuming an AWAITING_PAYMENT record re-enters ONLY the ledger
    watch — the XUMM payload was built once in prepare_payment, so resume
    must never re-request payment, and the backfill anchor (not_before =
    created_at - 10) must be preserved so a pre-crash payment is found."""
    calls = {"payload": 0}
    captured = {}

    async def _payload(*a, **kw):
        calls["payload"] += 1
        return {"xumm_url": "x", "uuid": "u"}

    async def _wait(**kw):
        captured.update(kw)
        return False

    monkeypatch.setattr(bulk_mint_flow.xumm_ops, "create_payment_payload", _payload)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _wait)

    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    j.payment_link, j.payment_uuid = "https://xumm.app/sign/u1", "u1"
    bulk_mint_flow.persist(j)

    resumed = bulk_mint_flow.load_all_resumable()[0]
    assert resumed.created_at == j.created_at  # anchor survives the round-trip
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert calls["payload"] == 0  # NEVER re-request payment on resume
    assert captured["not_before"] == j.created_at - 10
    assert resumed.state == bulk_mint_flow.PAYMENT_TIMEOUT


def test_expired_awaiting_payment_gets_bounded_grace_not_fresh_wait(tmp_path, monkeypatch):
    """#228 TTL: a record resumed after its payment window elapsed must not
    open a fresh multi-minute wait — only the short bounded grace check
    (which still honours a payment that landed before the crash) — and then
    go PAYMENT_TIMEOUT, persisted terminal."""
    captured = {}

    async def _wait(**kw):
        captured.update(kw)
        return False

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _wait)

    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    j.created_at = time.time() - bulk_mint_flow.config.PAYMENT_TIMEOUT_SECONDS - 3600
    bulk_mint_flow.persist(j)

    resumed = bulk_mint_flow.load_all_resumable()[0]
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert captured["timeout_seconds"] == bulk_mint_flow._EXPIRED_PAYMENT_GRACE_SECONDS
    assert captured["not_before"] == resumed.created_at - 10
    assert resumed.state == bulk_mint_flow.PAYMENT_TIMEOUT
    assert bulk_mint_flow.load_all_resumable() == []  # terminal state persisted


def test_cancel_deletes_awaiting_payment_record(tmp_path, monkeypatch):
    """A cancelled AWAITING_PAYMENT job must drop its durable record so a
    restart can't resurrect it as a live payment watch."""
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    bulk_mint_flow.persist(j)
    assert len(bulk_mint_flow.load_all_resumable()) == 1
    assert j.cancel() is True
    assert j.state == bulk_mint_flow.CANCELLED
    assert bulk_mint_flow.load_all_resumable() == []


def test_resumed_awaiting_payment_honours_preclaimed_payment(tmp_path, monkeypatch):
    """#228: payment_ledger.try_consume commits durably BEFORE the job can
    persist PAID, so a crash in that gap makes the resumed re-watch miss the
    payment (tx-hash dedup). Resume must reconcile via the job's exact
    claimant tag and fulfill — never terminalize PAYMENT_TIMEOUT and keep
    the money."""
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)

    captured = {}

    async def _wait(**kw):
        captured.update(kw)
        return False  # dedup: the pre-crash claim makes the re-watch miss it

    async def _mint(**kw):
        from lfg_core.mint_flow import UnitResult

        return UnitResult(
            nft_number=kw["nft_number"],
            nft_id=f"N{kw['nft_number']}",
            image_url="i",
            offer_id="O",
            accept={"qr_url": "q", "xumm_url": "x", "uuid": "u"},
            error=None,
        )

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _wait)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _mint)
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=6000)
    )

    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    bulk_mint_flow.persist(j)
    # The pre-crash process claimed the K x payment under this job's tag.
    assert bulk_mint_flow.payment_ledger.try_consume(
        "TXHASH1", "rUSER", "rDEST", claimant=j.payment_claimant
    )

    resumed = bulk_mint_flow.load_all_resumable()[0]
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert captured["claimant"] == j.payment_claimant  # tag threaded through
    assert resumed.state == bulk_mint_flow.DONE  # honoured, not timed out
    assert resumed.paid_at is not None


def test_run_job_never_fulfills_terminal_states(tmp_path, monkeypatch):
    """Terminal entry guard: a cancelled (or otherwise terminal) job must
    never wait for payment or mint — closes the cancel-during-prepare race
    where the start handler launches the task after a concurrent cancel
    already landed (state CANCELLED, task None at cancel time)."""
    calls = {"wait": 0, "mint": 0}

    async def _wait(**kw):
        calls["wait"] += 1
        return True

    async def _mint(**kw):
        calls["mint"] += 1
        raise AssertionError("must never mint")

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _wait)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _mint)

    for state in sorted(bulk_mint_flow.TERMINAL_STATES):
        j = _paid_job(tmp_path, monkeypatch, state)
        asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
        assert j.state == state  # untouched
        assert all(u.state == bulk_mint_flow.PENDING for u in j.units)
    assert calls == {"wait": 0, "mint": 0}


def test_cancel_refused_once_payment_claimed(tmp_path, monkeypatch):
    """cancel() racing the gap between the ledger claim committing and PAID
    landing on the job object must refuse: the money is already taken, so
    the in-flight watch must be left to surface PAID and fulfill."""
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    bulk_mint_flow.persist(j)
    assert bulk_mint_flow.payment_ledger.try_consume(
        "TXHASH2", "rUSER", "rDEST", claimant=j.payment_claimant
    )
    assert j.cancel() is False
    assert j.state == bulk_mint_flow.AWAITING_PAYMENT
    assert len(bulk_mint_flow.load_all_resumable()) == 1  # record survives


def test_cancel_tombstones_record_when_delete_fails(tmp_path, monkeypatch):
    """#228 degraded disk: delete_record must never raise mid-cancel (a raise
    would leave a CANCELLED job with a live payment watch). If the delete
    fails, the record is rewritten as a cancelled tombstone so a restart
    can't resurrect the watch."""
    import json as _json

    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    bulk_mint_flow.persist(j)

    def _fail_remove(path):
        raise PermissionError("read-only dir")

    monkeypatch.setattr(bulk_mint_flow.os, "remove", _fail_remove)

    assert j.cancel() is True  # never raises
    assert j.state == bulk_mint_flow.CANCELLED
    assert bulk_mint_flow.load_all_resumable() == []  # tombstone not resumable
    with open(bulk_mint_flow._record_path(j.id)) as f:
        assert _json.load(f)["state"] == bulk_mint_flow.CANCELLED


def test_persist_failure_pauses_before_next_unit(tmp_path, monkeypatch):
    """#228 blast-radius cap: once persists keep failing, at most the unit
    already in flight is at double-mint risk — fulfillment parks FULFILLING
    (non-terminal, resumable) before starting the NEXT unit instead of
    delivering every remaining unit off a stale disk record."""
    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.FULFILLING)  # 3 units
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(bulk_mint_flow, "_PERSIST_RETRY_DELAY_SECONDS", 0)

    calls = {"mint": 0}

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

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _count_mint)
    monkeypatch.setattr(
        bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(start=8000)
    )

    def _replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(bulk_mint_flow.os, "replace", _replace)

    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))

    assert calls["mint"] == 1  # only the in-flight unit delivered
    assert j.units[0].state == bulk_mint_flow.OFFERED
    assert j.units[1].state == bulk_mint_flow.PENDING
    assert j.units[2].state == bulk_mint_flow.PENDING
    assert j.state == bulk_mint_flow.FULFILLING  # parked, resumable
    assert j.error == "durability degraded: fulfillment paused"
    assert j.persist_failed is True


def test_cancel_refuses_on_indeterminate_claim_check(tmp_path, monkeypatch):
    """find_claimed is tri-state: None (ledger read failed) means "unpaid" is
    unprovable, so cancel must refuse rather than risk deleting a paid job."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.payment_ledger, "find_claimed", lambda c: None)
    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    j.clamp_to_headroom()
    assert j.cancel() is False
    assert j.state == bulk_mint_flow.AWAITING_PAYMENT


def test_indeterminate_reconciliation_never_terminalizes(tmp_path, monkeypatch):
    """A failed claim-ledger read during resume reconciliation proves nothing:
    the job must stay awaiting_payment (resumable) — never payment_timeout."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))
    monkeypatch.setattr(bulk_mint_flow.supply, "current_supply", lambda net: 0)

    async def _wait(**kw):
        return False  # re-watch misses (dedup or nothing landed)

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "wait_for_payment", _wait)
    monkeypatch.setattr(bulk_mint_flow.payment_ledger, "find_claimed", lambda c: None)

    j = _paid_job(tmp_path, monkeypatch, bulk_mint_flow.AWAITING_PAYMENT)
    bulk_mint_flow.persist(j)
    resumed = bulk_mint_flow.load_all_resumable()[0]
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(resumed))

    assert resumed.state == bulk_mint_flow.AWAITING_PAYMENT
    # Still on disk and resumable for the next restart's retry.
    assert len(bulk_mint_flow.load_all_resumable()) == 1


def test_ensure_offer_lookup_failure_leaves_unit_minted(tmp_path, monkeypatch):
    """An adopt-scan RPC failure is indeterminate — a live offer may be hiding
    behind the blip, so creating blind could duplicate it. The unit stays
    MINTED for a later resume; create_nft_offer is never called."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path))

    calls = {"offer": 0}

    async def _count_offer(nft_id, destination, **kw):
        calls["offer"] += 1
        return "OFFER123"

    async def _lookup_boom(nft_id, **kw):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "create_nft_offer", _count_offer)
    monkeypatch.setattr(bulk_mint_flow.xrpl_ops, "get_nft_sell_offers", _lookup_boom)

    j = bulk_mint_flow.BulkMintJob("u1", "rUSER", 1, platform="discord")
    j.clamp_to_headroom()
    unit = j.units[0]
    unit.state = bulk_mint_flow.MINTED
    unit.nft_id = "EXISTING_NFT"
    asyncio.run(bulk_mint_flow._ensure_offer(j, unit))

    assert unit.state == bulk_mint_flow.MINTED
    assert "offer lookup failed" in (unit.error or "")
    assert calls["offer"] == 0
