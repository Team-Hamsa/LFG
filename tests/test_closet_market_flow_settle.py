import json
import os

import pytest

from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core import market_ops, memos
from lfg_core.xrpl_ops import RIPPLE_EPOCH_OFFSET
from tests.closet_market_helpers import (
    APP,
    BUYER,
    CANCEL_AFTER,
    OTHER,
    SELLER,
    Fakes,
    conn,
    deps,
    fill,
    landed,
    make_db,
    open_bid,
    run,
)


def _db(tmp_path, seller_count=1):
    path = str(tmp_path / "onchain.db")
    make_db(path, seller_count=seller_count)
    return path


def _holder_fill(path, f, *, price="10", fee_bps=0):
    bid = open_bid(path, f, price=price)
    c = conn(path)
    got = cms.fill_bid(c, bid["id"], SELLER, fee_bps=fee_bps, platform=None)
    c.close()
    return got


def _counts(path):
    c = conn(path)
    try:
        return cms.holding_count(c, SELLER, "Head", "Crown"), cms.holding_count(
            c, BUYER, "Head", "Crown"
        )
    finally:
        c.close()


def test_holder_fill_happy_path(tmp_path):
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f, price="10", fee_bps=700)
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, fee_bps=700))) == cms.MIRRORED
    assert f.finishes == [(BUYER, 5, "COND", "FULFILL", f"lfg:closet_finish:{fl['id']}")]
    assert f.payments == [
        (SELLER, "9.3", f"lfg:closet_forward:{fl['id']}", memos.ACTION_CLOSET_FORWARD)
    ]
    assert sorted(f.mirrored) == [BUYER, SELLER]
    assert _counts(path) == (0, 1)
    got = fill(path, fl["id"])
    assert (got["escrow_finish_hash"], got["forward_tx_hash"]) == ("FIN1", "PAY1")
    journal = json.load(open(os.path.join(tmp_path, "records", f"closet-fill-{fl['id']}.json")))
    assert journal["state"] == cms.MIRRORED


def test_cross_refunds_overshoot_to_the_bidder(tmp_path):
    path, f = _db(tmp_path), Fakes()
    c = conn(path)
    cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    c.close()
    bid = open_bid(path, f, price="10")
    c = conn(path)
    fl = cms.cross_incoming(c, bid["id"], fee_bps=0)
    c.close()
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path))) == cms.MIRRORED
    assert [(p[0], p[1]) for p in f.payments] == [(SELLER, "8"), (BUYER, "2")]
    assert f.payments[1][3] == memos.ACTION_CLOSET_REFUND


def test_precheck_blocks_finish_when_buyer_has_no_closet(tmp_path):
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f)
    c = conn(path)
    es.set_closet_status(c, BUYER, ct.PENDING_ACCEPT)
    c.close()
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path))) == cms.FAILED
    assert f.finishes == []
    c = conn(path)
    assert cms.get_order(c, fl["bid_order_id"])["state"] == cms.CANCELLING
    c.close()


def test_unknown_finish_waits_then_resolves_landed(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.INDETERMINATE
    c = conn(path)
    assert cms.available_count(c, SELLER, "Head", "Crown") == 0  # still encumbered while unknown
    c.close()
    f.found[f"lfg:closet_finish:{fl['id']}"] = [landed("FINX")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.finishes) == 1 and fill(path, fl["id"])["escrow_finish_hash"] == "FINX"


def test_unknown_finish_absent_past_lls_is_retried(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    fl = _holder_fill(path, f)  # an unknown finish leaves the fake escrow in place
    d = deps(path, f, tmp_path)
    run(cmf.settle_fill(fl["id"], d))
    f.ledger_index = 500
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.finishes) == 2


def test_failed_finish_with_escrow_gone_fails_the_fill(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["failed"])
    fl = _holder_fill(path, f)
    f.escrows.clear()
    assert run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path))) == cms.FAILED
    c = conn(path)
    assert cms.get_order(c, fl["bid_order_id"])["state"] == cms.EXPIRED
    assert cms.available_count(c, SELLER, "Head", "Crown") == 1  # seller's unit released
    c.close()


