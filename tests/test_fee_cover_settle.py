"""Fee-cover settlement orchestration with injected ledger fakes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from lfg_core import fee_cover, history_store, xrpl_ops
from lfg_core import fee_cover_settle as settle
from lfg_core import fee_cover_store as store

FIXTURE = Path(__file__).parent / "fixtures" / "fee_cover" / "cafe_brokered_accept.json"
NET = "mainnet"
CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"
ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
SELLER = "rLfbT5Vkbigi4gFUi1QUGu5udijV79i3tF"
BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"
NFT_ID = "00191B58D1AE1BC312BEF9C68233FB0C8CF6A338F7C227BECADA906904943EEE"
BUY_OFFER = "37B0856D51DF75221A9140AF8FCD89E1AC3CAC703D36070BA7E2FE85567C4B7D"
SELL_OFFER = "1C827D06CDE70456A31AA10FC2169356964093BE4BF42B18DAD551518A186B35"
ACCEPT = "FED6256DC27A6D06C43E20ADA2B5D2FFF4AEAEE7560FADBF584CAE6396C1ACFA"
ACCEPT_TS = 1_787_421_471
NOW = 1_787_500_000


def _tx():
    return json.loads(FIXTURE.read_text())


class Ledger:
    """Records calls; behaviour is set per test."""

    def __init__(self):
        self.get_tx_calls: list[str] = []
        self.sent: list[dict] = []
        self.tx = _tx()
        self.get_tx_error: Exception | None = None
        self.payment = xrpl_ops.ClaimPayment("confirmed", "PAYOUT", 7_040)
        self.send_error: BaseException | None = None
        self.found: dict[str, str] = {}
        self.find_error: Exception | None = None
        self.validated_index: int | None = 7_000
        self.find_min_ledgers: list[int | None] = []

    async def get_tx(self, tx_hash):
        self.get_tx_calls.append(tx_hash)
        if self.get_tx_error:
            raise self.get_tx_error
        return self.tx

    async def send_refund(
        self, destination, drops, accept_tx_hash, *, campaign_id, max_last_ledger_seq=None
    ):
        self.sent.append(
            {
                "destination": destination,
                "drops": drops,
                "accept": accept_tx_hash,
                "campaign_id": campaign_id,
                "max_lls": max_last_ledger_seq,
            }
        )
        await asyncio.sleep(0)
        if self.send_error:
            raise self.send_error
        return self.payment

    async def find_refund_payment(self, accept_tx_hash, *, destination, drops, min_ledger):
        self.find_min_ledgers.append(min_ledger)
        if self.find_error:
            raise self.find_error
        return self.found.get(accept_tx_hash)

    async def current_ledger(self):
        return self.validated_index


@pytest.fixture()
def env(tmp_path):
    ledger = Ledger()
    deps = settle.FeeCoverDeps(
        network=NET,
        app_db_path=str(tmp_path / "app.db"),
        history_db_path=str(tmp_path / "history.db"),
        payer=ISSUER,
        ledger_margin=40,
        get_tx=ledger.get_tx,
        send_refund=ledger.send_refund,
        find_refund_payment=ledger.find_refund_payment,
        current_ledger=ledger.current_ledger,
        broker_rate_for=lambda account, nft_id: {CAFE: 0.01589}.get(account),
        linked=lambda a, b: False,
        now=lambda: NOW,
    )
    conn = store.connect(deps.app_db_path)
    knobs = store.Knobs(
        coverage_bps=10_000, budget_drops=1_000_000, wallet_cap_drops=1_000_000, min_bid_drops=0
    )
    campaign, _ = store.start_campaign(
        conn, network=NET, actor="t", knobs=knobs, duration_seconds=None, now=ACCEPT_TS - 600
    )
    conn.close()
    return deps, ledger, campaign


def _promise(deps, campaign, offer_index=BUY_OFFER, bidder=BUYER, expiration=900_000_000):
    conn = store.connect(deps.app_db_path)
    try:
        inp = store.PromiseInput(
            offer_index=offer_index,
            network=NET,
            nft_id=NFT_ID,
            bidder=bidder,
            bid_drops=5_075_000,
            ask_drops=4_990_000,
            broker=CAFE,
            broker_rate=0.01589,
            bid_expiration=expiration,
        )
        return store.record_promise(conn, campaign, inp, decline_reason=None, now=ACCEPT_TS - 60)
    finally:
        conn.close()


def _archive_accept(deps, tx=None, ts=ACCEPT_TS):
    tx = tx or _tx()
    hconn = history_store.init_history_db(deps.history_db_path)
    history_store.insert_tx(
        hconn,
        tx_hash=tx["hash"],
        ledger_index=tx.get("ledger_index"),
        close_time=ts,
        tx_type="NFTokenAcceptOffer",
        account=tx["Account"],
        source_tag=tx.get("SourceTag"),
        raw_json=json.dumps(tx),
    )
    history_store.insert_nft_event(
        hconn,
        {
            "tx_hash": tx["hash"],
            "nft_id": NFT_ID,
            "event": "sale",
            "from_addr": SELLER,
            "to_addr": BUYER,
            "price_drops": 5_075_000,
            "ts": ts,
            "offer_index": SELL_OFFER,
        },
    )
    hconn.commit()
    hconn.close()


def _archive_state(deps, *, close_time=NOW, baseline=1, gap=None):
    hconn = history_store.init_history_db(deps.history_db_path)
    hconn.execute(
        "INSERT OR REPLACE INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " continuity_gap_reason, updated_at) VALUES (?, 'genesis', ?, ?, ?, 1)",
        (NET, baseline, close_time, gap),
    )
    hconn.commit()
    hconn.close()


def _refund(deps, key=ACCEPT):
    conn = store.connect(deps.app_db_path)
    try:
        return store.get_refund(conn, key)
    finally:
        conn.close()


def _run(coro):
    # A private loop: never disturbs (or depends on) the global event loop,
    # which asyncio.run() in other test modules leaves unset on Python 3.10.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- archive helpers ----------------------------------------------------------


def test_find_accept_tx_matches_only_this_buy_offer_after_since(env):
    deps, _, _ = env
    _archive_accept(deps)
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        assert settle.find_accept_tx(hconn, NFT_ID, BUY_OFFER, ACCEPT_TS - 10)["hash"] == ACCEPT
        assert settle.find_accept_tx(hconn, NFT_ID, "F" * 64, ACCEPT_TS - 10) is None
        assert settle.find_accept_tx(hconn, NFT_ID, BUY_OFFER, ACCEPT_TS + 1) is None
    finally:
        hconn.close()


def test_cancel_seen_splits_grouped_offer_indexes(env):
    deps, _, _ = env
    hconn = history_store.init_history_db(deps.history_db_path)
    history_store.insert_nft_event(
        hconn,
        {
            "tx_hash": "CANCEL",
            "nft_id": NFT_ID,
            "event": "offer_cancel",
            "ts": ACCEPT_TS,
            "offer_index": f"AAAA,{BUY_OFFER}",
        },
    )
    hconn.commit()
    try:
        assert settle.cancel_seen(hconn, NFT_ID, BUY_OFFER) is True
        assert settle.cancel_seen(hconn, NFT_ID, "AAAA") is True
        assert settle.cancel_seen(hconn, NFT_ID, "BBBB") is False
    finally:
        hconn.close()


def test_archive_blocker(env):
    deps, _, _ = env
    hconn = history_store.init_history_db(deps.history_db_path)
    try:
        assert settle.archive_blocker(hconn, NET, 100) == "no archive_state row"
    finally:
        hconn.close()
    for kwargs, expected in (
        ({"baseline": 0}, "baseline not complete"),
        ({"gap": "restart"}, "continuity gap recorded"),
        ({"close_time": 100}, "archive not yet validated past the expiry"),
        ({}, None),
    ):
        _archive_state(deps, **kwargs)
        hconn = history_store.init_history_db(deps.history_db_path)
        try:
            assert settle.archive_blocker(hconn, NET, 100) == expected
        finally:
            hconn.close()


def test_normalize_tx_flattens_api_v2():
    v2 = {
        "tx_json": {"Account": CAFE, "TransactionType": "NFTokenAcceptOffer"},
        "meta": {"x": 1},
        "hash": ACCEPT,
        "validated": True,
    }
    flat = settle.normalize_tx(v2)
    assert (
        flat["Account"] == CAFE
        and flat["meta"] == {"x": 1}
        and flat["hash"] == ACCEPT
        and "tx_json" not in flat
    )
    assert settle.normalize_tx(_tx())["Account"] == CAFE


# --- settle -------------------------------------------------------------------


def test_settle_promise_writes_an_owed_refund_from_the_archived_accept(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    _archive_accept(deps)
    view = _run(settle.settle_promise(deps, BUY_OFFER))
    assert view == {
        "state": "owed",
        "drops": 80_642,
        "xrp": "0.080642",
        "reason": None,
        "payout_tx_hash": None,
    }
    assert ledger.get_tx_calls == [ACCEPT]
    assert _refund(deps)["observed_royalty_drops"] == 349_605


def test_settle_promise_accepts_an_api_v2_get_tx_result(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    _archive_accept(deps)
    tx = _tx()
    meta = tx.pop("meta")
    ledger.tx = {
        "tx_json": tx,
        "meta": meta,
        "hash": tx.pop("hash"),
        "validated": tx.pop("validated"),
    }
    assert _run(settle.settle_promise(deps, BUY_OFFER))["state"] == "owed"


def test_settle_promise_without_an_archived_accept_stays_open(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    assert _run(settle.settle_promise(deps, BUY_OFFER))["state"] == "open"
    assert ledger.get_tx_calls == []


def test_settle_promise_lookup_failure_stays_open(env):
    deps, ledger, _ = env
    _promise(deps, env[2])
    _archive_accept(deps)
    ledger.get_tx_error = RuntimeError("rpc down")
    assert _run(settle.settle_promise(deps, BUY_OFFER))["state"] == "open"


# --- pay ------------------------------------------------------------------------


def _owed(deps, campaign):
    _promise(deps, campaign)
    _archive_accept(deps)
    _run(settle.settle_promise(deps, BUY_OFFER))


def test_pay_refund_confirms(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    assert _run(settle.pay_refund(deps, ACCEPT)) == "confirmed"
    assert ledger.sent == [
        {
            "destination": BUYER,
            "drops": 80_642,
            "accept": ACCEPT,
            "campaign_id": campaign.id,
            "max_lls": 7_400,
        }
    ]
    row = _refund(deps)
    assert (row["state"], row["payout_tx_hash"], row["last_ledger_seq"]) == (
        "confirmed",
        "PAYOUT",
        7_040,
    )


def test_concurrent_pay_refund_submits_exactly_once(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)

    async def both():
        return await asyncio.gather(
            settle.pay_refund(deps, ACCEPT), settle.pay_refund(deps, ACCEPT)
        )

    states = _run(both())
    assert len(ledger.sent) == 1
    assert "confirmed" in states


def test_pay_refund_not_submitted_returns_to_owed_and_defers(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = xrpl_ops.ClaimNotSubmitted("no ledger")
    assert _run(settle.pay_refund(deps, ACCEPT)) == "deferred"
    assert _refund(deps)["state"] == "owed" and _refund(deps)["last_ledger_seq"] is None


def test_pay_refund_unknown_stays_submitted_with_the_payment_deadline(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.payment = xrpl_ops.ClaimPayment("unknown", None, 7_040)
    assert _run(settle.pay_refund(deps, ACCEPT)) == "submitted"
    row = _refund(deps)
    assert row["state"] == "submitted" and row["last_ledger_seq"] == 7_040


def test_pay_refund_unexpected_error_leaves_it_submitted_for_recovery(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = RuntimeError("boom")
    assert _run(settle.pay_refund(deps, ACCEPT)) == "submitted"
    row = _refund(deps)
    assert row["last_ledger_seq"] == 7_400  # the provisional deadline
    assert row["claim_ledger"] == 7_000  # the validated ledger read before the claim


def test_recovery_scans_from_the_claim_ledger_not_deadline_minus_margin(env):
    """The deadline was set with the margin in force at CLAIM time. Recovery
    must not re-derive a floor from it with whatever margin is configured now:
    lower the margin and that floor would move past a refund that landed, the
    row would park `payout_expired`, and a requeue would pay it twice."""
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = RuntimeError("boom")  # left submitted for recovery
    assert _run(settle.pay_refund(deps, ACCEPT)) == "submitted"
    deps.ledger_margin = 1  # the operator lowers the margin after the claim
    ledger.found = {ACCEPT: "PAYOUT"}
    assert _run(settle.recover_refunds(deps)) == {ACCEPT: "confirmed"}
    assert ledger.find_min_ledgers == [7_000]


def test_returning_to_owed_clears_the_claim_ledger(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = xrpl_ops.ClaimNotSubmitted("no ledger")
    assert _run(settle.pay_refund(deps, ACCEPT)) == "deferred"
    assert _refund(deps)["claim_ledger"] is None


def test_pay_refund_without_a_ledger_index_does_not_claim(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.validated_index = None
    assert _run(settle.pay_refund(deps, ACCEPT)) == "deferred"
    assert ledger.sent == []
    assert _refund(deps)["state"] == "owed"


def test_pay_refund_defers_when_the_ledger_read_raises_and_the_sweep_counts_no_attempt(env):
    deps, ledger, campaign = env
    _owed(deps, campaign)

    async def ledger_down():
        raise RuntimeError("rippled unreachable")

    deps.current_ledger = ledger_down
    assert _run(settle.pay_refund(deps, ACCEPT)) == "deferred"
    assert ledger.sent == [] and _refund(deps)["state"] == "owed"
    attempts: dict[str, int] = {"OTHER": 1}
    gave_up: list[dict] = []
    for _ in range(3):
        _run(settle.sweep_once(deps, attempts=attempts, max_attempts=1, on_giveup=gave_up.append))
    assert attempts == {"OTHER": 1}
    assert gave_up == [] and ledger.sent == []


def test_pay_refund_missing_row(env):
    deps, _, _ = env
    assert _run(settle.pay_refund(deps, "NOPE")) == "missing"


# --- recover ----------------------------------------------------------------------


def _submitted(deps, ledger, campaign, lls=7_040):
    _owed(deps, campaign)
    ledger.payment = xrpl_ops.ClaimPayment("unknown", None, lls)
    _run(settle.pay_refund(deps, ACCEPT))


def test_recover_found_confirms(env):
    deps, ledger, campaign = env
    _submitted(deps, ledger, campaign)
    ledger.found[ACCEPT] = "FOUNDHASH"
    assert _run(settle.recover_refunds(deps)) == {ACCEPT: "confirmed"}
    assert _refund(deps)["payout_tx_hash"] == "FOUNDHASH"


def test_recover_absent_before_deadline_is_untouched_and_after_is_failed(env):
    deps, ledger, campaign = env
    _submitted(deps, ledger, campaign, lls=7_040)
    ledger.validated_index = 7_040
    assert _run(settle.recover_refunds(deps)) == {}
    ledger.validated_index = 7_041
    assert _run(settle.recover_refunds(deps)) == {ACCEPT: "failed"}
    assert _refund(deps)["reason"] == "payout_expired"


def test_recover_lookup_error_is_untouched(env):
    deps, ledger, campaign = env
    _submitted(deps, ledger, campaign)
    ledger.validated_index = 99_999
    ledger.find_error = RuntimeError("account_tx down")
    assert _run(settle.recover_refunds(deps)) == {}
    assert _refund(deps)["state"] == "submitted"


def test_recover_without_submitted_rows_never_reads_the_ledger(env):
    deps, ledger, _ = env

    async def boom():
        raise AssertionError("must not be called")

    deps.current_ledger = boom
    assert _run(settle.recover_refunds(deps)) == {}


# --- sweep ------------------------------------------------------------------------


def test_sweep_settles_pays_and_releases_expired(env):
    deps, ledger, campaign = env
    _promise(deps, campaign)
    _archive_accept(deps)
    _promise(deps, campaign, offer_index="E" * 64, bidder="rOtherBidder", expiration=800_000_000)
    _archive_state(deps, close_time=NOW)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert (report.settled, report.released, report.paid) == (1, 1, 1)
    conn = store.connect(deps.app_db_path)
    try:
        assert store.get_promise(conn, "E" * 64)["reason"] == "expired"
        assert store.get_refund(conn, ACCEPT)["state"] == "confirmed"
    finally:
        conn.close()


def test_sweep_releases_a_cancelled_bid(env):
    deps, _, campaign = env
    _promise(deps, campaign)
    hconn = history_store.init_history_db(deps.history_db_path)
    history_store.insert_nft_event(
        hconn,
        {
            "tx_hash": "CANCEL",
            "nft_id": NFT_ID,
            "event": "offer_cancel",
            "ts": ACCEPT_TS,
            "offer_index": BUY_OFFER,
        },
    )
    hconn.commit()
    hconn.close()
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert report.released == 1


def test_sweep_never_releases_an_expired_bid_across_an_archive_gap(env):
    deps, _, campaign = env
    _promise(deps, campaign, offer_index="E" * 64, bidder="rOtherBidder", expiration=800_000_000)
    _archive_state(deps, close_time=NOW, gap="listener restart")
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert report.released == 0


def test_open_promises_are_honored_after_stop(env):
    deps, ledger, campaign = env
    _promise(deps, campaign)
    _archive_accept(deps)
    conn = store.connect(deps.app_db_path)
    store.stop_campaign(conn, network=NET, actor="t", now=ACCEPT_TS)
    conn.close()
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert (report.settled, report.paid) == (1, 1)
    assert _refund(deps)["state"] == "confirmed"


def test_sweep_never_gives_up_on_a_deferred_refund(env):
    """ClaimNotSubmitted refuses before anything reached the ledger: it is
    retried every sweep and never counted towards the giveup."""
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.send_error = xrpl_ops.ClaimNotSubmitted("still no ledger")
    attempts: dict[str, int] = {}
    gave_up: list[dict] = []
    for _ in range(3):
        _run(settle.sweep_once(deps, attempts=attempts, max_attempts=2, on_giveup=gave_up.append))
    assert len(ledger.sent) == 3
    assert attempts == {} and gave_up == []
    assert _refund(deps)["state"] == "owed"


def test_sweep_gives_up_on_a_refund_after_max_attempts(env, monkeypatch):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    calls: list[str] = []

    async def still_owed(deps_, key):
        calls.append(key)
        return "owed"

    monkeypatch.setattr(settle, "pay_refund", still_owed)
    attempts: dict[str, int] = {}
    gave_up: list[dict] = []
    for _ in range(3):
        _run(settle.sweep_once(deps, attempts=attempts, max_attempts=2, on_giveup=gave_up.append))
    assert calls == [ACCEPT, ACCEPT]
    assert [row["accept_tx_hash"] for row in gave_up] == [ACCEPT]


def _extra_owed(deps, campaign, offer_index, accept, bidder="rOtherBidder"):
    _promise(deps, campaign, offer_index=offer_index, bidder=bidder)
    conn = store.connect(deps.app_db_path)
    try:
        store.fill_promise(
            conn,
            offer_index,
            store.RefundDecision(accept, SELLER, CAFE, 80_642, 349_605, 50_000, None),
            now=ACCEPT_TS,
        )
    finally:
        conn.close()


def test_a_promise_whose_settle_raises_does_not_stop_an_owed_refund_being_paid(env, monkeypatch):
    deps, ledger, campaign = env
    _owed(deps, campaign)  # ACCEPT is owed
    _promise(deps, campaign, offer_index="E" * 64, bidder="rOtherBidder")
    real_settle = settle.settle_promise

    async def flaky_settle(deps_, offer_index):
        if offer_index == "E" * 64:
            raise sqlite3.OperationalError("database is locked")
        return await real_settle(deps_, offer_index)

    monkeypatch.setattr(settle, "settle_promise", flaky_settle)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda r: None))
    assert report.paid == 1
    assert _refund(deps)["state"] == "confirmed"


def test_one_owed_row_raising_does_not_stop_the_next_or_recovery(env, monkeypatch):
    deps, ledger, campaign = env
    _owed(deps, campaign)
    _extra_owed(deps, campaign, "E" * 64, "ACCEPT2")
    real_pay = settle.pay_refund
    recovered: list[bool] = []

    async def flaky_pay(deps_, key):
        if key == ACCEPT:
            raise RuntimeError("row read failed")
        return await real_pay(deps_, key)

    async def flaky_recover(deps_):
        recovered.append(True)
        raise RuntimeError("account_tx down")

    monkeypatch.setattr(settle, "pay_refund", flaky_pay)
    monkeypatch.setattr(settle, "recover_refunds", flaky_recover)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda r: None))
    assert report.paid == 1 and report.recovered == 0
    assert _refund(deps, "ACCEPT2")["state"] == "confirmed"
    assert _refund(deps)["state"] == "owed"
    assert recovered == [True]


# --- expiry release margin tests ---


def test_expired_release_with_archived_accept_but_get_tx_fails_stays_open(env):
    """Expired bid with accept archived but get_tx down: release nothing, stay open."""
    deps, ledger, campaign = env
    test_offer = "E" * 64
    # Create promise and archive accept for the same offer (so accept lookup finds it)
    _promise(deps, campaign, offer_index=test_offer, bidder="rOtherBidder", expiration=800_000_000)
    # Archive an accept but with the test_offer as NFTokenBuyOffer
    tx = _tx()
    tx["NFTokenBuyOffer"] = test_offer
    _archive_accept(deps, tx=tx, ts=ACCEPT_TS)
    ledger.get_tx_error = RuntimeError("rpc down")
    _archive_state(deps, close_time=NOW)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert report.released == 0
    conn = store.connect(deps.app_db_path)
    try:
        promise = store.get_promise(conn, test_offer)
        assert promise["state"] == "open" and promise["reason"] is None
    finally:
        conn.close()


def test_expired_release_respects_archive_margin(env):
    """Expired bid without accept: within margin (expiry+300) → not released;
    past margin (expiry+601) → released."""
    deps, _, campaign = env
    expiration = 800_000_000
    _promise(deps, campaign, offer_index="E" * 64, bidder="rOtherBidder", expiration=expiration)
    expiry_unix = expiration + settle.RIPPLE_EPOCH_OFFSET
    # Test within margin: archive close_time = expiry_unix + 300
    _archive_state(deps, close_time=expiry_unix + 300)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert report.released == 0
    # Test past margin: archive close_time = expiry_unix + 601
    _archive_state(deps, close_time=expiry_unix + 601)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=5, on_giveup=lambda row: None))
    assert report.released == 1
    conn = store.connect(deps.app_db_path)
    try:
        promise = store.get_promise(conn, "E" * 64)
        assert promise["reason"] == "expired"
    finally:
        conn.close()


def test_pay_refund_with_failed_claim_payment(env):
    """pay_refund where send_refund returns ClaimPayment("failed", None, 7_040)
    → returns "failed", row state becomes "failed", reason becomes "payout_failed"."""
    deps, ledger, campaign = env
    _owed(deps, campaign)
    ledger.payment = xrpl_ops.ClaimPayment("failed", None, 7_040)
    assert _run(settle.pay_refund(deps, ACCEPT)) == "failed"
    row = _refund(deps)
    assert row["state"] == "failed" and row["reason"] == "payout_failed"


def test_settle_defers_when_the_linkage_lookup_cannot_answer(env):
    """A linkage lookup that errors must not settle the sale as "unrelated" —
    that pays a refund on a sale the self-dealing rule might forbid. Nothing is
    written; the promise stays open and the next sweep retries it."""
    deps, _ledger, campaign = env

    def boom(bidder, seller):
        raise fee_cover.LinkageUnavailable("identity db locked")

    deps.linked = boom
    _promise(deps, campaign)
    _archive_accept(deps)
    view = _run(settle.settle_promise(deps, BUY_OFFER))
    assert view is not None and view["state"] == "open"
    assert _refund(deps) is None
    conn = store.connect(deps.app_db_path)
    try:
        assert store.get_promise(conn, BUY_OFFER)["state"] == "open"
    finally:
        conn.close()
    # ...and once the lookup can answer again, the same promise settles.
    deps.linked = lambda a, b: False
    assert _run(settle.settle_promise(deps, BUY_OFFER))["state"] == "owed"


def test_a_promise_whose_linkage_defers_does_not_stop_the_sweep(env):
    """The deferral is per-promise: the sweep's other work still runs."""
    deps, _ledger, campaign = env

    def boom(bidder, seller):
        raise fee_cover.LinkageUnavailable("identity db locked")

    deps.linked = boom
    _promise(deps, campaign)
    _archive_accept(deps)
    report = _run(settle.sweep_once(deps, attempts={}, max_attempts=3, on_giveup=lambda row: None))
    assert report.settled == 0 and report.released == 0
    assert _refund(deps) is None
