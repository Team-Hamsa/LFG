"""BRIX daily drip: schema, accrual store, and pure accrual evaluation (#48).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from lfg_core import brix_drip
from lfg_core.nft_index import OnchainNft


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "history_test.db")
    c.row_factory = sqlite3.Row
    brix_drip.ensure_schema(c)
    yield c
    c.close()


def _nft(nft_id: str, owner: str | None = "rHolder", burned: bool = False) -> OnchainNft:
    return OnchainNft(
        nft_id=nft_id,
        nft_number=1,
        owner=owner,
        is_burned=burned,
        mutable=True,
        uri_hex="",
        body="ape",
        attributes=[],
        image="",
        ledger_index=1,
    )


# --- Task 1: schema + store ------------------------------------------------


def test_schema_creates_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"brix_accruals", "brix_claims", "brix_meta"} <= names


def test_amounts_are_integer_columns(conn):
    """Spec §4: currency ledger is INTEGER whole BRIX, never REAL — the
    conservation audit compares exact SUMs and must never need epsilons."""
    accrual_cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(brix_accruals)")}
    claim_cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(brix_claims)")}
    assert accrual_cols["amount"].upper() == "INTEGER"
    assert claim_cols["amount"].upper() == "INTEGER"
    assert "last_ledger_seq" in claim_cols


def test_one_open_claim_per_wallet_enforced_by_index(conn):
    conn.execute(
        "INSERT INTO brix_claims (wallet, amount, state) VALUES (?, ?, ?)",
        ("rAlice", 5, "pending"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO brix_claims (wallet, amount, state) VALUES (?, ?, ?)",
            ("rAlice", 3, "submitted"),
        )
    # A terminal claim does not block a new one.
    conn.execute("UPDATE brix_claims SET state='confirmed' WHERE wallet='rAlice'")
    conn.execute(
        "INSERT INTO brix_claims (wallet, amount, state) VALUES (?, ?, ?)",
        ("rAlice", 3, "pending"),
    )
    conn.commit()


def test_record_accruals_is_idempotent_by_primary_key(conn):
    rows = [
        brix_drip.Accrual("2026-08-18", "NFT_A", "rAlice", 1),
        brix_drip.Accrual("2026-08-18", "NFT_B", "rBob", 1),
    ]
    assert brix_drip.record_accruals(conn, rows) == 2
    assert brix_drip.record_accruals(conn, rows) == 0
    assert conn.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 2


def test_claimable_sums_only_unbound_rows_and_returns_int(conn):
    brix_drip.record_accruals(
        conn,
        [
            brix_drip.Accrual("2026-08-17", "NFT_A", "rAlice", 1),
            brix_drip.Accrual("2026-08-18", "NFT_A", "rAlice", 1),
            brix_drip.Accrual("2026-08-18", "NFT_B", "rBob", 1),
        ],
    )
    balance = brix_drip.claimable(conn, "rAlice")
    assert balance == 2
    assert isinstance(balance, int)

    conn.execute(
        "UPDATE brix_accruals SET claim_id=7 WHERE nft_id='NFT_A' AND epoch_date='2026-08-17'"
    )
    conn.commit()
    assert brix_drip.claimable(conn, "rAlice") == 1
    assert brix_drip.claimable(conn, "rNobody") == 0


def test_meta_round_trip(conn):
    assert brix_drip.get_meta(conn, "last_accrued_epoch") is None
    brix_drip.set_meta(conn, "last_accrued_epoch", "2026-08-18")
    assert brix_drip.get_meta(conn, "last_accrued_epoch") == "2026-08-18"
    brix_drip.set_meta(conn, "last_accrued_epoch", "2026-08-19")
    assert brix_drip.get_meta(conn, "last_accrued_epoch") == "2026-08-19"


# --- Task 2: pure evaluation ----------------------------------------------


def test_evaluate_accruals_pays_only_unlisted_user_held_live_tokens():
    tokens = [
        _nft("LIVE_UNLISTED", "rAlice"),
        _nft("LIVE_LISTED", "rBob"),
        _nft("BURNED", "rAlice", burned=True),
        _nft("SYSTEM", "rDistributor"),
        _nft("OWNERLESS", None),
    ]
    listed = {"LIVE_LISTED": True}
    result = brix_drip.evaluate_accruals(
        tokens,
        listed_fn=lambda nft_id: listed.get(nft_id, False),
        system_accounts=frozenset({"rDistributor"}),
        epoch="2026-08-18",
    )
    assert [r.nft_id for r in result.rows] == ["LIVE_UNLISTED"]
    assert result.rows[0] == brix_drip.Accrual("2026-08-18", "LIVE_UNLISTED", "rAlice", 1)
    assert result.skipped_listed == 1
    assert result.skipped_system == 1
    assert result.skipped_burned == 1
    assert result.skipped_ownerless == 1
    assert result.unknown == 0


def test_evaluate_accruals_fails_closed_on_unknown_listing_state():
    """Spec §3: unknown offer state must never pay — an accrual is a monetary
    grant, and paying listed NFTs during a clio outage is unrecoverable."""
    tokens = [_nft("UNKNOWN_STATE", "rAlice"), _nft("FINE", "rAlice")]
    result = brix_drip.evaluate_accruals(
        tokens,
        listed_fn=lambda nft_id: None if nft_id == "UNKNOWN_STATE" else False,
        system_accounts=frozenset(),
        epoch="2026-08-18",
    )
    assert [r.nft_id for r in result.rows] == ["FINE"]
    assert result.unknown == 1


def test_classify_sell_offers_listed_only_for_current_holder():
    """A stale offer left by a PREVIOUS owner is invalid on-ledger and must
    not suppress the current holder's accrual."""
    resp = {"offers": [{"owner": "rPrevious", "amount": "1000"}]}
    assert brix_drip.classify_sell_offers(resp, holder="rAlice") is False

    resp = {"offers": [{"owner": "rAlice", "amount": "1000"}]}
    assert brix_drip.classify_sell_offers(resp, holder="rAlice") is True