def test_forward_failure_retries_without_moving_twice(tmp_path):
    path, f = _db(tmp_path), Fakes(payment_outcomes=["failed"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.ASSET_MOVED
    assert fill(path, fl["id"])["attempts"] == 1
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert _counts(path) == (0, 1) and len(f.payments) == 2


def test_unknown_forward_keeps_owner_unmirrored_until_resolved(tmp_path):
    path, f = _db(tmp_path), Fakes(payment_outcomes=["unknown"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.INDETERMINATE
    c = conn(path)
    assert cms.has_unmirrored_fill(c, SELLER)
    c.close()
    f.found[f"lfg:closet_forward:{fl['id']}"] = [landed("FWD")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.payments) == 1 and fill(path, fl["id"])["forward_tx_hash"] == "FWD"


def test_mirror_failure_stays_paid_and_retries(tmp_path):
    path, f = _db(tmp_path), Fakes(mirror_error=ct.ClosetIndeterminateError("boom"))
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.PAID
    f.mirror_error = None
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED


def _take(path, f, *, price="5", user_lls=None):
    c = conn(path)
    ask = cms.create_ask(
        c, owner=SELLER, slot="Head", value="Crown", price_brix=price, platform=None
    )
    fl = cms.create_take_fill(c, ask["id"], BUYER, fee_bps=0, platform=None)
    cms.update_fill(c, fl["id"], payload_uuid="PU", user_lls=user_lls)
    fl = cms.get_fill(c, fl["id"])
    c.close()
    return ask, fl


def _payment_tx(fill_id, *, account=BUYER, delivered="5", destination=APP):
    return {
        "validated": True,
        "hash": "PAYTX",
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": market_ops.brix_amount_dict(delivered),
        },
        "tx_json": {
            "TransactionType": "Payment",
            "Account": account,
            "Destination": destination,
            "InvoiceID": cms.invoice_id(fill_id),
            "Amount": market_ops.brix_amount_dict("5"),
        },
    }


def test_take_happy_path(tmp_path):
    path, f = _db(tmp_path), Fakes()
    ask, fl = _take(path, f)
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING  # unsigned
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert f.payments == [
        (SELLER, "5", f"lfg:closet_forward:{fl['id']}", memos.ACTION_CLOSET_FORWARD)
    ]
    assert _counts(path) == (0, 1)


def test_take_signed_by_another_account_refunds_that_account(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": OTHER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"], account=OTHER)
    assert (
        run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10)))
        == cms.REFUNDED
    )
    assert f.payments == [(OTHER, "5", f"lfg:closet_refund:{fl['id']}", memos.ACTION_CLOSET_REFUND)]
    assert _counts(path) == (1, 0)


def test_take_partial_delivery_refunds_what_arrived(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"], delivered="4.5")
    assert (
        run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10)))
        == cms.REFUNDED
    )
    assert f.payments[0][:2] == (BUYER, "4.5")


