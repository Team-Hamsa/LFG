"""Fee-cover store (spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md)."""

from __future__ import annotations

import sqlite3

import pytest

from lfg_core import fee_cover_store as store

NET = "testnet"
KNOBS = store.Knobs(
    coverage_bps=10_000,
    budget_drops=50_000_000,
    wallet_cap_drops=5_000_000,
    min_bid_drops=1_000_000,
)


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "app.db"))
    yield c
    c.close()


def _audit_rows(conn):
    return [
        dict(r)
        for r in conn.execute("SELECT action, result, actor FROM fee_cover_audit ORDER BY id")
    ]


def test_start_creates_a_live_campaign_with_the_knobs(conn):
    campaign, result = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    assert result == "started"
    assert campaign.status == "active"
    assert campaign.coverage_bps == 10_000
    assert campaign.budget_drops == 50_000_000
    assert campaign.wallet_cap_drops == 5_000_000
    assert campaign.min_bid_drops == 1_000_000
    assert campaign.wallet_window_seconds == store.DEFAULT_WALLET_WINDOW_SECONDS
    assert campaign.ends_at is None
    assert store.live_campaign(conn, NET, now=2000) == campaign
    assert _audit_rows(conn) == [{"action": "start", "result": "started", "actor": "discord:1"}]


def test_start_twice_is_idempotent_and_audited(conn):
    first, _ = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    second, result = store.start_campaign(
        conn, network=NET, actor="discord:2", knobs=KNOBS, duration_seconds=None, now=1001
    )
    assert result == "already_active"
    assert second.id == first.id
    assert [r["result"] for r in _audit_rows(conn)] == ["started", "already_active"]


def test_expired_campaign_is_not_live_and_start_replaces_it(conn):
    old, _ = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=100, now=1000
    )
    assert old.ends_at == 1100
    assert store.live_campaign(conn, NET, now=1099) is not None
    assert store.live_campaign(conn, NET, now=1100) is None
    new, result = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1200
    )
    assert result == "started"
    assert new.id != old.id
    retired = store.get_campaign(conn, old.id)
    assert retired is not None and retired.status == "stopped" and retired.stopped_by == "system"


def test_update_changes_knobs_only_on_a_live_campaign(conn):
    missing, result = store.update_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, now=1000
    )
    assert (missing, result) == (None, "no_live_campaign")
    store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    new_knobs = store.Knobs(
        coverage_bps=5_000, budget_drops=10_000_000, wallet_cap_drops=2_000_000, min_bid_drops=0
    )
    updated, result = store.update_campaign(
        conn, network=NET, actor="discord:1", knobs=new_knobs, now=1001
    )
    assert result == "updated"
    assert updated is not None
    assert (
        updated.coverage_bps,
        updated.budget_drops,
        updated.wallet_cap_drops,
        updated.min_bid_drops,
    ) == (5_000, 10_000_000, 2_000_000, 0)


def test_stop_then_stop_again(conn):
    started, _ = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    stopped, result = store.stop_campaign(conn, network=NET, actor="discord:2", now=1500)
    assert result == "stopped"
    assert stopped is not None and stopped.id == started.id
    assert (
        stopped.status == "stopped"
        and stopped.stopped_by == "discord:2"
        and stopped.stopped_at == 1500
    )
    again, result = store.stop_campaign(conn, network=NET, actor="discord:2", now=1600)
    assert result == "already_inactive"
    assert again is not None and again.id == started.id
    assert store.live_campaign(conn, NET, now=1700) is None


@pytest.mark.parametrize(
    "knobs",
    [
        store.Knobs(coverage_bps=10_001, budget_drops=1, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=-1, budget_drops=1, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=0, wallet_cap_drops=1, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=1, wallet_cap_drops=0, min_bid_drops=0),
        store.Knobs(coverage_bps=100, budget_drops=1, wallet_cap_drops=1, min_bid_drops=-1),
    ],
)
def test_invalid_knobs_are_rejected(conn, knobs):
    with pytest.raises(ValueError):
        store.start_campaign(
            conn, network=NET, actor="discord:1", knobs=knobs, duration_seconds=None, now=1000
        )


