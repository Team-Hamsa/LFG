"""BRIX daily drip: schema, accrual store, and pure accrual evaluation (#48).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from lfg_core import brix_drip
from lfg_core.epoch_state import EpochToken
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


class _FakeReplay:
    """Stands in for epoch_state.EpochReplay: a fixed state for every epoch."""

    def __init__(self, tokens):
        self._tokens = tokens

    def advance_to(self, epoch):
        return self._tokens


def _tok(nft_id, owner="rHolder", listed=False, live=True):
    return EpochToken(nft_id=nft_id, owner=owner, listed=listed, live=live)


def _all_eligible(tokens):
    """Default eligibility for the fixtures: every replayed token is a
    collection character (the #411 C1 filter is exercised explicitly below)."""
    return {nft_id: tok.owner for nft_id, tok in tokens.items()}


def _run(conn, tokens, *, today, certify=lambda c, n, e: None, eligible=None):
    return brix_drip.run_archive_accrual(
        conn,
        "testnet",
        frozenset(),
        today=today,
        eligible=_all_eligible(tokens) if eligible is None else eligible,
        certify=certify,
        replay_factory=lambda c: _FakeReplay(tokens),
    )


def test_run_archive_accrual_advances_cursor_and_is_a_no_op_on_rerun(conn):
    tokens = {"A": _tok("A", "rAlice"), "B": _tok("B", "rBob")}
    reports = _run(conn, tokens, today="2026-08-19")
    assert [r.epoch for r in reports] == ["2026-08-18"]
    assert reports[0].accrued == 2 and reports[0].deferred is None
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-18"
    assert _run(conn, tokens, today="2026-08-19") == []
    assert conn.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 2


def test_run_archive_accrual_reports_skip_reasons(conn):
    tokens = {
        "A": _tok("A", "rAlice"),
        "B": _tok("B", "rBob", listed=True),
        "C": _tok("C", "rCarol", listed=None),
        "D": _tok("D", "rDave", live=False),
        "E": _tok("E", "rSys"),
        # Padding: one unknown out of three decidable tokens would trip the
        # #411 I1 mass-unknown deferral, which this test is not about.
        **{f"P{i}": _tok(f"P{i}", f"rPad{i}") for i in range(10)},
    }
    reports = brix_drip.run_archive_accrual(
        conn,
        "testnet",
        frozenset({"rSys"}),
        today="2026-08-19",
        eligible=_all_eligible(tokens),
        certify=lambda c, n, e: None,
        replay_factory=lambda c: _FakeReplay(tokens),
    )
    r = reports[0]
    assert r.deferred is None
    assert (r.accrued, r.skipped_listed, r.unknown, r.skipped_burned, r.skipped_system) == (
        11,
        1,
        1,
        1,
        1,
    )


def test_run_archive_accrual_catch_up_writes_every_missed_epoch(conn):
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, "2026-08-15")
    reports = _run(conn, {"A": _tok("A", "rAlice")}, today="2026-08-19")
    assert [r.epoch for r in reports] == ["2026-08-16", "2026-08-17", "2026-08-18"]
    assert brix_drip.claimable(conn, "rAlice") == 3
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-18"


def test_uncertified_epoch_defers_and_leaves_cursor_behind(conn):
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, "2026-08-15")
    gated = {"2026-08-17"}
    certify = lambda c, n, e: "gap" if e in gated else None  # noqa: E731
    reports = _run(conn, {"A": _tok("A", "rAlice")}, today="2026-08-19", certify=certify)
    assert [(r.epoch, r.deferred) for r in reports] == [("2026-08-16", None), ("2026-08-17", "gap")]
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) == "2026-08-16"
    assert brix_drip.claimable(conn, "rAlice") == 1
    # gap healed → next run completes 17 and 18 exactly once
    again = _run(conn, {"A": _tok("A", "rAlice")}, today="2026-08-19")
    assert [r.epoch for r in again] == ["2026-08-17", "2026-08-18"]
    assert brix_drip.claimable(conn, "rAlice") == 3
    assert conn.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 3