def test_take_wrong_invoice_that_delivered_brix_refunds_the_sender(tmp_path):
    """(#443 final review I4) A validated payment to the app wallet that
    delivered BRIX but isn't this fill's invoice is still money we hold —
    refund it to its sender instead of failing with nothing returned."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    tx = _payment_tx(fl["id"], delivered="4")
    tx["tx_json"]["InvoiceID"] = "00" * 32
    f.txs["PAYTX"] = tx
    assert (
        run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10)))
        == cms.REFUNDED
    )
    assert f.payments == [
        (BUYER, "4", cms.memo_tag("refund", fl["id"]), memos.ACTION_CLOSET_REFUND)
    ]
    assert _counts(path) == (1, 0)


def test_take_wrong_type_with_no_brix_still_fails_without_refund(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    tx = _payment_tx(fl["id"])
    tx["tx_json"]["InvoiceID"] = "00" * 32
    tx["meta"]["delivered_amount"] = "1000000"  # XRP, not BRIX
    f.txs["PAYTX"] = tx
    assert (
        run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10)))
        == cms.FAILED
    )
    assert f.payments == []


def test_take_after_ask_sold_refunds(tmp_path):
    path, f = _db(tmp_path), Fakes()
    ask, fl = _take(path, f)
    c = conn(path)
    cms.cancel_ask(c, ask["id"], SELLER)
    c.close()
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    assert (
        run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10)))
        == cms.REFUNDED
    )
    assert f.payments[0][:2] == (BUYER, "5")


def test_take_payload_expiry_fails_quietly(tmp_path):
    path, f = _db(tmp_path), Fakes()
    ask, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    assert (
        run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 10)))
        == cms.FAILED
    )
    c = conn(path)
    assert cms.get_order(c, ask["id"])["state"] == cms.OPEN
    c.close()


# --- write-ahead-intent crash recovery (task-9 addendum) -----------------------


def test_crash_after_landed_finish_recovers_from_memo(tmp_path):
    """A crash between the EscrowFinish landing and this process recording
    the hash must not leave the fill stuck or double-send: the write-ahead
    pending_phase/pending_lls, persisted before submit, is what lets the next
    pass find the landed tx and continue settlement from there."""
    path, f = _db(tmp_path), Fakes(finish_outcomes=["crash"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    with pytest.raises(RuntimeError):
        run(cmf.settle_fill(fl["id"], d))
    got = fill(path, fl["id"])
    assert got["state"] == cms.FUNDS_PENDING and got["pending_phase"] == "finish"
    assert got["escrow_finish_hash"] is None
    f.found[f"lfg:closet_finish:{fl['id']}"] = [landed("FINX")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.finishes) == 1 and len(f.payments) == 1
    assert fill(path, fl["id"])["escrow_finish_hash"] == "FINX"
    assert _counts(path) == (0, 1)


def test_crash_after_landed_forward_recovers_from_memo(tmp_path):
    """Same crash window on the forward Payment: a second pass must find the
    landed tx and never send a second forward payment."""
    path, f = _db(tmp_path), Fakes(payment_outcomes=["crash"])
    fl = _holder_fill(path, f)
    d = deps(path, f, tmp_path)
    with pytest.raises(RuntimeError):
        run(cmf.settle_fill(fl["id"], d))
    got = fill(path, fl["id"])
    assert got["state"] == cms.ASSET_MOVED and got["pending_phase"] == "forward"
    assert got["forward_tx_hash"] is None
    assert len(f.payments) == 1
    f.found[f"lfg:closet_forward:{fl['id']}"] = [landed("FWD")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.payments) == 1  # no second forward payment was ever sent
    assert fill(path, fl["id"])["forward_tx_hash"] == "FWD"


def test_crash_after_landed_refund_recovers_from_memo(tmp_path):
    """Same crash window on a refund Payment (a take signed by a different
    account than the fill's buyer)."""
    path, f = _db(tmp_path), Fakes(payment_outcomes=["crash"])
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": OTHER, "txid": "PAYTX"}
    f.txs["PAYTX"] = _payment_tx(fl["id"], account=OTHER)
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    with pytest.raises(RuntimeError):
        run(cmf.settle_fill(fl["id"], d))
    got = fill(path, fl["id"])
    assert got["state"] == cms.REFUND_PENDING and got["pending_phase"] == "refund"
    assert got["refund_tx_hash"] is None
    assert len(f.payments) == 1
    f.found[cms.memo_tag("refund", fl["id"])] = [landed("REFX")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.REFUNDED
    assert len(f.payments) == 1  # no second refund payment was ever sent
    assert fill(path, fl["id"])["refund_tx_hash"] == "REFX"


def test_finish_intent_recorded_before_crash_retries(tmp_path):
    """The intent was durably written but the process died before the
    backend tx was even submitted: the memo lookup finds nothing, and the
    validated ledger has already passed the recorded (stale) deadline, so
    it's decidably absent — clear the stale intent and submit exactly once."""
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f)
    c = conn(path)
    cms.update_fill(c, fl["id"], pending_phase="finish", pending_lls=50)
    c.close()
    f.ledger_index = 100  # already past the recorded (stale) lls of 50
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert len(f.finishes) == 1 and len(f.payments) == 1


# --- review fix round 1: money-safety fixes ------------------------------------


def test_finish_refused_inside_expiry_guard_window(tmp_path):
    """(#443 review fix) A fill against a just-expired bid must not get stuck
    forever: past CancelAfter the ledger rejects EscrowFinish outright (tec*),
    so attempting it would just burn a doomed submit while the seller's unit
    stays encumbered and the bid sits `matched` (invisible to the expiry
    sweep). Route it straight to cancelling instead."""
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f)
    now = RIPPLE_EPOCH_OFFSET + CANCEL_AFTER - 100  # inside the 300s guard window
    d = deps(path, f, tmp_path, now=now)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED
    assert f.finishes == []
    c = conn(path)
    bid = cms.get_order(c, fl["bid_order_id"])
    assert bid["state"] == cms.CANCELLING and bid["cancel_reason"] == "expired"
    assert cms.available_count(c, SELLER, "Head", "Crown") == 1  # never encumbered
    c.close()


