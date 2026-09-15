import json
import os

import pytest

from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core import market_ops, memos
from tests.closet_market_helpers import (
    APP,
    BUYER,
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


def _take(path, f, *, price="5"):
    c = conn(path)
    ask = cms.create_ask(
        c, owner=SELLER, slot="Head", value="Crown", price_brix=price, platform=None
    )
    fl = cms.create_take_fill(c, ask["id"], BUYER, fee_bps=0, platform=None)
    cms.update_fill(c, fl["id"], payload_uuid="PU")
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


def test_take_wrong_invoice_fails_without_refund(tmp_path):
    path, f = _db(tmp_path), Fakes()
    _, fl = _take(path, f)
    f.payload_status["PU"] = {"signed": True, "account": BUYER, "txid": "PAYTX"}
    tx = _payment_tx(fl["id"])
    tx["tx_json"]["InvoiceID"] = "00" * 32
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
