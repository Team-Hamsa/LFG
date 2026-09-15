import pytest

from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import market_ops, memos
from lfg_core.xrpl_ops import RIPPLE_EPOCH_OFFSET
from tests.closet_market_helpers import (
    BUYER,
    CANCEL_AFTER,
    OTHER,
    SELLER,
    Fakes,
    conn,
    deps,
    escrow_create_tx,
    escrow_node,
    landed,
    make_db,
    open_bid,
    order,
    pending_bid,
    run,
)

EXPIRED_NOW = float(CANCEL_AFTER + RIPPLE_EPOCH_OFFSET + cmf.CANCEL_SLACK_SECONDS + 1)


def _db(tmp_path):
    path = str(tmp_path / "onchain.db")
    make_db(path)
    return path


def test_unsigned_bid_stays_pending(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": False}
    assert (
        run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5))) is None
    )
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW


def test_expired_payload_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLED and "expired" in got["error"]


def test_signer_mismatch_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": OTHER, "txid": "T"}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED


def test_verified_escrow_opens_and_crosses_a_resting_ask(tmp_path):
    path, f = _db(tmp_path), Fakes()
    c = conn(path)
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    c.close()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": BUYER, "txid": "ECH"}
    f.txs["ECH"] = escrow_create_tx(BUYER, "10", seq=7)
    f.escrows[(BUYER, 7)] = escrow_node(BUYER, "10")
    fill_id = run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert (got["escrow_owner_seq"], got["escrow_tx_hash"], got["state"]) == (7, "ECH", cms.MATCHED)
    assert fill_id is not None
    assert order(path, ask["id"])["state"] == cms.MATCHED


def test_amount_mismatch_never_opens(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": BUYER, "txid": "ECH"}
    f.txs["ECH"] = escrow_create_tx(BUYER, "10", amount=market_ops.brix_amount_dict("1"))
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLED and "wrong amount" in got["error"]


def test_open_bid_expiry_cancels_escrow(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f)
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=EXPIRED_NOW)))
    got = order(path, bid["id"])
    assert got["state"] == cms.EXPIRED and got["cancel_refund_hash"] == "CAN"
    assert f.cancels == [(BUYER, 5, f"lfg:closet_cancel:{bid['id']}")]
    assert f.finishes == [] and f.payments == []


def test_user_cancel_before_expiry_finishes_then_refunds(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f, price="10")
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path)))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLED
    assert f.finishes == [(BUYER, 5, "COND", "FULFILL", f"lfg:closet_cancel_finish:{bid['id']}")]
    assert f.payments == [
        (BUYER, "10", f"lfg:closet_cancel_refund:{bid['id']}", memos.ACTION_CLOSET_REFUND)
    ]


def test_unknown_cancel_finish_resolves_from_memo_then_refunds(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    bid = open_bid(path, f)
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    d = deps(path, f, tmp_path)
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["pending_phase"], got["pending_lls"]) == (
        cms.CANCELLING,
        "cancel_finish",
        140,
    )
    run(cmf.advance_bid(bid["id"], d))  # ledger 100 <= lls 140, nothing found: wait
    assert order(path, bid["id"])["pending_phase"] == "cancel_finish" and f.payments == []
    f.found[f"lfg:closet_cancel_finish:{bid['id']}"] = [landed("FINX")]
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["cancel_finish_hash"]) == (cms.CANCELLED, "FINX")
    assert len(f.finishes) == 1 and len(f.payments) == 1


def test_unknown_cancel_finish_absent_past_lls_retries(tmp_path):
    path, f = _db(tmp_path), Fakes(finish_outcomes=["unknown"])
    bid = open_bid(path, f)
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    d = deps(path, f, tmp_path)
    run(cmf.advance_bid(bid["id"], d))
    f.ledger_index = 200  # past lls 140, no memo'd tx: it can never validate
    run(cmf.advance_bid(bid["id"], d))
    run(cmf.advance_bid(bid["id"], d))
    assert len(f.finishes) == 2 and order(path, bid["id"])["state"] == cms.CANCELLED