def test_failed_finish_with_node_present_past_cancel_after_aborts(tmp_path):
    """(#443 review fix) The pre-submit guard read passes (still inside the
    window), but real wall-clock time advances past CancelAfter while the
    EscrowFinish is in flight and comes back `failed` with the escrow node
    still present — the same abort-to-cancelling applies, keyed on a fresh
    read of `now`, not the stale one from the guard check."""
    path, f = _db(tmp_path), Fakes(finish_outcomes=["failed"])
    fl = _holder_fill(path, f)
    before = RIPPLE_EPOCH_OFFSET + CANCEL_AFTER - (cmf.FINISH_GUARD_SECONDS + 1)
    after = RIPPLE_EPOCH_OFFSET + CANCEL_AFTER + 1
    clock = iter([before, after])
    d = deps(path, f, tmp_path)
    d.now_fn = lambda: next(clock)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED
    assert len(f.finishes) == 1
    c = conn(path)
    bid = cms.get_order(c, fl["bid_order_id"])
    assert bid["state"] == cms.CANCELLING and bid["cancel_reason"] == "expired"
    c.close()


def test_take_none_status_past_old_window_still_waits(tmp_path):
    """(#443 review fix) A None payload status (XUMM API error / 429) must
    never be treated like an explicit expiry, no matter how long it's been —
    `signing_wait_seconds` plays no role in this branch any more."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 3700)  # past the old window
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


def test_take_signed_tx_not_found_for_a_long_time_still_waits(tmp_path):
    """(#443 review fix) A signed tx that the lookup can't find yet (no
    LastLedgerSequence to judge absence against) must keep waiting, however
    long it's been — never declared "never validated" on a timer."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    # no f.txs["PAYTX"] entry -> get_tx_fn's default {"validated": False}, no LastLedgerSequence
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 3700)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