def test_blank_actor_and_bad_duration_are_rejected(conn):
    with pytest.raises(ValueError):
        store.start_campaign(
            conn, network=NET, actor="  ", knobs=KNOBS, duration_seconds=None, now=1000
        )
    with pytest.raises(ValueError):
        store.start_campaign(
            conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=0, now=1000
        )


def test_sqlite_allows_one_active_campaign_per_network(conn):
    store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fee_cover_campaigns (network, status, coverage_bps, budget_drops, wallet_cap_drops,"
            " min_bid_drops, started_at, started_by) VALUES (?, 'active', 1, 1, 1, 0, 1, 'x')",
            (NET,),
        )
    conn.rollback()  # the failed INSERT left Python's implicit transaction open
    # a different network is independent
    store.start_campaign(
        conn, network="mainnet", actor="discord:1", knobs=KNOBS, duration_seconds=None, now=1000
    )


# --- Task 2: promises, refunds, payout transitions ------------------------

BIDDER = "rBidder1111111111111111111111111"
SELLER = "rSeller1111111111111111111111111"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"


def _live(conn, *, budget=1_000_000, cap=500_000, now=1000, min_bid=0):
    knobs = store.Knobs(
        coverage_bps=10_000, budget_drops=budget, wallet_cap_drops=cap, min_bid_drops=min_bid
    )
    campaign, _ = store.start_campaign(
        conn, network=NET, actor="discord:1", knobs=knobs, duration_seconds=None, now=now
    )
    return campaign


# What `_inp()`'s bid derives at 100% coverage: ceil(5_075_000 * 0.01589).
PROMISE = 80_642


def _inp(offer_index="BID1", bidder=BIDDER, bid=5_075_000):
    return store.PromiseInput(
        offer_index=offer_index,
        network=NET,
        nft_id="NFT1",
        bidder=bidder,
        bid_drops=bid,
        ask_drops=4_990_000,
        broker=CAFE,
        broker_rate=0.01589,
        bid_expiration=900_000_000,
    )


def _owed(accept="ACCEPT1", refund=80_642):
    return store.RefundDecision(accept, SELLER, CAFE, 80_642, 349_605, refund, None)


def test_record_promise_opens_and_reserves_budget(conn):
    c = _live(conn)
    row = store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    assert row is not None and row["state"] == "open" and row["reason"] is None
    assert row["coverage_bps"] == 10_000 and row["promised_drops"] == PROMISE
    assert store.committed_drops(conn, c.id) == PROMISE


def test_record_promise_is_idempotent_on_offer_index(conn):
    c = _live(conn)
    first = store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    second = store.record_promise(conn, c, _inp(), decline_reason="below_clearing", now=1002)
    assert second == first
    assert store.committed_drops(conn, c.id) == PROMISE


def test_record_promise_budget_boundary(conn):
    c = _live(conn, budget=PROMISE, cap=1_000_000)
    exact = store.record_promise(conn, c, _inp("A"), decline_reason=None, now=1001)
    assert exact is not None and exact["state"] == "open"
    over = store.record_promise(conn, c, _inp("B", bidder="rOther"), decline_reason=None, now=1002)
    assert over is not None and over["state"] == "declined" and over["reason"] == "budget_exhausted"
    assert store.committed_drops(conn, c.id) == PROMISE


def test_record_promise_wallet_cap_counts_only_the_window(conn):
    c = _live(conn, budget=10_000_000, cap=PROMISE, now=1000)
    store.record_promise(conn, c, _inp("OLD"), decline_reason=None, now=1000)
    capped = store.record_promise(conn, c, _inp("NOW"), decline_reason=None, now=1001)
    assert capped is not None and capped["reason"] == "wallet_cap"
    later = 1000 + c.wallet_window_seconds + 1
    fresh = store.record_promise(conn, c, _inp("LATER"), decline_reason=None, now=later)
    assert fresh is not None and fresh["state"] == "open"