def test_crash_after_landed_cancel_finish_recovers_from_memo(tmp_path):
    """A crash between the EscrowFinish landing and this process recording
    the hash must not strand the BRIX in the app wallet — the write-ahead
    pending_phase/pending_lls, persisted before submit, is what lets the next
    pass find the landed tx and still send the refund."""
    path, f = _db(tmp_path), Fakes(finish_outcomes=["crash"])
    bid = open_bid(path, f)
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    d = deps(path, f, tmp_path)
    with pytest.raises(RuntimeError):
        run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLING and got["pending_phase"] == "cancel_finish"
    assert got["cancel_finish_hash"] is None
    f.found[f"lfg:closet_cancel_finish:{bid['id']}"] = [landed("FINX")]
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["cancel_finish_hash"]) == (cms.CANCELLED, "FINX")
    assert len(f.finishes) == 1 and len(f.payments) == 1


def test_crash_after_landed_cancel_refund_never_double_pays(tmp_path):
    """Same crash window on the refund Payment: a second attempt must never
    pay twice just because the hash never made it to disk."""
    path, f = _db(tmp_path), Fakes(payment_outcomes=["crash"])
    bid = open_bid(path, f, price="10")
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    d = deps(path, f, tmp_path)
    with pytest.raises(RuntimeError):
        run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLING and got["pending_phase"] == "cancel_refund"
    assert got["cancel_finish_hash"] is not None and got["cancel_refund_hash"] is None
    assert len(f.payments) == 1
    f.found[f"lfg:closet_cancel_refund:{bid['id']}"] = [landed("REFX")]
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["cancel_refund_hash"]) == (cms.CANCELLED, "REFX")
    assert len(f.payments) == 1  # no second payment was ever sent


def test_cancel_finish_intent_recorded_before_crash_retries(tmp_path):
    """A crash after the intent is durably recorded but before the backend
    tx was even submitted: the memo lookup finds nothing, the validated
    ledger has already passed the recorded deadline, so it's decidably
    absent — clear the stale intent and retry cleanly."""
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f)
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    cms.update_order(c, bid["id"], pending_phase="cancel_finish", pending_lls=50)
    c.close()
    f.ledger_index = 100  # already past the recorded (stale) lls of 50
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path)))
    got = order(path, bid["id"])
    assert got["state"] == cms.CANCELLED
    assert len(f.finishes) == 1 and len(f.payments) == 1


def test_cancel_when_escrow_already_gone_sends_nothing(tmp_path):
    """Legitimate case: pending_phase is unset (no backend tx of ours can be
    in flight — the write-ahead branch above always resolves that first) and
    the escrow is simply gone, so the bidder must have EscrowCancel'ed it
    themselves after CancelAfter. Nothing needs to be sent."""
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f)
    assert bid["pending_phase"] is None
    f.escrows.clear()  # the bidder EscrowCancel'ed it themselves after CancelAfter
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED
    assert f.finishes == f.cancels == f.payments == []


def test_open_bid_recrosses_on_sweep(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f, price="10")
    c = conn(path)
    c.execute(
        "INSERT INTO closet_orders (id, side, owner, slot, value, price_brix, state, created_ts, updated_ts) "
        "VALUES ('A1', 'ask', ?, 'Head', 'Crown', '9', 'open', 1, 1)",
        (SELLER,),
    )
    c.commit()
    c.close()
    assert run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path))) is not None


# --- final review C2: find the escrow by condition before "unopened" -----------


def _recoverable_escrow(f, *, owner=BUYER, price="10", seq=7, prev="ECH"):
    f.escrows_by_condition[(owner, "COND")] = escrow_node(
        owner, price, CancelAfter=CANCEL_AFTER, PreviousTxnID=prev
    )
    f.txs[prev] = escrow_create_tx(owner, price, seq=seq)


def test_expired_payload_but_escrow_found_opens_the_bid(tmp_path):
    """(#443 final review C2) The signing status lapsed to expired, but the
    escrow exists on-ledger: open the bid with the sequence recovered from the
    EscrowCreate (PreviousTxnID) instead of untracking a live escrow."""
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    _recoverable_escrow(f, seq=7)
    assert (
        run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5))) is None
    )
    got = order(path, bid["id"])
    assert (got["state"], got["escrow_owner_seq"], got["escrow_tx_hash"]) == (cms.OPEN, 7, "ECH")
    assert f.escrow_lookups == [(BUYER, "COND")]