def test_take_unvalidated_past_last_ledger_sequence_fails(tmp_path):
    """(#443 review fix) Once found but unvalidated, failure is decided by the
    validated ledger passing the tx's OWN LastLedgerSequence — not a wall-clock
    timer."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = {"validated": False, "tx_json": {"LastLedgerSequence": 50}}
    f.ledger_index = 100  # already past the tx's own deadline
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED


def test_phase_outcome_uses_prescan_index_not_a_later_one(tmp_path):
    """(#443 review fix) Reading the ledger index AFTER the memo scan let a tx
    that validates in between look "absent" and get resubmitted (double pay).
    The fix reads the index BEFORE scanning: `find_txs_fn` here simulates the
    ledger advancing past `lls` WHILE the scan runs (as a real account_tx
    round-trip could) — if the index is read after the scan, that later value
    (200) wrongly clears the intent and resubmits; read before, only the
    earlier value (100) is ever consulted and the fill correctly keeps
    waiting."""
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f)
    c = conn(path)
    cms.update_fill(c, fl["id"], state=cms.INDETERMINATE, pending_phase="finish", pending_lls=140)
    c.close()
    clock = {"value": 100}
    d = deps(path, f, tmp_path)

    async def index_fn():
        return clock["value"]

    async def find_txs_fn(tag, lls):
        clock["value"] = 200  # the ledger advances past lls during the scan itself
        return []

    d.ledger_index_fn = index_fn
    d.find_txs_fn = find_txs_fn
    assert run(cmf.settle_fill(fl["id"], d)) == cms.INDETERMINATE
    got = fill(path, fl["id"])
    assert got["pending_phase"] == "finish" and got["escrow_finish_hash"] is None
    assert f.finishes == []  # never resubmitted


def test_intent_not_yet_past_lls_waits_without_resubmitting(tmp_path):
    """An intent whose lls has not passed yet must simply wait — no resubmit,
    no state change."""
    path, f = _db(tmp_path), Fakes()
    fl = _holder_fill(path, f)
    c = conn(path)
    cms.update_fill(c, fl["id"], pending_phase="finish", pending_lls=140)
    c.close()
    f.ledger_index = 100  # has not passed the recorded lls yet
    d = deps(path, f, tmp_path)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    assert f.finishes == []


# --- review fix round 2: _take_funds race + 24h giveup -------------------------


def test_take_race_uses_preread_index_not_a_later_one(tmp_path):
    """(#443 review fix round 2) `_take_funds` had the same read-order race as
    `_phase_outcome`: get_tx_fn is called, THEN the ledger index is read. Here
    get_tx_fn itself simulates the ledger advancing past the tx's own
    LastLedgerSequence WHILE the lookup runs (as a real round-trip could) — if
    the index were read after the lookup, that later value (200) would wrongly
    fail a fill whose BRIX had already arrived; read before, only the earlier
    value (140, still under the LLS of 150) is ever consulted."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    clock = {"value": 140}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)

    async def index_fn():
        return clock["value"]

    async def get_tx_fn(tx_hash):
        clock["value"] = 200  # the ledger advances past the tx's LLS during the lookup itself
        return {"validated": False, "tx_json": {"LastLedgerSequence": 150}}

    d.ledger_index_fn = index_fn
    d.get_tx_fn = get_tx_fn
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


def test_take_recheck_finds_validated_is_not_failed(tmp_path):
    """(#443 review fix round 2) The pre-read index is legitimately past the
    tx's LLS, but a re-check (one extra lookup) finds it actually validated in
    the gap — that must not fail the fill; the next pass processes it
    normally."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.ledger_index = 100
    calls = {"n": 0}

    async def get_tx_fn(tx_hash):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"validated": False, "tx_json": {"LastLedgerSequence": 50}}
        return _payment_tx(fl["id"])

    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    d.get_tx_fn = get_tx_fn
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    assert calls["n"] == 2  # first lookup + one re-check, no fail
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED


def test_take_none_status_never_fails_even_past_giveup(tmp_path):
    """(#443 review fix round 3) A `None` payload status — an XUMM API error,
    a 429 with an empty cache (always true right after a restart) — is NEVER
    a failure signal, no matter how old the fill is. Only a definite,
    non-None answer (unsigned, or signed-with-no-txid) can give up."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


def test_take_none_status_just_under_giveup_still_waits(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 23 * 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


def test_take_unsigned_status_beyond_giveup_fails(tmp_path):
    """(#443 review fix round 3) A non-None, explicitly-unsigned status past
    the giveup DOES fail — unlike a None read, this is a definite answer."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED


def test_take_signed_no_txid_beyond_giveup_fails(tmp_path):
    """(#443 review fix round 3, folded minor) A signed-but-no-txid-yet
    status is also a definite (non-None) answer, so it must not poll forever
    either."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True}  # no "txid" key
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED


def test_take_too_busy_error_never_fails_even_past_giveup(tmp_path):
    """(#443 review fix round 3) A bare rippled error body that ISN'T
    txnNotFound (tooBusy, slowDown, ...) must never be treated as absence —
    only a confirmed txnNotFound-on-both-reads can give up post-sign."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = {"error": "tooBusy"}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


def test_take_txnnotfound_then_toobusy_recheck_still_waits(tmp_path):
    """(#443 review fix round 3) txnNotFound on the initial read triggers the
    giveup re-check, but if THAT re-fetch comes back some other error
    (tooBusy here, e.g. a partial-history node lagging behind another that
    briefly saw the tx as not-found), the giveup does not fire — both reads
    must agree it's txnNotFound."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    calls = {"n": 0}

    async def get_tx_fn(tx_hash):
        calls["n"] += 1
        return {"error": "txnNotFound"} if calls["n"] == 1 else {"error": "tooBusy"}

    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    d.get_tx_fn = get_tx_fn
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    assert calls["n"] == 2


def test_take_txnnotfound_both_reads_beyond_giveup_fails(tmp_path):
    """(#443 review fix round 3) Only when BOTH the initial lookup and the
    one re-fetch agree `error == "txnNotFound"`, past the giveup, does the
    fill fail."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = {"error": "txnNotFound"}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED


def test_take_not_found_tx_just_under_giveup_still_waits(tmp_path):
    """A bare `{"validated": False}` (no "error" key — the Fakes' generic
    not-yet-seen shape) never fails regardless of age: there's no
    LastLedgerSequence AND no txnNotFound to agree on."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 23 * 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


# --- final review C2: reconcile on-ledger before "nothing arrived" -------------


def _invoice_entry(fill_id, tx_hash="PAYTX"):
    return {
        "validated": True,
        "hash": tx_hash,
        "meta": {"TransactionResult": "tesSUCCESS"},
        "tx_json": {"TransactionType": "Payment", "InvoiceID": cms.invoice_id(fill_id)},
    }


def test_take_expired_status_but_invoice_payment_found_settles(tmp_path):
    """(#443 final review C2) A Joey user who signs and closes the tab leaves
    the sign row lapsing to "expired" while the client-submitted Payment
    validated — look it up by InvoiceID before writing the buy off."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    f.found_invoices[cms.invoice_id(fl["id"])] = [_invoice_entry(fl["id"])]
    now = fl["created_ts"] + 30
    d = deps(path, f, tmp_path, now=now)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING  # tx not readable yet
    got = fill(path, fl["id"])
    assert got["state"] != cms.FAILED and got["signed_txid"] == "PAYTX"
    # min_ledger = validated index - elapsed/3s - 1000 slack, floored at 1
    assert f.invoice_lookups[0] == (cms.invoice_id(fl["id"]), 1)
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert _counts(path) == (0, 1)


def test_take_invoice_lookup_min_ledger_estimate(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=90_000)
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    run(cmf.settle_fill(fl["id"], deps(path, f, tmp_path, now=fl["created_ts"] + 3000)))
    assert f.invoice_lookups == [(cms.invoice_id(fl["id"]), 90_000 - 1000 - 1000)]
    assert fill(path, fl["id"])["state"] == cms.FAILED  # nothing found: today's behavior


def test_take_giveup_but_invoice_payment_found_is_not_failed(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False}
    f.found_invoices[cms.invoice_id(fl["id"])] = [_invoice_entry(fl["id"])]
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED


def test_take_post_sign_giveup_adopts_the_found_invoice_payment(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "WRONGTXID"}
    f.txs["WRONGTXID"] = {"error": "txnNotFound"}
    f.found_invoices[cms.invoice_id(fl["id"])] = [_invoice_entry(fl["id"])]
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    assert fill(path, fl["id"])["payment_tx_hash"] == "PAYTX"


def test_take_lls_passed_but_invoice_payment_found_is_not_failed(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = {"validated": False, "tx_json": {"LastLedgerSequence": 50}}
    f.found_invoices[cms.invoice_id(fl["id"])] = [_invoice_entry(fl["id"])]  # same hash
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING  # the tx server lags: wait
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED


def test_take_invoice_lookup_raising_waits(tmp_path):
    path, f = _db(tmp_path), Fakes(lookup_error=RuntimeError("account_tx down"))
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS + 3600)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = {"error": "txnNotFound"}
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING


def test_take_ignores_a_failed_invoice_payment(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    entry = _invoice_entry(fl["id"])
    entry["meta"]["TransactionResult"] = "tecPATH_PARTIAL"
    f.found_invoices[cms.invoice_id(fl["id"])] = [entry]
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED


# --- PR #502 G1: a pinned user LastLedgerSequence gates "nothing arrived" -----

USER_LLS = 150


def test_take_expired_before_user_lls_waits_without_a_lookup(tmp_path):
    """A Joey `expired` row (or a late WalletConnect approval) can still
    validate up to the Payment's pinned LastLedgerSequence."""
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS)
    _, fl = _take(path, f, user_lls=USER_LLS)
    assert fl["user_lls"] == USER_LLS
    f.payload_status["PU"] = {"signed": False, "expired": True}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    assert f.invoice_lookups == []


def test_take_expired_past_user_lls_waits_while_the_lookup_lacks_coverage(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS + 1, lookup_coverage=USER_LLS - 1)
    _, fl = _take(path, f, user_lls=USER_LLS)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    assert f.invoice_requires == [USER_LLS]


def test_take_expired_past_user_lls_with_coverage_and_nothing_found_fails(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS + 1)
    _, fl = _take(path, f, user_lls=USER_LLS)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED
    assert f.invoice_requires == [USER_LLS]


def test_take_never_validated_with_user_lls_needs_the_gate(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS)
    _, fl = _take(path, f, user_lls=USER_LLS)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    f.txs["PAYTX"] = {"validated": False, "tx_json": {"LastLedgerSequence": 50}}
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    f.ledger_index = USER_LLS + 1
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED


def test_take_validated_failure_with_user_lls_looks_past_the_tracked_tx(tmp_path):
    """A validated tec (or wrong-shape) tracked tx is not proof nothing else
    landed: with user_lls set it waits for the deadline, then a covered
    InvoiceID lookup — which ignores the tracked hash — decides."""
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS)
    _, fl = _take(path, f, user_lls=USER_LLS)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "TECTX"}
    tec = _payment_tx(fl["id"])
    tec["hash"] = "TECTX"
    tec["meta"]["TransactionResult"] = "tecPATH_DRY"
    f.txs["TECTX"] = tec
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FUNDS_PENDING
    f.ledger_index = USER_LLS + 1
    f.found_invoices[cms.invoice_id(fl["id"])] = [_invoice_entry(fl["id"], tx_hash="TECTX")]
    assert run(cmf.settle_fill(fl["id"], d)) == cms.FAILED  # only the tracked hash: absent

    path2 = str(tmp_path / "second.db")
    make_db(path2)
    f2 = Fakes(ledger_index=USER_LLS + 1)
    _, fl2 = _take(path2, f2, user_lls=USER_LLS)
    f2.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "TECTX"}
    f2.txs["TECTX"] = tec
    f2.found_invoices[cms.invoice_id(fl2["id"])] = [_invoice_entry(fl2["id"])]
    f2.txs["PAYTX"] = _payment_tx(fl2["id"])
    d2 = deps(path2, f2, tmp_path, now=fl2["created_ts"] + 10)
    assert run(cmf.settle_fill(fl2["id"], d2)) == cms.MIRRORED


# --- PR #502 G2: a public InvoiceID must not let junk mask the real payment -----


def _paying_entry(fill_id, *, tx_hash, account=BUYER, delivered="5"):
    entry = _invoice_entry(fill_id, tx_hash=tx_hash)
    entry["tx_json"].update(Account=account, Destination=APP)
    entry["meta"]["delivered_amount"] = market_ops.brix_amount_dict(delivered)
    return entry


def test_take_reconcile_prefers_the_buyers_full_payment_over_junk(tmp_path):
    """Anyone can send the app wallet a Payment carrying this fill's InvoiceID.
    A junk entry (wrong sender / underpaid) listed first must not be adopted
    over the buyer's own full payment."""
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    junk_sender = _paying_entry(fl["id"], tx_hash="JUNK1", account=OTHER)
    junk_short = _paying_entry(fl["id"], tx_hash="JUNK2", delivered="1")
    real = _paying_entry(fl["id"], tx_hash="PAYTX")
    f.found_invoices[cms.invoice_id(fl["id"])] = [junk_sender, junk_short, real]
    f.txs["PAYTX"] = _payment_tx(fl["id"])
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    assert run(cmf.settle_fill(fl["id"], d)) == cms.MIRRORED
    got = fill(path, fl["id"])
    assert (got["signed_txid"], got["payment_tx_hash"]) == ("PAYTX", "PAYTX")
    assert _counts(path) == (0, 1)


def test_take_reconcile_with_only_junk_refunds_the_junk_sender(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": False, "expired": True}
    f.found_invoices[cms.invoice_id(fl["id"])] = [
        _paying_entry(fl["id"], tx_hash="JUNK1", account=OTHER, delivered="2")
    ]
    junk_tx = _payment_tx(fl["id"], account=OTHER, delivered="2")
    junk_tx["hash"] = "JUNK1"
    f.txs["JUNK1"] = junk_tx
    d = deps(path, f, tmp_path, now=fl["created_ts"] + 10)
    f.payment_outcomes = ["unknown"]  # stop right after the refund is sent
    run(cmf.settle_fill(fl["id"], d))
    got = fill(path, fl["id"])
    assert (got["signed_txid"], got["refund_to"], got["refund_brix"]) == ("JUNK1", OTHER, "2")
    assert f.payments[0][:2] == (OTHER, "2")
    assert _counts(path) == (1, 0)