def test_record_promise_pure_decline_is_recorded_without_reserving(conn):
    c = _live(conn)
    row = store.record_promise(conn, c, _inp(), decline_reason="below_clearing", now=1001)
    assert row is not None and row["state"] == "declined" and row["reason"] == "below_clearing"
    assert store.committed_drops(conn, c.id) == 0


def test_record_promise_rejects_unrecorded_reasons(conn):
    c = _live(conn)
    for reason in ("campaign_inactive", "not_external_listing"):
        with pytest.raises(ValueError):
            store.record_promise(conn, c, _inp(), decline_reason=reason, now=1001)


def test_record_promise_after_stop_writes_nothing(conn):
    c = _live(conn)
    store.stop_campaign(conn, network=NET, actor="discord:1", now=1001)
    assert store.record_promise(conn, c, _inp(), decline_reason=None, now=1002) is None
    assert store.get_promise(conn, "BID1") is None


def test_record_promise_sizes_on_the_campaign_read_under_the_lock(conn):
    """An admin Update that commits between the caller's read and the write
    lock applies to this promise: the promise is sized (and its coverage
    snapshotted) from the campaign as it stands when the row is written, never
    from the stale read the caller priced against. Halving coverage after the
    read must not leave a promise owed at the old rate."""
    stale = _live(conn)
    store.update_campaign(
        conn,
        network=NET,
        actor="discord:2",
        knobs=store.Knobs(
            coverage_bps=5_000,
            budget_drops=50_000_000,
            wallet_cap_drops=5_000_000,
            min_bid_drops=1_000_000,
        ),
        now=1001,
    )
    row = store.record_promise(conn, stale, _inp(), decline_reason=None, now=1002)
    assert row is not None and row["state"] == "open"
    assert stale.coverage_bps == 10_000  # the caller's read really is stale
    assert row["coverage_bps"] == 5_000
    assert row["promised_drops"] == PROMISE // 2
    assert store.committed_drops(conn, stale.id) == PROMISE // 2


def test_record_promise_applies_a_raised_minimum_bid_under_the_lock(conn):
    """Same rule for the minimum bid: the caller cleared the old floor, but the
    promise that gets written is judged against the campaign it is written
    into."""
    stale = _live(conn)
    store.update_campaign(
        conn,
        network=NET,
        actor="discord:2",
        knobs=store.Knobs(
            coverage_bps=10_000,
            budget_drops=50_000_000,
            wallet_cap_drops=5_000_000,
            min_bid_drops=9_000_000,
        ),
        now=1001,
    )
    row = store.record_promise(conn, stale, _inp(), decline_reason=None, now=1002)
    assert row is not None and row["state"] == "declined" and row["reason"] == "below_min_bid"
    assert store.committed_drops(conn, stale.id) == 0