def test_listed_token_earns_nothing_from_archive_fixtures(conn, tmp_path):
    """Regression driven by the real replay, not a mocked RPC (spec §Testing)."""
    from lfg_core import epoch_state, history_store

    h = history_store.init_history_db(str(tmp_path / "h.db"))
    brix_drip.ensure_schema(h)
    h.execute(
        "INSERT INTO archive_state (network, genesis_hash, baseline_complete, validated_close_time,"
        " updated_at) VALUES ('testnet', ?, 1, ?, 1)",
        ("G" * 64, 4102444800),
    )
    for i, (ev, kw) in enumerate(
        [
            ("mint", {"nft_id": "L", "to_addr": "rAlice"}),
            (
                "offer_create",
                {"nft_id": "L", "from_addr": "rAlice", "offer_index": "O1", "offer_flags": 1},
            ),
            ("mint", {"nft_id": "U", "to_addr": "rBob"}),
        ]
    ):
        history_store.insert_nft_event(
            h, {"tx_hash": f"T{i}", "event": ev, "ts": 1767225600 + i, "ledger_index": i, **kw}
        )
    h.commit()
    brix_drip.set_meta(h, brix_drip.LAST_ACCRUED_EPOCH, "2025-12-31")
    reports = brix_drip.run_archive_accrual(
        h, "testnet", frozenset(), today="2026-01-03", eligible={"L": "rAlice", "U": "rBob"}
    )
    assert reports[-1].epoch == "2026-01-02"
    assert brix_drip.claimable(h, "rAlice") == 0
    assert brix_drip.claimable(h, "rBob") == 2  # 01-01 and 01-02
    assert epoch_state.state_at_epoch(h, "2026-01-02")["L"].listed is True


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
    results = brix_drip.audit_distribution(
        conn, distributor="rDistributor", token_supply_ceiling=100
    )
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


def test_audit_supply_ceiling_counts_burned_tokens_too(conn):
    """A past epoch legitimately accrued for tokens that have since been
    burned. Comparing against today's LIVE count would report false drift and
    fail the audit on a perfectly correct history."""
    brix_drip.record_accruals(
        conn,
        [
            brix_drip.Accrual("2026-08-18", "NFT_1", "rAlice", 1),
            brix_drip.Accrual("2026-08-18", "NFT_2", "rBob", 1),
            brix_drip.Accrual("2026-08-18", "NFT_3", "rCarol", 1),
        ],
    )
    _seed_onchain_claim_event(conn, amount=0)
    # 3 tokens existed then; only 1 is live now, but 3 were ever minted.
    results = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 3)}
    assert results["epoch_within_supply"].ok is True


# --- endpoint chain identity ----------------------------------------------


def test_verify_endpoint_chain_refuses_a_wrong_chain_endpoint(conn, monkeypatch):
    """--network matching XRPL_NETWORK does not prove the endpoint is on that
    chain: XRPL_JSON_RPC_URL overrides it independently. On the wrong chain
    every token looks unlisted, and unlisted is what pays."""
    from lfg_core import history_store

    monkeypatch.setattr(
        history_store,
        "get_archive_state",
        lambda c, net: type("S", (), {"genesis_hash": "EXPECTED_HASH"})(),
    )

    async def wrong_chain(request_fn):
        return history_store.EndpointSnapshot(
            genesis_hash="OTHER_CHAIN_HASH", validated_ledger_index=100
        )

    monkeypatch.setattr(history_store, "fetch_endpoint_snapshot", wrong_chain)
    error = asyncio.run(brix_drip.verify_endpoint_chain(conn, "testnet"))
    assert error is not None
    assert "not on the testnet chain" in error


def test_verify_endpoint_chain_accepts_a_matching_endpoint(conn, monkeypatch):
    from lfg_core import history_store

    monkeypatch.setattr(
        history_store,
        "get_archive_state",
        lambda c, net: type("S", (), {"genesis_hash": "EXPECTED_HASH"})(),
    )

    async def right_chain(request_fn):
        return history_store.EndpointSnapshot(
            genesis_hash="EXPECTED_HASH", validated_ledger_index=100
        )

    monkeypatch.setattr(history_store, "fetch_endpoint_snapshot", right_chain)
    assert asyncio.run(brix_drip.verify_endpoint_chain(conn, "testnet")) is None