def test_found_escrow_opens_and_crosses_a_resting_ask(tmp_path):
    path, f = _db(tmp_path), Fakes()
    c = conn(path)
    ask = cms.create_ask(c, owner=SELLER, slot="Head", value="Crown", price_brix="8", platform=None)
    c.close()
    bid = pending_bid(path, price="10")
    _recoverable_escrow(f, seq=9)
    d = deps(path, f, tmp_path, now=bid["created_ts"] + cmf.PAYMENT_GIVEUP_SECONDS)  # status None
    assert run(cmf.advance_bid(bid["id"], d)) is not None
    assert order(path, bid["id"])["state"] == cms.MATCHED
    assert order(path, ask["id"])["state"] == cms.MATCHED


def test_never_validated_and_failed_create_check_for_the_escrow(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": BUYER, "txid": "TX1"}
    f.txs["TX1"] = escrow_create_tx(BUYER, "10", result="tecUNFUNDED")
    _recoverable_escrow(f, seq=11)
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert (got["state"], got["escrow_owner_seq"]) == (cms.OPEN, 11)


def test_escrow_lookup_raising_keeps_the_bid_pending(tmp_path):
    path, f = _db(tmp_path), Fakes(lookup_error=RuntimeError("account_objects down"))
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW


def test_found_escrow_with_wrong_expiry_or_foreign_creator_still_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    f.escrows_by_condition[(BUYER, "COND")] = escrow_node(
        BUYER, "10", CancelAfter=CANCEL_AFTER + 1, PreviousTxnID="ECH"
    )
    f.txs["ECH"] = escrow_create_tx(BUYER, "10")
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED

    bid2 = pending_bid(path, owner=OTHER, price="10")
    f.payload_status[bid2["payload_uuid"]] = {"signed": False, "expired": True}
    _recoverable_escrow(f, owner=OTHER, prev="FOREIGN")
    f.txs["FOREIGN"]["tx_json"]["Account"] = SELLER
    run(cmf.advance_bid(bid2["id"], deps(path, f, tmp_path, now=bid2["created_ts"] + 5)))
    assert order(path, bid2["id"])["state"] == cms.CANCELLED


def test_signer_mismatch_with_no_owner_escrow_still_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path)
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": OTHER, "txid": "T"}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED
    assert f.escrow_lookups == [(BUYER, "COND")]


def test_cancel_unseal_failure_writes_no_intent(tmp_path):
    """(#443 final review I3) the fulfillment is unsealed BEFORE the
    write-ahead intent: a bad enc key must not leave a pending_phase that
    names an EscrowFinish that was never sent."""
    path, f = _db(tmp_path), Fakes()
    bid = open_bid(path, f)
    c = conn(path)
    cms.begin_cancel_bid(c, bid["id"], owner=BUYER, reason="user")
    c.close()
    d = deps(path, f, tmp_path)

    def bad_unseal(blob):
        raise ValueError("InvalidToken")

    d.unseal_fn = bad_unseal
    with pytest.raises(ValueError):
        run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["pending_phase"], got["pending_lls"]) == (cms.CANCELLING, None, None)
    assert f.finishes == []


# --- PR #502 G1: a pinned user LastLedgerSequence gates "nothing landed" ------

USER_LLS = 150


def test_expired_bid_waits_until_the_validated_ledger_passes_user_lls(tmp_path):
    """A Joey row can lapse to `expired` while the client-submitted
    EscrowCreate can still validate up to its pinned LastLedgerSequence."""
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS)
    bid = pending_bid(path, user_lls=USER_LLS)
    assert order(path, bid["id"])["user_lls"] == USER_LLS
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW
    assert f.escrow_lookups == []


def test_expired_bid_past_user_lls_waits_while_the_lookup_lacks_coverage(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS + 1, lookup_coverage=USER_LLS - 1)
    bid = pending_bid(path, user_lls=USER_LLS)
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW
    assert f.escrow_requires == [USER_LLS]


def test_expired_bid_past_user_lls_with_coverage_and_no_escrow_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS + 1)
    bid = pending_bid(path, user_lls=USER_LLS)
    f.payload_status[bid["payload_uuid"]] = {"signed": False, "expired": True}
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED
    assert f.escrow_requires == [USER_LLS]