def test_release_frees_budget_and_only_releases_open(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    assert store.release_promise(conn, "BID1", "expired", now=2000) is True
    assert store.release_promise(conn, "BID1", "expired", now=2001) is False
    row = store.get_promise(conn, "BID1")
    assert row["state"] == "released" and row["reason"] == "expired" and row["closed_at"] == 2000
    assert store.committed_drops(conn, c.id) == 0


def test_fill_promise_writes_one_owed_refund(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    refund = store.fill_promise(conn, "BID1", _owed(), now=1100)
    assert refund["state"] == "owed" and refund["refund_drops"] == 80_642
    assert (
        refund["bidder"] == BIDDER and refund["seller"] == SELLER and refund["campaign_id"] == c.id
    )
    assert store.get_promise(conn, "BID1")["state"] == "filled"
    # poll + sweep race: the second fill returns the same row, no second refund
    again = store.fill_promise(conn, "BID1", _owed(accept="ACCEPT1"), now=1101)
    assert again == refund
    assert conn.execute("SELECT COUNT(*) FROM fee_cover_refunds").fetchone()[0] == 1
    assert store.committed_drops(conn, c.id) == 80_642


def test_fill_promise_declined_refund_frees_budget(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    decision = store.RefundDecision(
        "ACCEPT1", SELLER, CAFE, 80_642, 349_605, 0, "linked_counterparty"
    )
    refund = store.fill_promise(conn, "BID1", decision, now=1100)
    assert (
        refund["state"] == "declined"
        and refund["refund_drops"] == 0
        and refund["reason"] == "linked_counterparty"
    )
    assert store.committed_drops(conn, c.id) == 0


def test_fill_promise_on_a_released_promise_returns_none(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    store.release_promise(conn, "BID1", "cancelled", now=1002)
    assert store.fill_promise(conn, "BID1", _owed(), now=1100) is None


def test_payout_claim_happens_exactly_once(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    assert store.claim_for_payout(conn, "ACCEPT1", 5_000, claim_ledger=4_000, now=1101) is True
    assert store.claim_for_payout(conn, "ACCEPT1", 5_000, claim_ledger=4_000, now=1102) is False
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "submitted" and row["last_ledger_seq"] == 5_000


def test_record_payout_and_return_to_owed(conn):
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    store.claim_for_payout(conn, "ACCEPT1", 5_000, claim_ledger=4_000, now=1101)
    assert store.return_to_owed(conn, "ACCEPT1", now=1102) is True
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "owed" and row["last_ledger_seq"] is None
    store.claim_for_payout(conn, "ACCEPT1", 6_000, claim_ledger=4_000, now=1103)
    store.record_payout(
        conn, "ACCEPT1", state="confirmed", tx_hash="PAYOUT1", last_ledger_seq=5_990, now=1104
    )
    row = store.get_refund(conn, "ACCEPT1")
    assert (row["state"], row["payout_tx_hash"], row["last_ledger_seq"]) == (
        "confirmed",
        "PAYOUT1",
        5_990,
    )
    assert store.paid_drops(conn, c.id) == 80_642
    with pytest.raises(ValueError):
        store.record_payout(
            conn, "ACCEPT1", state="owed", tx_hash=None, last_ledger_seq=None, now=1105
        )


def test_requeue_failed_requires_budget_headroom(conn):
    c = _live(conn, budget=100_000, cap=1_000_000)
    store.record_promise(conn, c, _inp("BID1"), decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed("ACCEPT1"), now=1100)
    store.claim_for_payout(conn, "ACCEPT1", 5_000, claim_ledger=4_000, now=1101)
    store.record_payout(
        conn, "ACCEPT1", state="failed", tx_hash=None, last_ledger_seq=5_000, now=1102
    )
    assert store.get_refund(conn, "ACCEPT1")["reason"] == "payout_failed"
    assert store.committed_drops(conn, c.id) == 0
    # someone else takes most of the budget meanwhile
    store.record_promise(conn, c, _inp("BID2", bidder="rOther"), decline_reason=None, now=1103)
    assert store.requeue_failed(conn, "ACCEPT1", now=1104) == "budget_exhausted"
    store.release_promise(conn, "BID2", "cancelled", now=1105)
    assert store.requeue_failed(conn, "ACCEPT1", now=1106) == "requeued"
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "owed" and row["reason"] is None and row["payout_tx_hash"] is None
    assert store.requeue_failed(conn, "ACCEPT1", now=1107) == "not_failed"


def test_record_payout_failed_reason_defaults_and_can_be_expired(conn):
    c = _live(conn)
    for bid, accept, reason, expected in (
        ("BID1", "ACCEPT1", None, "payout_failed"),
        ("BID2", "ACCEPT2", "payout_expired", "payout_expired"),
    ):
        store.record_promise(
            conn,
            c,
            _inp(bid, bidder=f"r{bid}"),
            decline_reason=None,
            now=1001,
        )
        store.fill_promise(conn, bid, _owed(accept), now=1100)
        store.claim_for_payout(conn, accept, 5_000, claim_ledger=4_000, now=1101)
        store.record_payout(
            conn,
            accept,
            state="failed",
            tx_hash=None,
            last_ledger_seq=None,
            now=1102,
            reason=reason,
        )
        row = store.get_refund(conn, accept)
        assert (row["state"], row["reason"]) == ("failed", expected)
    # a non-failed outcome never takes the reason
    store.record_promise(conn, c, _inp("BID3", bidder="rBID3"), decline_reason=None, now=1001)
    store.fill_promise(conn, "BID3", _owed("ACCEPT3"), now=1100)
    store.claim_for_payout(conn, "ACCEPT3", 5_000, claim_ledger=4_000, now=1101)
    store.record_payout(
        conn,
        "ACCEPT3",
        state="confirmed",
        tx_hash="P3",
        last_ledger_seq=None,
        now=1102,
        reason="payout_expired",
    )
    assert store.get_refund(conn, "ACCEPT3")["reason"] is None


def test_view_folds_refund_over_promise(conn):
    c = _live(conn)
    assert store.view_for_offer(conn, "BID1") is None
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    assert store.view_for_offer(conn, "BID1") == {
        "state": "open",
        "drops": 80_642,
        "xrp": "0.080642",
        "reason": None,
        "payout_tx_hash": None,
    }
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    store.claim_for_payout(conn, "ACCEPT1", 5_000, claim_ledger=4_000, now=1101)
    store.record_payout(
        conn, "ACCEPT1", state="confirmed", tx_hash="PAYOUT1", last_ledger_seq=4_990, now=1102
    )
    assert store.view_for_offer(conn, "BID1") == {
        "state": "confirmed",
        "drops": 80_642,
        "xrp": "0.080642",
        "reason": None,
        "payout_tx_hash": "PAYOUT1",
    }


def test_status_summary(conn):
    assert store.status_summary(conn, NET, now=1000)["state"] == "never_started"
    c = _live(conn, budget=1_000_000, cap=1_000_000)
    store.record_promise(conn, c, _inp("A"), decline_reason=None, now=1001)
    store.record_promise(conn, c, _inp("B"), decline_reason=None, now=1002)
    store.record_promise(conn, c, _inp("C"), decline_reason="below_clearing", now=1003)
    store.fill_promise(conn, "B", _owed("ACC_B", refund=70_000), now=1100)
    summary = store.status_summary(conn, NET, now=1200)
    assert summary["state"] == "active"
    assert summary["campaign"]["id"] == c.id
    # the open promise A at its derived size, plus refund B's owed 70_000
    assert summary["committed_drops"] == PROMISE + 70_000
    assert summary["remaining_drops"] == 1_000_000 - (PROMISE + 70_000)
    assert summary["paid_drops"] == 0
    assert summary["open_promises"] == 1
    assert summary["refunds_by_state"] == {"owed": 1}
    assert summary["declines_by_reason"] == {"below_clearing": 1}
    assert summary["top_wallets"] == [{"wallet": BIDDER, "drops": 70_000}]
    store.stop_campaign(conn, network=NET, actor="discord:1", now=1300)
    assert store.status_summary(conn, NET, now=1400)["state"] == "stopped"


def test_a_confirmed_payout_overrides_a_row_recovery_already_parked_failed(conn):
    """Recovery can park a row `payout_expired` on the strength of an absence
    while its payout is still in flight. If that payout then comes back
    confirmed, the money moved: the hash must be persisted over the guess, or
    the row stays requeueable and the refund is paid twice."""
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    assert store.claim_for_payout(conn, "ACCEPT1", 5_000, claim_ledger=4_000, now=1101) is True
    assert (
        store.record_payout(
            conn,
            "ACCEPT1",
            state="failed",
            tx_hash=None,
            last_ledger_seq=None,
            now=1102,
            reason="payout_expired",
        )
        is True
    )
    assert store.get_refund(conn, "ACCEPT1")["state"] == "failed"
    assert (
        store.record_payout(
            conn, "ACCEPT1", state="confirmed", tx_hash="PAYOUT1", last_ledger_seq=None, now=1103
        )
        is True
    )
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "confirmed"
    assert row["payout_tx_hash"] == "PAYOUT1"
    assert row["reason"] is None
    # ...and the row can no longer be requeued into a second payment.
    assert store.requeue_failed(conn, "ACCEPT1", now=1104) == "not_failed"


def test_a_failed_payout_never_overrides_a_confirmed_one(conn):
    """The reverse is not symmetric: only the confirmed outcome is ground
    truth, so a losing `failed` write must change nothing."""
    c = _live(conn)
    store.record_promise(conn, c, _inp(), decline_reason=None, now=1001)
    store.fill_promise(conn, "BID1", _owed(), now=1100)
    store.claim_for_payout(conn, "ACCEPT1", 5_000, claim_ledger=4_000, now=1101)
    store.record_payout(
        conn, "ACCEPT1", state="confirmed", tx_hash="PAYOUT1", last_ledger_seq=None, now=1102
    )
    assert (
        store.record_payout(
            conn,
            "ACCEPT1",
            state="failed",
            tx_hash=None,
            last_ledger_seq=None,
            now=1103,
            reason="payout_expired",
        )
        is False
    )
    row = store.get_refund(conn, "ACCEPT1")
    assert row["state"] == "confirmed" and row["payout_tx_hash"] == "PAYOUT1"


def test_record_promise_applies_a_lowered_minimum_bid_under_the_lock(conn):
    """The mirror image: the caller's stale read declined the bid below the old
    floor, but the admin lowered it before the write. The promise is written
    into the campaign as it now stands, so the bid qualifies and is promised."""
    stale = _live(conn, min_bid=9_000_000)
    store.update_campaign(
        conn,
        network=NET,
        actor="discord:2",
        knobs=store.Knobs(
            coverage_bps=10_000,
            budget_drops=50_000_000,
            wallet_cap_drops=5_000_000,
            min_bid_drops=1_000_000,
        ),
        now=1001,
    )
    row = store.record_promise(conn, stale, _inp(), decline_reason="below_min_bid", now=1002)
    assert row is not None and row["state"] == "open" and row["reason"] is None
    assert store.committed_drops(conn, stale.id) == PROMISE


def test_record_promise_keeps_campaign_independent_pure_declines(conn):
    """Only `below_min_bid` is re-decided under the lock; a verdict that doesn't
    depend on the campaign (e.g. below the listing's clearing price) stands."""
    c = _live(conn)
    row = store.record_promise(conn, c, _inp(), decline_reason="below_clearing", now=1001)
    assert row is not None and row["state"] == "declined" and row["reason"] == "below_clearing"


def _quote(session_id="S1", created_at=1000, **over):
    base = {
        "session_id": session_id,
        "network": NET,
        "payload_uuid": "U1",
        "txid": None,
        "nft_id": "NFT1",
        "owner": "rOwner",
        "bidder": BIDDER,
        "bid_drops": 5_075_000,
        "bid_expires_at": created_at + 604_800,
        "listing": {"offer_index": "E" * 64, "broker": CAFE, "broker_rate": 0.01589},
        "created_at": created_at,
    }
    base.update(over)
    return store.Quote(**base)


def test_quote_lifecycle(conn):
    store.record_quote(conn, _quote())
    store.record_quote(conn, _quote(bid_drops=1))  # idempotent on session_id
    row = store.get_quote(conn, "S1")
    assert row["state"] == "pending" and row["bid_drops"] == 5_075_000 and row["txid"] is None
    store.set_quote_txid(conn, "S1", "TX1")
    store.set_quote_txid(conn, "S1", "TX2")  # the first signed hash sticks
    assert store.get_quote(conn, "S1")["txid"] == "TX1"
    [pending] = store.pending_quotes(conn, NET, created_before=2000, limit=10)
    assert pending.txid == "TX1" and pending.listing["broker"] == CAFE
    assert store.close_quote(conn, "S1", "promised", offer_index="BID1", now=1500) is True
    assert store.close_quote(conn, "S1", "failed", now=1600) is False  # only pending moves
    row = store.get_quote(conn, "S1")
    assert (row["state"], row["outcome"], row["offer_index"]) == ("closed", "promised", "BID1")
    assert store.pending_quotes(conn, NET, created_before=2000, limit=10) == []


def test_pending_quotes_respects_the_grace_period_network_and_limit(conn):
    store.record_quote(conn, _quote("OLD1", created_at=1000))
    store.record_quote(conn, _quote("OLD2", created_at=1001))
    store.record_quote(conn, _quote("FRESH", created_at=1990))
    store.record_quote(conn, _quote("MAINNET", created_at=1000, network="mainnet"))
    ids = [q.session_id for q in store.pending_quotes(conn, NET, created_before=1500, limit=10)]
    assert ids == ["OLD1", "OLD2"]
    assert [
        q.session_id for q in store.pending_quotes(conn, NET, created_before=1500, limit=1)
    ] == ["OLD1"]


def test_pending_quotes_rotate_by_last_attempt_so_old_ones_cannot_starve_new(conn):
    """Five long-unresolved quotes and a batch of two: every attempted quote is
    touched, so the newer ones still get their turn."""
    for n in range(5):
        store.record_quote(conn, _quote(f"Q{n}", created_at=1000 + n))

    def batch(now):
        picked = [
            q.session_id for q in store.pending_quotes(conn, NET, created_before=5000, limit=2)
        ]
        for session_id in picked:
            store.touch_quote(conn, session_id, now=now)
        return picked

    assert batch(2000) == ["Q0", "Q1"]
    assert batch(2001) == ["Q2", "Q3"]
    assert batch(2002) == ["Q4", "Q0"]  # never-attempted first, then least recent
    assert store.get_quote(conn, "Q0")["last_attempt_at"] == 2002


def test_connect_migrates_a_db_created_by_an_earlier_build(tmp_path):
    """CREATE TABLE IF NOT EXISTS never alters an existing table: a DB from a
    build before claim_ledger / bid_expires_at / last_attempt_at must gain the
    columns (idempotently), and a pre-existing pending quote must get an expiry
    that can't abandon it while its bid could still fill."""
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE fee_cover_refunds (
          accept_tx_hash TEXT PRIMARY KEY, offer_index TEXT NOT NULL UNIQUE,
          campaign_id INTEGER NOT NULL, network TEXT NOT NULL, bidder TEXT NOT NULL,
          seller TEXT, broker TEXT, observed_fee_drops INTEGER, observed_royalty_drops INTEGER,
          refund_drops INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, reason TEXT,
          payout_tx_hash TEXT, last_ledger_seq INTEGER,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE fee_cover_quotes (
          session_id TEXT PRIMARY KEY, network TEXT NOT NULL, payload_uuid TEXT NOT NULL,
          txid TEXT, nft_id TEXT NOT NULL, owner TEXT NOT NULL, bidder TEXT NOT NULL,
          bid_drops INTEGER NOT NULL, listing_json TEXT NOT NULL,
          state TEXT NOT NULL CHECK (state IN ('pending','closed')), outcome TEXT,
          offer_index TEXT, created_at INTEGER NOT NULL, closed_at INTEGER
        );
        CREATE INDEX idx_fee_cover_quotes_pending ON fee_cover_quotes(network, state, created_at);
        INSERT INTO fee_cover_quotes (session_id, network, payload_uuid, nft_id, owner, bidder,
          bid_drops, listing_json, state, created_at)
          VALUES ('OLD', 'testnet', 'U1', 'NFT1', 'rOwner', 'rBidder', 1, '{}', 'pending', 1000);
        """
    )
    old.commit()
    old.close()
    for _ in range(2):  # idempotent
        conn = store.connect(path)
        conn.close()
    conn = store.connect(path)
    try:
        refund_cols = {r[1] for r in conn.execute("PRAGMA table_info(fee_cover_refunds)")}
        quote_cols = {r[1] for r in conn.execute("PRAGMA table_info(fee_cover_quotes)")}
        assert "claim_ledger" in refund_cols
        assert {"bid_expires_at", "last_attempt_at"} <= quote_cols
        [quote] = store.pending_quotes(conn, "testnet", created_before=2000, limit=5)
        assert quote.bid_expires_at == 1000 + 30 * 86_400
        store.touch_quote(conn, "OLD", now=1500)
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(fee_cover_quotes)")}
        assert "idx_fee_cover_quotes_rotation" in indexes
        assert "idx_fee_cover_quotes_pending" not in indexes
    finally:
        conn.close()