def test_verify_endpoint_chain_is_a_no_op_without_a_recorded_identity(conn, monkeypatch):
    """Testnet with an uncertified archive: nothing trustworthy to compare
    against, so don't fabricate a verdict — or query the endpoint at all.
    Mainnet never reaches this branch (it has a hardcoded identity)."""
    from lfg_core import history_store

    monkeypatch.setattr(history_store, "get_archive_state", lambda c, net: None)

    async def explode(request_fn):
        raise AssertionError("must not query the endpoint with nothing to compare")

    monkeypatch.setattr(history_store, "fetch_endpoint_snapshot", explode)
    assert asyncio.run(brix_drip.verify_endpoint_chain(conn, "testnet")) is None


def test_verify_endpoint_chain_checks_mainnet_without_any_archive_identity(conn, monkeypatch):
    """Mainnet's ledger-32570 hash is permanent, so the wrong-chain guard must
    NOT depend on the archive having been certified first — otherwise an
    uncertified mainnet archive silently disables the check that protects real
    money."""
    from lfg_core import history_store

    monkeypatch.setattr(history_store, "get_archive_state", lambda c, net: None)

    async def wrong_chain(request_fn):
        return history_store.EndpointSnapshot(
            genesis_hash="TESTNET_HASH", validated_ledger_index=100
        )

    monkeypatch.setattr(history_store, "fetch_endpoint_snapshot", wrong_chain)
    error = asyncio.run(brix_drip.verify_endpoint_chain(conn, "mainnet"))
    assert error is not None
    assert "not on the mainnet chain" in error


def test_verify_endpoint_chain_accepts_the_real_mainnet_identity(conn, monkeypatch):
    from lfg_core import history_store

    monkeypatch.setattr(history_store, "get_archive_state", lambda c, net: None)

    async def right_chain(request_fn):
        return history_store.EndpointSnapshot(
            genesis_hash=brix_drip.MAINNET_GENESIS_HASH, validated_ledger_index=100
        )

    monkeypatch.setattr(history_store, "fetch_endpoint_snapshot", right_chain)
    assert asyncio.run(brix_drip.verify_endpoint_chain(conn, "mainnet")) is None


def test_verify_endpoint_chain_ignores_hash_case(conn, monkeypatch):
    """Ledger hashes are hex; a case-only difference between the server's
    rendering and the stored value must not reject a correct endpoint."""
    from lfg_core import history_store

    monkeypatch.setattr(history_store, "get_archive_state", lambda c, net: None)

    async def lowercased(request_fn):
        return history_store.EndpointSnapshot(
            genesis_hash=brix_drip.MAINNET_GENESIS_HASH.lower(), validated_ledger_index=100
        )

    monkeypatch.setattr(history_store, "fetch_endpoint_snapshot", lowercased)
    assert asyncio.run(brix_drip.verify_endpoint_chain(conn, "mainnet")) is None


def test_audit_fails_when_accruals_exist_but_the_index_is_empty(conn):
    """Accruals for tokens the index does not know is a real contradiction —
    an empty, unavailable, or wrong-network index — not an un-runnable check."""
    brix_drip.record_accruals(conn, [brix_drip.Accrual("2026-08-18", "NFT_1", "rAlice", 1)])
    _seed_onchain_claim_event(conn, amount=0)
    results = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 0)}
    assert results["epoch_within_supply"].ok is False
    assert results["epoch_within_supply"].skipped is False


def test_audit_reports_skipped_not_passed_on_a_fresh_install(conn):
    """No tokens and no accruals: nothing to verify. Reporting PASS would
    claim a clean bill of health nothing actually earned."""
    _seed_onchain_claim_event(conn, amount=0)
    result = {r.name: r for r in brix_drip.audit_distribution(conn, "rDistributor", 0)}[
        "epoch_within_supply"
    ]
    assert result.skipped is True
    assert result.ok is True


# --- #411 fix wave: eligibility scope, owner drift, mass-unknown deferral ---