def test_signer_mismatch_with_user_lls_waits_then_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS)
    bid = pending_bid(path, user_lls=USER_LLS)
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": OTHER, "txid": "T"}
    d = deps(path, f, tmp_path, now=bid["created_ts"] + 5)
    run(cmf.advance_bid(bid["id"], d))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW
    f.ledger_index = USER_LLS + 1
    run(cmf.advance_bid(bid["id"], d))
    assert order(path, bid["id"])["state"] == cms.CANCELLED


def test_escrow_gone_with_user_lls_needs_the_deadline_and_a_covered_lookup(tmp_path):
    """ "The escrow is no longer on-ledger" is a lagging account_objects /
    ledger_entry node as often as a real absence: with user_lls set it cancels
    only past the deadline AND after a covered by-condition lookup finds
    nothing (a found escrow opens the bid instead)."""
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS)
    bid = pending_bid(path, price="10", user_lls=USER_LLS)
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": BUYER, "txid": "ECH"}
    f.txs["ECH"] = escrow_create_tx(BUYER, "10", seq=7)  # validated, but get_escrow -> None
    d = deps(path, f, tmp_path, now=bid["created_ts"] + 5)
    run(cmf.advance_bid(bid["id"], d))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW

    f.ledger_index = USER_LLS + 1
    f.lookup_coverage = USER_LLS - 1
    run(cmf.advance_bid(bid["id"], d))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW

    f.lookup_coverage = None
    _recoverable_escrow(f, seq=7)
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["escrow_owner_seq"]) == (cms.OPEN, 7)

    bid2 = pending_bid(path, owner=OTHER, price="10", user_lls=USER_LLS)
    f.payload_status[bid2["payload_uuid"]] = {"signed": True, "account": OTHER, "txid": "ECH2"}
    f.txs["ECH2"] = escrow_create_tx(OTHER, "10", seq=8)
    run(cmf.advance_bid(bid2["id"], deps(path, f, tmp_path, now=bid2["created_ts"] + 5)))
    assert order(path, bid2["id"])["state"] == cms.CANCELLED
    assert f.escrow_requires[-1] == USER_LLS


def test_escrow_gone_without_user_lls_cancels_as_before(tmp_path):
    path, f = _db(tmp_path), Fakes()
    bid = pending_bid(path, price="10")
    f.payload_status[bid["payload_uuid"]] = {"signed": True, "account": BUYER, "txid": "ECH"}
    f.txs["ECH"] = escrow_create_tx(BUYER, "10", seq=7)
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.CANCELLED
    assert f.escrow_lookups == []


# --- PR #502: a uuid-less pending bid (Xaman unreachable) is found by condition -


def test_uuidless_bid_before_user_lls_waits(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS)
    bid = pending_bid(path, user_lls=USER_LLS, payload_uuid=None)
    _recoverable_escrow(f, seq=7)
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW
    assert f.escrow_lookups == []


def test_uuidless_bid_with_the_escrow_found_by_condition_opens(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS + 1)
    bid = pending_bid(path, user_lls=USER_LLS, payload_uuid=None)
    _recoverable_escrow(f, seq=7)
    run(cmf.advance_bid(bid["id"], deps(path, f, tmp_path, now=bid["created_ts"] + 5)))
    got = order(path, bid["id"])
    assert (got["state"], got["escrow_tx_hash"], got["escrow_owner_seq"]) == (cms.OPEN, "ECH", 7)
    assert f.escrow_requires == [USER_LLS]


def test_uuidless_bid_past_user_lls_with_proven_absence_cancels(tmp_path):
    path, f = _db(tmp_path), Fakes(ledger_index=USER_LLS + 1, lookup_coverage=USER_LLS - 1)
    bid = pending_bid(path, user_lls=USER_LLS, payload_uuid=None)
    d = deps(path, f, tmp_path, now=bid["created_ts"] + 5)
    run(cmf.advance_bid(bid["id"], d))
    assert order(path, bid["id"])["state"] == cms.PENDING_ESCROW  # lookup lacks coverage
    f.lookup_coverage = None
    run(cmf.advance_bid(bid["id"], d))
    got = order(path, bid["id"])
    assert (got["state"], got["cancel_reason"], got["error"]) == (
        cms.CANCELLED,
        "unopened",
        "could not reach Xaman",
    )