def test_classify_sell_offers_destination_locked_offer_still_counts_as_listed():
    """Brokered marketplaces (xrp.cafe style) create destination-locked sell
    offers — precisely the listings we must exclude (spec §3)."""
    resp = {"offers": [{"owner": "rAlice", "amount": "1000", "destination": "rBroker"}]}
    assert brix_drip.classify_sell_offers(resp, holder="rAlice") is True


def test_classify_sell_offers_no_offers_is_unlisted():
    assert brix_drip.classify_sell_offers({"offers": []}, holder="rAlice") is False


# --- Task 2 (cont): listing lookup with retry + fail-closed ---------------


def test_fetch_sell_offer_state_maps_tokens_to_listing_state(monkeypatch):
    calls: list[str] = []

    async def fake_offers(nft_id, raise_on_error=False):
        calls.append(nft_id)
        if nft_id == "LISTED":
            return [{"owner": "rAlice", "amount": "1000"}]
        return []

    monkeypatch.setattr(brix_drip.xrpl_ops, "get_nft_sell_offers", fake_offers)
    holders = {"LISTED": "rAlice", "UNLISTED": "rBob"}
    state = asyncio.run(brix_drip.fetch_sell_offer_state(holders))
    assert state == {"LISTED": True, "UNLISTED": False}
    assert sorted(calls) == ["LISTED", "UNLISTED"]


def test_fetch_sell_offer_state_retries_then_reports_unknown(monkeypatch):
    """A lookup failure must surface as None (unknown), never as False —
    False would silently pay a token that may well be listed."""
    attempts: list[str] = []

    async def flaky(nft_id, raise_on_error=False):
        attempts.append(nft_id)
        raise RuntimeError("clio unavailable")

    monkeypatch.setattr(brix_drip.xrpl_ops, "get_nft_sell_offers", flaky)
    state = asyncio.run(brix_drip.fetch_sell_offer_state({"FLAKY": "rAlice"}, retries=3))
    assert state == {"FLAKY": None}
    assert len(attempts) == 3


def test_fetch_sell_offer_state_recovers_on_a_later_attempt(monkeypatch):
    seen: list[str] = []

    async def flaky_once(nft_id, raise_on_error=False):
        seen.append(nft_id)
        if len(seen) == 1:
            raise RuntimeError("transient")
        return []

    monkeypatch.setattr(brix_drip.xrpl_ops, "get_nft_sell_offers", flaky_once)
    state = asyncio.run(brix_drip.fetch_sell_offer_state({"NFT": "rAlice"}, retries=3))
    assert state == {"NFT": False}


# --- Task 3: epoch catch-up orchestration ---------------------------------


def test_epochs_to_accrue_defaults_to_yesterday_on_first_run():
    assert brix_drip.epochs_to_accrue(None, "2026-08-19") == ["2026-08-18"]


def test_epochs_to_accrue_catches_up_a_missed_cron_day():
    assert brix_drip.epochs_to_accrue("2026-08-15", "2026-08-19") == [
        "2026-08-16",
        "2026-08-17",
        "2026-08-18",
    ]


def test_epochs_to_accrue_is_empty_when_already_current():
    assert brix_drip.epochs_to_accrue("2026-08-18", "2026-08-19") == []
    # A cursor somehow ahead of yesterday must not produce negative ranges.
    assert brix_drip.epochs_to_accrue("2026-08-25", "2026-08-19") == []


def test_run_accrual_advances_cursor_and_is_a_no_op_on_rerun(conn):
    tokens = [_nft("NFT_A", "rAlice"), _nft("NFT_B", "rBob")]
    reports = brix_drip.run_accrual(
        conn,
        tokens,
        listed_fn=lambda nft_id: False,
        system_accounts=frozenset(),
        today="2026-08-19",
    )
    assert [r.epoch for r in reports] == ["2026-08-18"]
    assert reports[0].accrued == 2
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-18"

    again = brix_drip.run_accrual(
        conn,
        tokens,
        listed_fn=lambda nft_id: False,
        system_accounts=frozenset(),
        today="2026-08-19",
    )
    assert again == []
    assert conn.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 2