def test_tokens_outside_the_collection_index_never_accrue(conn):
    """C1: nft_events also carries Closet (taxon 1762) and trait (176) tokens.
    They were never drip-eligible; only onchain_nfts membership pays."""
    tokens = {"CHAR": _tok("CHAR", "rAlice"), "CLOSET": _tok("CLOSET", "rAlice")}
    reports = _run(conn, tokens, today="2026-08-19", eligible={"CHAR": "rAlice"})
    r = reports[0]
    assert (r.accrued, r.skipped_ineligible) == (1, 1)
    assert brix_drip.claimable(conn, "rAlice") == 1
    assert [row[0] for row in conn.execute("SELECT nft_id FROM brix_accruals")] == ["CHAR"]


def test_owner_drift_on_the_newest_epoch_blocks_payment(conn):
    """C2: a replayed owner that disagrees with the index means nft_events is
    missing an accept — the token must not pay to the stale wallet."""
    tokens = {"A": _tok("A", "rStale"), "B": _tok("B", "rBob")}
    # Padding keeps the drift-induced unknown under the I1 mass-unknown
    # threshold, so this test isolates the drift behaviour.
    tokens.update({f"P{i}": _tok(f"P{i}", f"rPad{i}") for i in range(20)})
    eligible = {"A": "rFresh", "B": "rBob"}
    eligible.update({f"P{i}": f"rPad{i}" for i in range(20)})
    reports = _run(conn, tokens, today="2026-08-19", eligible=eligible)
    r = reports[0]
    assert (r.accrued, r.owner_drift, r.unknown) == (21, 1, 1)
    assert brix_drip.claimable(conn, "rStale") == 0
    assert brix_drip.claimable(conn, "rBob") == 1


def test_owner_drift_is_not_checked_on_older_catch_up_epochs(conn):
    """The same mismatch on a catch-up epoch is legitimate history — the token
    genuinely had a different owner then — so it still earns."""
    brix_drip.set_meta(conn, brix_drip.LAST_ACCRUED_EPOCH, "2026-08-16")
    tokens = {"A": _tok("A", "rStale"), "B": _tok("B", "rBob")}
    # Padding keeps the drift-induced unknown under the I1 mass-unknown
    # threshold, so this test isolates the drift behaviour.
    tokens.update({f"P{i}": _tok(f"P{i}", f"rPad{i}") for i in range(20)})
    eligible = {"A": "rFresh", "B": "rBob"}
    eligible.update({f"P{i}": f"rPad{i}" for i in range(20)})
    reports = _run(conn, tokens, today="2026-08-19", eligible=eligible)
    assert [(r.epoch, r.owner_drift) for r in reports] == [
        ("2026-08-17", 0),
        ("2026-08-18", 1),
    ]
    # Paid for 08-17 (no drift check) but not for 08-18 (drift).
    assert brix_drip.claimable(conn, "rStale") == 1


def test_mass_unknown_epoch_defers_and_leaves_the_cursor_behind(conn):
    """I1: a stale derived table makes almost everything unknown. Writing ~0
    rows and advancing the cursor would silently forfeit the day forever."""
    tokens = {f"N{i}": _tok(f"N{i}", f"r{i}", listed=None) for i in range(9)}
    tokens["OK"] = _tok("OK", "rOk")
    reports = _run(conn, tokens, today="2026-08-19")
    r = reports[0]
    assert r.deferred is not None and "derive_history_events" in r.deferred
    assert r.accrued == 0
    assert conn.execute("SELECT COUNT(*) FROM brix_accruals").fetchone()[0] == 0
    assert brix_drip.get_meta(conn, brix_drip.LAST_ACCRUED_EPOCH) is None


def test_a_few_unknowns_below_the_threshold_still_accrue(conn):
    tokens = {f"N{i}": _tok(f"N{i}", f"r{i}") for i in range(20)}
    tokens["U"] = _tok("U", "rUnknown", listed=None)
    reports = _run(conn, tokens, today="2026-08-19")
    assert reports[0].deferred is None and reports[0].accrued == 20