def test_run_accrual_reports_skip_reasons_per_epoch(conn):
    tokens = [_nft("A", "rAlice"), _nft("B", "rBob"), _nft("C", "rCarol")]

    def listed_fn(nft_id):
        return {"B": True, "C": None}.get(nft_id, False)

    reports = brix_drip.run_accrual(
        conn,
        tokens,
        listed_fn=listed_fn,
        system_accounts=frozenset(),
        today="2026-08-19",
    )
    assert reports[0].accrued == 1
    assert reports[0].skipped_listed == 1
    assert reports[0].unknown == 1


def test_run_accrual_catch_up_writes_every_missed_epoch(conn):
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, "2026-08-15")
    reports = brix_drip.run_accrual(
        conn,
        [_nft("NFT_A", "rAlice")],
        listed_fn=lambda nft_id: False,
        system_accounts=frozenset(),
        today="2026-08-19",
    )
    assert [r.epoch for r in reports] == ["2026-08-16", "2026-08-17", "2026-08-18"]
    assert brix_drip.claimable(conn, "rAlice") == 3
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-18"


# --- Task 4: conservation audit -------------------------------------------


def _seed_confirmed_claim(conn, wallet="rAlice", amount=2, tx_hash="HASH1", claim_id=1):
    conn.execute(
        "INSERT INTO brix_claims (claim_id, wallet, amount, state, tx_hash)"
        " VALUES (?, ?, ?, 'confirmed', ?)",
        (claim_id, wallet, amount, tx_hash),
    )
    for i in range(amount):
        conn.execute(
            "INSERT INTO brix_accruals (epoch_date, nft_id, owner, amount, claim_id)"
            " VALUES (?, ?, ?, 1, ?)",
            (f"2026-08-{10 + i:02d}", f"NFT_{i}", wallet, claim_id),
        )
    conn.commit()


def _seed_onchain_claim_event(conn, distributor="rDistributor", amount=2, tx_hash="HASH1"):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS brix_events (tx_hash TEXT, account TEXT,"
        " counterparty TEXT, delta REAL, kind TEXT, ts INTEGER,"
        " PRIMARY KEY (tx_hash, account))"
    )
    conn.execute(
        "INSERT INTO brix_events (tx_hash, account, counterparty, delta, kind, ts)"
        " VALUES (?, ?, ?, ?, 'claim', 0)",
        (tx_hash, distributor, "rAlice", -float(amount)),
    )
    conn.commit()


def test_audit_passes_when_claims_match_accruals_and_chain(conn):
    _seed_confirmed_claim(conn)
    _seed_onchain_claim_event(conn)
    results = brix_drip.audit_distribution(conn, distributor="rDistributor", live_token_count=100)
    assert all(r.ok for r in results), [r for r in results if not r.ok]


def test_audit_fails_when_confirmed_claim_has_no_onchain_debit(conn):
    _seed_confirmed_claim(conn)
    _seed_onchain_claim_event(conn, amount=0)
    results = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 100)}
    assert results["claims_match_chain"].ok is False


def test_audit_fails_when_bound_accruals_disagree_with_claim_amount(conn):
    _seed_confirmed_claim(conn, amount=2)
    # A third accrual bound to the claim without the claim amount following it.
    conn.execute(
        "INSERT INTO brix_accruals (epoch_date, nft_id, owner, amount, claim_id)"
        " VALUES ('2026-08-20', 'NFT_X', 'rAlice', 1, 1)"
    )
    conn.commit()
    _seed_onchain_claim_event(conn)
    results = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 100)}
    assert results["accruals_match_claims"].ok is False


def test_audit_fails_on_accrual_bound_to_failed_claim(conn):
    conn.execute(
        "INSERT INTO brix_claims (claim_id, wallet, amount, state) VALUES (2, 'rBob', 1, 'failed')"
    )
    conn.execute(
        "INSERT INTO brix_accruals (epoch_date, nft_id, owner, amount, claim_id)"
        " VALUES ('2026-08-18', 'NFT_Z', 'rBob', 1, 2)"
    )
    conn.commit()
    _seed_onchain_claim_event(conn, amount=0)
    results = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 100)}
    assert results["no_orphaned_bindings"].ok is False


def test_audit_fails_on_confirmed_claim_without_tx_hash(conn):
    conn.execute(
        "INSERT INTO brix_claims (claim_id, wallet, amount, state, tx_hash)"
        " VALUES (3, 'rCarol', 1, 'confirmed', NULL)"
    )
    conn.commit()
    _seed_onchain_claim_event(conn, amount=0)
    results = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 100)}
    assert results["confirmed_claims_have_hashes"].ok is False


def test_audit_fails_when_an_epoch_accrued_more_than_the_live_supply(conn):
    brix_drip.record_accruals(
        conn,
        [
            brix_drip.Accrual("2026-08-18", "NFT_1", "rAlice", 1),
            brix_drip.Accrual("2026-08-18", "NFT_2", "rBob", 1),
        ],
    )
    _seed_onchain_claim_event(conn, amount=0)
    results = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 1)}
    assert results["epoch_within_supply"].ok is False
