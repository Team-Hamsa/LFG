"""Regression tests for the final sponsored-mint whole-branch review."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import onchain_listener  # noqa: E402

from lfg_core import (  # noqa: E402
    config,
    economy_store,  # noqa: E402
    history_store,
    market_store,
    mint_flow,  # noqa: E402
    nft_index,
    sponsored_burn,
    sponsored_mint,
    xrpl_ops,
)
from lfg_service import app as server  # noqa: E402

# Fixture ledger ranges must sit above the real earliest-available ledger (32570).
L0 = history_store.EARLIEST_AVAILABLE_LEDGER


def _run(coro):
    return asyncio.run(coro)


def _ready_history(path: str, *, network: str = "mainnet", now: int = 1_000):
    conn = history_store.init_history_db(path)
    history_store.record_archive_baseline(
        conn,
        network=network,
        genesis_hash=f"{network}-genesis",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 500,
        provenance="review-test",
        coverage=json.dumps(
            {
                "version": 1,
                "source_tag": config.SOURCE_TAG,
                "ledger_min": history_store.EARLIEST_AVAILABLE_LEDGER,
                "ledger_max": L0 + 500,
                "accounts": {
                    "signing": config.SIGNING_ACCOUNT,
                    "token_issuer": config.TOKEN_ISSUER_ADDRESS,
                },
            }
        ),
        completed_at=now - 10,
    )
    history_store.record_validated_ledger(
        conn,
        network=network,
        genesis_hash=f"{network}-genesis",
        ledger_index=L0 + 501,
        close_time=now - 1,
        observed_at=now,
    )
    conn.close()


def _listener_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(nft_index._SCHEMA)
    economy_store.init_economy_schema(conn)
    market_store.init_db(conn)
    return conn


async def _none(*_args, **_kwargs):
    return None


def test_archive_health_requires_complete_matching_fresh_provenance(tmp_path):
    history = str(tmp_path / "history.db")
    conn = history_store.init_history_db(history)
    assert not sponsored_mint.archive_is_usable(history, network="mainnet", now=1_000)

    history_store.record_archive_baseline(
        conn,
        network="mainnet",
        genesis_hash="mainnet-genesis",
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=L0 + 500,
        provenance="clio-account-tx-audit",
        coverage=json.dumps(
            {
                "version": 1,
                "source_tag": config.SOURCE_TAG,
                "ledger_min": history_store.EARLIEST_AVAILABLE_LEDGER,
                "ledger_max": L0 + 500,
                "accounts": {
                    "signing": config.SIGNING_ACCOUNT,
                    "token_issuer": config.TOKEN_ISSUER_ADDRESS,
                },
            }
        ),
        completed_at=990,
    )
    history_store.record_validated_ledger(
        conn,
        network="mainnet",
        genesis_hash="mainnet-genesis",
        ledger_index=L0 + 501,
        close_time=999,
        observed_at=1_000,
    )
    conn.close()

    assert sponsored_mint.archive_is_usable(history, network="mainnet", now=1_000)
    assert not sponsored_mint.archive_is_usable(history, network="testnet", now=1_000)
    assert not sponsored_mint.archive_is_usable(history, network="mainnet", now=2_000)


def test_archive_health_rejects_unbound_baseline_coverage(tmp_path):
    history = str(tmp_path / "history.db")
    _ready_history(history, network="mainnet", now=1_000)
    with sqlite3.connect(history) as conn:
        conn.execute(
            "UPDATE archive_state SET baseline_coverage = ?",
            ('{"signing":"rUnbound","token_issuer":"rUnbound"}',),
        )
    assert not sponsored_mint.archive_is_usable(history, network="mainnet", now=1_000)


def test_archive_health_requires_matching_source_tag_and_no_continuity_gap(tmp_path):
    history = str(tmp_path / "history.db")
    _ready_history(history, network="mainnet", now=1_000)

    with sqlite3.connect(history) as conn:
        conn.execute(
            "UPDATE archive_state SET source_tag = ? WHERE network = 'mainnet'",
            (config.SOURCE_TAG + 1,),
        )
    assert not sponsored_mint.archive_is_usable(history, network="mainnet", now=1_000)

    with sqlite3.connect(history) as conn:
        conn.execute(
            "UPDATE archive_state SET source_tag = ? WHERE network = 'mainnet'",
            (config.SOURCE_TAG,),
        )
        history_store.invalidate_archive_continuity(
            conn,
            network="mainnet",
            reason="listener disconnected",
            gap_after=L0 + 501,
            invalidated_at=1_000,
        )
        # Even a corrupt/manual baseline flag cannot conceal the durable gap marker.
        conn.execute("UPDATE archive_state SET baseline_complete = 1")
    assert not sponsored_mint.archive_is_usable(history, network="mainnet", now=1_000)


def test_reservation_serializes_archive_snapshot_against_invalidation(tmp_path, monkeypatch):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    _ready_history(history, network="mainnet", now=101)
    sponsored_mint.start_campaign(db, network="mainnet", actor="admin", now=100)

    writer_started = threading.Event()
    writer_finished = threading.Event()
    writer_thread = None
    real_snapshot = sponsored_mint._archive_eligibility_snapshot

    def snapshot_then_race(*args, **kwargs):
        nonlocal writer_thread
        result = real_snapshot(*args, **kwargs)

        def invalidate():
            writer_started.set()
            conn = history_store.init_history_db(history)
            try:
                history_store.invalidate_archive_continuity(
                    conn,
                    network="mainnet",
                    reason="disconnect during admission",
                    gap_after=L0 + 501,
                    invalidated_at=101,
                )
            finally:
                conn.close()
                writer_finished.set()

        writer_thread = threading.Thread(target=invalidate)
        writer_thread.start()
        assert writer_started.wait(1)
        # BEGIN IMMEDIATE covers both attached DBs, so invalidation cannot
        # linearize between the archive evidence and the claim commit.
        assert not writer_finished.wait(0.1)
        return result

    monkeypatch.setattr(
        sponsored_mint,
        "_archive_eligibility_snapshot",
        snapshot_then_race,
    )
    result = sponsored_mint.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="rAtomicAdmission",
        session_id="session",
        now=101,
    )
    assert writer_thread is not None
    writer_thread.join(timeout=2)
    assert result.sponsored is True
    assert writer_finished.is_set()
    assert not sponsored_mint.archive_is_usable(history, network="mainnet", now=101)


def test_history_schema_has_sourcetag_account_index_and_state(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "history.db"))
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='xrpl_txs'"
        )
    }
    assert "idx_txs_source_tag_account" in indexes
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_state'"
    ).fetchone()


def test_listener_archives_validated_sourcetag_before_event_filters(tmp_path, monkeypatch):
    history = str(tmp_path / "history.db")
    _ready_history(history, network="mainnet")
    hconn = history_store.init_history_db(history)
    conn = _listener_conn()
    tx = {
        "TransactionType": "Payment",
        "Account": "rTagged",
        "SourceTag": config.SOURCE_TAG,
        "hash": "A" * 64,
        "ledger_index": L0 + 502,
        "date": 53,
        "validated": True,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    ctx = {
        "network": "mainnet",
        "genesis_hash": "mainnet-genesis",
        "nft_issuer": "unused",
        "issuer_hex": "00" * 20,
        "brix_issuer": "unused",
        "brix_hex": "unused",
        "numbers": {},
    }
    monkeypatch.setattr(onchain_listener.history_events, "derive_nft_events", lambda *a, **k: [])
    monkeypatch.setattr(onchain_listener.history_events, "derive_brix_events", lambda *a, **k: [])

    _run(
        onchain_listener.process_stream_tx(
            conn,
            tx,
            fetch_token=_none,
            fetch_meta=_none,
            is_ours=lambda _token: False,
            history_conn=hconn,
            history_ctx=ctx,
        )
    )

    row = hconn.execute(
        "SELECT account, source_tag FROM xrpl_txs WHERE tx_hash=?", (tx["hash"],)
    ).fetchone()
    assert tuple(row) == ("rTagged", config.SOURCE_TAG)
    state = history_store.get_archive_state(hconn, "mainnet")
    assert state is not None
    assert state.validated_ledger_index == L0 + 502


def test_campaign_off_does_not_scan_archive(tmp_path, monkeypatch):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    _ready_history(history)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("archive scan should not run while campaign is off")

    monkeypatch.setattr(sponsored_mint, "is_tagged_wallet", forbidden)
    result = sponsored_mint.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="rNew",
        session_id="session",
        now=1_000,
    )
    assert result.reason == "campaign_off"


@pytest.mark.parametrize("terminal_state", ["stopped", "expired"])
def test_orphaned_reversible_reservation_can_rebind_after_campaign_closes(tmp_path, terminal_state):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    _ready_history(history, now=101)
    sponsored_mint.start_campaign(db, network="mainnet", actor="admin", now=100)
    reserved = sponsored_mint.reserve_if_eligible(
        db, history, network="mainnet", wallet="rResume", session_id="lost", now=101
    )
    assert reserved.claim is not None
    if terminal_state == "stopped":
        sponsored_mint.stop_campaign(db, network="mainnet", actor="admin", now=102)
    else:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE free_mint_campaigns SET enabled_until=102 WHERE id=?",
                (reserved.claim.campaign_id,),
            )

    rebound = sponsored_mint.rebind_reservation(
        db,
        network="mainnet",
        wallet="rResume",
        expected_session_id="lost",
        new_session_id="replacement",
        now=103,
    )
    assert rebound is not None
    assert rebound.id == reserved.claim.id
    assert rebound.status == "reserved"
    assert rebound.session_id == "replacement"
    assert (
        sponsored_mint.rebind_reservation(
            db,
            network="mainnet",
            wallet="rResume",
            expected_session_id="lost",
            new_session_id="racer",
            now=104,
        )
        is None
    )


def _reserve_review_claim(tmp_path):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    _ready_history(history, now=101)
    sponsored_mint.start_campaign(db, network="mainnet", actor="admin", now=100)
    result = sponsored_mint.reserve_if_eligible(
        db, history, network="mainnet", wallet="rJournal", session_id="session", now=101
    )
    assert result.claim is not None
    return db, result.claim


def _prepare_claim(db: str):
    return sponsored_mint.record_mint_prepared(
        db,
        network="mainnet",
        wallet="rJournal",
        session_id="session",
        tx_hash="A" * 64,
        tx_blob="BLOB",
        signed_ledger_floor=500,
        nft_number=42,
        metadata_url="https://cdn.example/42.json",
        metadata_json='{"edition":42}',
        body_type="Straight Blue",
        now=102,
    )


def test_prepared_mint_is_exact_reversible_and_only_forward_becomes_irreversible(tmp_path):
    db, reserved = _reserve_review_claim(tmp_path)
    prepared = _prepare_claim(db)
    assert prepared is not None
    assert prepared.id == reserved.id
    assert prepared.status == "reserved"
    assert prepared.mint_signed_tx_hash == "A" * 64
    assert prepared.mint_signed_tx_blob == "BLOB"
    assert prepared.mint_signed_ledger_floor == 500
    assert prepared.mint_nft_number == 42

    # Preparation errors/cancellation do not consume the promised free mint.
    assert sponsored_mint.release_reservation(
        db,
        network="mainnet",
        wallet="rJournal",
        session_id="session",
        reason="cancelled_before_forward",
        now=103,
    )

    # Repeat with a live prepared identity and cross the first-forward boundary.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE free_mint_claims SET status='reserved', released_at=NULL WHERE id=?",
            (reserved.id,),
        )
    forwarded = sponsored_mint.mark_mint_forwarded(
        db,
        network="mainnet",
        wallet="rJournal",
        session_id="session",
        tx_hash="A" * 64,
        now=104,
    )
    assert forwarded is not None and forwarded.started
    assert forwarded.claim.status == "minting"
    assert forwarded.claim.mint_forwarded_at == 104
    assert not sponsored_mint.release_reservation(
        db,
        network="mainnet",
        wallet="rJournal",
        session_id="session",
        reason="too_late",
        now=105,
    )


def test_prepared_identity_is_idempotent_but_conflicts_fail_closed(tmp_path):
    db, _ = _reserve_review_claim(tmp_path)
    first = _prepare_claim(db)
    second = _prepare_claim(db)
    assert first == second
    with pytest.raises(ValueError, match="prepared mint conflicts"):
        sponsored_mint.record_mint_prepared(
            db,
            network="mainnet",
            wallet="rJournal",
            session_id="session",
            tx_hash="C" * 64,
            tx_blob="OTHER",
            signed_ledger_floor=500,
            nft_number=42,
            metadata_url="https://cdn.example/42.json",
            metadata_json='{"edition":42}',
            body_type="Straight Blue",
            now=103,
        )


def test_forwarded_mint_recovery_exposes_exact_journal_after_restart(tmp_path):
    db, _ = _reserve_review_claim(tmp_path)
    _prepare_claim(db)
    sponsored_mint.mark_mint_forwarded(
        db,
        network="mainnet",
        wallet="rJournal",
        session_id="session",
        tx_hash="A" * 64,
        now=103,
    )

    rows = sponsored_mint.mint_recovery_claims(db, network="mainnet")
    assert len(rows) == 1
    claim = rows[0]
    assert claim.mint_signed_tx_hash == "A" * 64
    assert claim.mint_signed_tx_blob == "BLOB"
    assert claim.mint_signed_ledger_floor == 500
    assert claim.mint_forwarded_at == 103


class _SignedMint:
    last_ledger_sequence = 600

    def get_hash(self):
        return "D" * 64

    def blob(self):
        return "SIGNED-MINT-BLOB"


def test_sponsored_mint_prepare_signs_once_with_floor_and_claim_correlation(monkeypatch):
    seen = {}

    async def floor(_client):
        return 500

    def sign(tx, _client, _wallet):
        seen["tx"] = tx
        return _SignedMint()

    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", floor)
    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", sign)
    monkeypatch.setattr(xrpl_ops.Wallet, "from_seed", lambda _seed: SimpleNamespace())
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", lambda _url: SimpleNamespace())

    result = _run(
        xrpl_ops.prepare_sponsored_mint(
            metadata_cdn_url="https://cdn.example/42.json",
            taxon=7,
            issuer=config.SWAP_ISSUER_ADDRESS,
            campaign="claim-123",
        )
    )
    assert result.state == "prepared"
    assert result.tx_hash == "D" * 64
    assert result.tx_blob == "SIGNED-MINT-BLOB"
    assert result.signed_ledger_floor == 500
    assert seen["tx"].source_tag == config.SOURCE_TAG
    assert seen["tx"].nftoken_taxon == 7


def test_sponsored_mint_submit_forwards_only_persisted_blob_and_classifies_exact_hash(
    monkeypatch,
):
    calls = []

    def submit(signed, _client, wallet, *, autofill):
        calls.append((signed, wallet, autofill))
        return SimpleNamespace(
            result={
                "validated": True,
                "hash": "D" * 64,
                "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "NFT42"},
            }
        )

    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", lambda _blob: _SignedMint())
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", submit)
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", lambda _url: SimpleNamespace())

    result = _run(
        xrpl_ops.submit_sponsored_mint(
            signed_tx_blob="SIGNED-MINT-BLOB",
            signed_tx_hash="D" * 64,
        )
    )
    assert result.state == "validated"
    assert result.tx_hash == "D" * 64
    assert result.nft_id == "NFT42"
    assert len(calls) == 1 and calls[0][1:] == (None, False)


def test_sponsored_mint_hash_blob_mismatch_never_forwards(monkeypatch):
    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", lambda _blob: _SignedMint())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("mismatched persisted identity must not be forwarded")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", forbidden)
    result = _run(
        xrpl_ops.submit_sponsored_mint(
            signed_tx_blob="SIGNED-MINT-BLOB",
            signed_tx_hash="E" * 64,
        )
    )
    assert result.state == "indeterminate"
    assert "hash/blob mismatch" in (result.error or "")


def test_sponsored_mint_corrupt_blob_is_indeterminate_and_never_forwards(monkeypatch):
    def corrupt(_blob):
        raise ValueError("corrupt journal")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a corrupt persisted blob must not be forwarded")

    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", corrupt)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", forbidden)
    result = _run(
        xrpl_ops.submit_sponsored_mint(
            signed_tx_blob="CORRUPT-MINT-BLOB",
            signed_tx_hash="E" * 64,
        )
    )
    assert result.state == "indeterminate"
    assert "decode failed" in (result.error or "")


def test_expired_unvalidated_prepared_mint_is_definitively_failed_without_forward(
    monkeypatch,
):
    async def current_ledger(_client):
        return 601

    async def absent(_client, _tx_hash):
        return None

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an expired prepared transaction must not be forwarded")

    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", lambda _blob: _SignedMint())
    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", current_ledger)
    monkeypatch.setattr(xrpl_ops, "_confirm_by_hash", absent)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", forbidden)
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", lambda _url: SimpleNamespace())

    result = _run(
        xrpl_ops.submit_sponsored_mint(
            signed_tx_blob="SIGNED-MINT-BLOB",
            signed_tx_hash="D" * 64,
            prove_expiry=True,
        )
    )

    assert result.state == "failed"
    assert result.tx_hash == "D" * 64
    assert "expired" in (result.error or "")


def test_startup_recovery_reforwards_only_the_exact_persisted_mint(monkeypatch):
    tx_hash = "D" * 64
    claim = SimpleNamespace(
        id="claim",
        network="mainnet",
        wallet="rRecover",
        session_id="session",
        mint_signed_tx_hash=tx_hash,
        mint_signed_tx_blob="EXACT-PERSISTED-BLOB",
        mint_signed_ledger_floor=500,
        mint_forwarded_at=501,
    )
    events = []

    async def reconcile(received_hash):
        events.append(("reconcile", received_hash))
        return xrpl_ops.MintReconciliation(
            False, "indeterminate", received_hash, None, "not found yet"
        )

    async def submit(*, signed_tx_blob, signed_tx_hash, **_kwargs):
        events.append(("submit", signed_tx_blob, signed_tx_hash))
        return xrpl_ops.MintSubmission("validated", signed_tx_hash, "NFT42", None)

    def record(_db, **kwargs):
        events.append(("record", kwargs["mint_tx_hash"], kwargs["nft_id"]))
        return SimpleNamespace(mint_tx_hash=kwargs["mint_tx_hash"], nft_id=kwargs["nft_id"])

    monkeypatch.setattr(
        server.sponsored_mint, "mint_recovery_claims", lambda *args, **kwargs: [claim]
    )
    monkeypatch.setattr(server.xrpl_ops, "reconcile_sponsored_mint", reconcile)
    monkeypatch.setattr(server.xrpl_ops, "submit_sponsored_mint", submit)
    monkeypatch.setattr(server.sponsored_mint, "record_minted_and_enqueue_burn", record)

    _run(server._recover_sponsored_mint_submissions("app.db", network="mainnet"))

    assert events == [
        ("reconcile", tx_hash),
        ("submit", "EXACT-PERSISTED-BLOB", tx_hash),
        ("record", tx_hash, "NFT42"),
    ]


def test_startup_recovery_records_validation_that_preceded_persistence(monkeypatch):
    tx_hash = "C" * 64
    claim = SimpleNamespace(
        id="claim-after-validation",
        network="mainnet",
        wallet="rRecover",
        session_id="session",
        mint_signed_tx_hash=tx_hash,
        mint_signed_tx_blob="EXACT-PERSISTED-BLOB",
        mint_signed_ledger_floor=500,
        mint_forwarded_at=501,
    )
    events = []

    async def reconcile(received_hash):
        events.append(("reconcile", received_hash))
        return xrpl_ops.MintReconciliation(True, "validated", received_hash, "NFT-VALIDATED", None)

    async def forbidden_submit(**_kwargs):
        raise AssertionError("a validated exact hash must not be submitted again")

    def record(_db, **kwargs):
        events.append(("record", kwargs["mint_tx_hash"], kwargs["nft_id"]))
        return SimpleNamespace(mint_tx_hash=kwargs["mint_tx_hash"], nft_id=kwargs["nft_id"])

    monkeypatch.setattr(
        server.sponsored_mint,
        "mint_recovery_claims",
        lambda *args, **kwargs: [claim],
    )
    monkeypatch.setattr(server.xrpl_ops, "reconcile_sponsored_mint", reconcile)
    monkeypatch.setattr(server.xrpl_ops, "submit_sponsored_mint", forbidden_submit)
    monkeypatch.setattr(server.sponsored_mint, "record_minted_and_enqueue_burn", record)

    _run(server._recover_sponsored_mint_submissions("app.db", network="mainnet"))

    assert events == [
        ("reconcile", tx_hash),
        ("record", tx_hash, "NFT-VALIDATED"),
    ]


def test_sponsored_mint_reconciliation_never_submits(monkeypatch):
    class Client:
        def request(self, _request):
            return SimpleNamespace(
                result={
                    "validated": True,
                    "hash": "D" * 64,
                    "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "NFT42"},
                }
            )

    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", lambda _url: Client())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("reconciliation must never submit")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", forbidden)
    result = _run(xrpl_ops.reconcile_sponsored_mint("D" * 64))
    assert result.complete
    assert result.state == "validated"
    assert result.nft_id == "NFT42"


@pytest.mark.parametrize(
    ("crash_point", "irreversible", "released"),
    [("after_prepare", False, True), ("after_forward", True, False)],
)
def test_sponsored_runner_crash_seams_match_first_forward_boundary(
    monkeypatch, crash_point, irreversible, released
):
    events = []
    session = mint_flow.MintSession(
        discord_id="user",
        wallet_address="rCrash",
        sponsored=True,
    )

    async def fake_unit(**kwargs):
        preparation = xrpl_ops.MintPreparation("prepared", "F" * 64, "BLOB", None, 500)
        await kwargs["on_mint_prepared"](
            42,
            "https://cdn.example/42.json",
            '{"edition":42}',
            "Straight Blue",
            preparation,
        )
        events.append("prepared")
        if crash_point == "after_prepare":
            raise RuntimeError("crash after prepare")
        await kwargs["on_mint_forwarded"]("F" * 64)
        events.append("forwarded")
        raise RuntimeError("crash after forward")

    async def prepared(*_args):
        events.append("persist_prepared")

    async def forwarded(_tx_hash):
        events.append("persist_forward")

    async def confirmed(*_args):
        raise AssertionError("mint did not validate in this crash seam")

    async def offered(*_args):
        raise AssertionError("offer stage was not reached")

    async def allocate():
        return 42

    releases = []

    def release(_session, _reason):
        releases.append(True)
        return True

    monkeypatch.setattr(mint_flow, "mint_one_unit", fake_unit)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", allocate)
    monkeypatch.setattr(mint_flow, "release_sponsored_reservation", release)
    monkeypatch.setattr(mint_flow, "settle_headroom", lambda _session: None)
    monkeypatch.setattr(
        mint_flow.sponsored_mint,
        "claim_for_session",
        lambda *a, **k: SimpleNamespace(
            status="reserved",
            nft_id=None,
            mint_signed_tx_hash=None,
            mint_signed_tx_blob=None,
            mint_signed_ledger_floor=None,
        ),
    )

    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_prepared=prepared,
            on_sponsored_forwarded=forwarded,
            on_sponsored_mint=confirmed,
            on_sponsored_offer=offered,
        )
    )
    assert session.state == mint_flow.FAILED
    assert session.sponsorship_irreversible is irreversible
    assert bool(releases) is released
    if crash_point == "after_prepare":
        assert events == ["persist_prepared", "prepared"]
    else:
        assert events == [
            "persist_prepared",
            "prepared",
            "persist_forward",
            "forwarded",
        ]


def test_offer_persistence_replays_acceptance_that_won_the_race(tmp_path):
    db, claim = _reserve_review_claim(tmp_path)
    history = str(tmp_path / "history.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE free_mint_claims SET status='minted', nft_id='NFT42', "
            "mint_tx_hash=? WHERE id=?",
            ("A" * 64, claim.id),
        )
    accept_tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rJournal",
        "SourceTag": config.SOURCE_TAG,
        "NFTokenSellOffer": "OFFER42",
        "hash": "C" * 64,
        "validated": True,
        "date": 53,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    with sqlite3.connect(history) as conn:
        conn.execute(
            """
            INSERT INTO xrpl_txs (
                tx_hash, ledger_index, close_time, tx_type, account, source_tag, raw_json
            ) VALUES (?, 502, 999, 'NFTokenAcceptOffer', ?, ?, ?)
            """,
            (
                accept_tx["hash"],
                accept_tx["Account"],
                config.SOURCE_TAG,
                __import__("json").dumps(accept_tx),
            ),
        )

    offered = sponsored_mint.record_offer(
        db,
        network="mainnet",
        wallet="rJournal",
        session_id="session",
        offer_id="OFFER42",
        history_path=history,
        now=103,
    )
    assert offered is not None
    assert offered.status == "accepted"
    assert offered.accept_tx_hash == "C" * 64
    assert offered.offer_id == "OFFER42"


def test_offer_persistence_does_not_replay_from_an_invalidated_archive(tmp_path):
    db, claim = _reserve_review_claim(tmp_path)
    history = str(tmp_path / "history.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE free_mint_claims SET status='minted', nft_id='NFT42', "
            "mint_tx_hash=? WHERE id=?",
            ("A" * 64, claim.id),
        )
    accept_tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": "rJournal",
        "SourceTag": config.SOURCE_TAG,
        "NFTokenSellOffer": "OFFER42",
        "hash": "C" * 64,
        "validated": True,
        "date": 53,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    with sqlite3.connect(history) as conn:
        conn.execute(
            """
            INSERT INTO xrpl_txs (
                tx_hash, ledger_index, close_time, tx_type, account, source_tag, raw_json
            ) VALUES (?, 502, 999, 'NFTokenAcceptOffer', ?, ?, ?)
            """,
            (
                accept_tx["hash"],
                accept_tx["Account"],
                config.SOURCE_TAG,
                __import__("json").dumps(accept_tx),
            ),
        )
        history_store.invalidate_archive_continuity(
            conn,
            network="mainnet",
            reason="listener disconnected",
            gap_after=L0 + 501,
            invalidated_at=102,
        )

    offered = sponsored_mint.record_offer(
        db,
        network="mainnet",
        wallet="rJournal",
        session_id="session",
        offer_id="OFFER42",
        history_path=history,
        now=103,
    )
    assert offered is not None
    assert offered.status == "offered"
    assert offered.accept_tx_hash is None


def test_earliest_burn_schema_migrates_amount_source_and_immutable_scope(tmp_path, monkeypatch):
    db = str(tmp_path / "legacy.db")
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE free_mint_campaigns (
                id TEXT PRIMARY KEY, network TEXT NOT NULL, status TEXT NOT NULL,
                started_at INTEGER NOT NULL, enabled_until INTEGER NOT NULL,
                stopped_at INTEGER, started_by TEXT NOT NULL, stopped_by TEXT,
                cap INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            CREATE TABLE free_mint_claims (
                id TEXT PRIMARY KEY, network TEXT NOT NULL, wallet TEXT NOT NULL,
                campaign_id TEXT NOT NULL, session_id TEXT NOT NULL, status TEXT NOT NULL,
                reserved_at INTEGER NOT NULL, reservation_expires_at INTEGER,
                released_at INTEGER, mint_tx_hash TEXT, nft_id TEXT, offer_id TEXT,
                accept_tx_hash TEXT, tagged_at INTEGER, last_error TEXT,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                UNIQUE(network, wallet)
            );
            CREATE TABLE free_mint_burns (
                id TEXT PRIMARY KEY, claim_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                memo_id TEXT NOT NULL UNIQUE, tx_hash TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at INTEGER, next_attempt_at INTEGER, last_error TEXT,
                burned_at INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            INSERT INTO free_mint_campaigns VALUES
                ('campaign', 'testnet', 'stopped', 1, 2, 2, 'a', 'a', 100, 1, 2);
            INSERT INTO free_mint_claims VALUES
                ('00112233445566778899aabbccddeeff', 'testnet', 'rLegacy', 'campaign',
                 'session', 'minted', 1, 2, NULL, 'MINT', 'NFT', NULL, NULL, NULL,
                 NULL, 1, 2);
            INSERT INTO free_mint_burns VALUES
                ('burn', '00112233445566778899aabbccddeeff', 'pending',
                 'fm-0011223344556677', NULL, 0, NULL, 2, NULL, NULL, 1, 2);
            """
        )
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "9.5")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rMigrationSource")
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rMigrationIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "MIGRATIONHEX")
    monkeypatch.setattr(config, "SOURCE_TAG", 12345)

    sponsored_mint.ensure_schema(db)

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM free_mint_burns").fetchone()
        assert row is not None
        assert row["amount"] == "9.5"
        assert row["source_account"] == "rMigrationSource"
        assert row["network"] == "testnet"
        assert row["issuer"] == "rMigrationIssuer"
        assert row["currency"] == "MIGRATIONHEX"
        assert row["source_tag"] == 12345


def test_burn_worker_rejects_immutable_scope_that_no_longer_matches_config(tmp_path, monkeypatch):
    db, claim = _reserve_review_claim(tmp_path)
    prepared = sponsored_mint.record_mint_prepared(
        db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="D" * 64,
        tx_blob="BLOB",
        signed_ledger_floor=500,
        nft_number=42,
        metadata_url="https://cdn.example/42.json",
        metadata_json='{"name":"LFG #42"}',
        body_type="Alien",
        now=102,
    )
    assert prepared is not None
    assert sponsored_mint.mark_mint_forwarded(
        db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="D" * 64,
        now=103,
    )
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "7.25")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rSnapshotSource")
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rSnapshotIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "SNAPSHOTHEX")
    monkeypatch.setattr(config, "SOURCE_TAG", 444)
    sponsored_mint.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="D" * 64,
        nft_id="NFT42",
        now=104,
    )
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rChangedIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "CHANGEDHEX")
    monkeypatch.setattr(config, "SOURCE_TAG", 999)
    seen = []

    async def prepare(_memo, **kwargs):
        seen.append(kwargs)
        return xrpl_ops.BurnPreparation("noop", None, None, "test")

    assert not _run(
        sponsored_burn.process_one(
            db, network="testnet", prepare=prepare, submit=None, reconcile=None, now=200
        )
    )
    assert _run(
        sponsored_burn.process_one(
            db, network="mainnet", prepare=prepare, submit=None, reconcile=None, now=200
        )
    )
    assert seen == []
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, last_error FROM free_mint_burns").fetchone()
    assert row[0] == "failed_terminal"
    assert "scope mismatch" in row[1]


def test_burn_worker_rejects_burn_claim_network_mismatch(tmp_path, monkeypatch):
    db, claim = _reserve_review_claim(tmp_path)
    _prepare_claim(db)
    sponsored_mint.mark_mint_forwarded(
        db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="A" * 64,
        now=103,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="A" * 64,
        nft_id="NFT-MISMATCH",
        now=104,
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE free_mint_burns SET network='testnet'")
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("mismatched claim scope must not prepare a transaction")

    assert _run(
        sponsored_burn.process_one(
            db,
            network="testnet",
            prepare=forbidden,
            submit=None,
            reconcile=None,
            now=200,
        )
    )

    assert calls == []
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, last_error FROM free_mint_burns").fetchone()
    assert row[0] == "failed_terminal"
    assert "claim network" in row[1]


def test_self_issuer_burn_is_only_a_testnet_noop(monkeypatch):
    # Each case pins config.XRPL_NETWORK to the obligation's own network.
    # prepare_sponsored_burn now checks network mismatch BEFORE the self-issuer
    # topology (CodeRabbit on #328): reversed, a testnet-scoped obligation
    # processed on mainnet took the self-issuer branch and returned "noop",
    # which process_one records as burned/self_issuer_noop — discharging a real
    # LFGO debt with no ledger effect. Passing network="mainnet" while the
    # ambient config is testnet now correctly reports the mismatch, so the
    # topology assertions need a matching network to reach the branch at all.
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rIssuer")

    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    mainnet = _run(
        xrpl_ops.prepare_sponsored_burn(
            "fm-review", network="mainnet", source_account="rIssuer", issuer="rIssuer"
        )
    )
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    testnet = _run(
        xrpl_ops.prepare_sponsored_burn(
            "fm-review", network="testnet", source_account="rIssuer", issuer="rIssuer"
        )
    )
    assert mainnet.state == "failed"
    assert "mainnet" in (mainnet.error or "")
    assert testnet.state == "noop"


def test_cross_network_obligation_never_discharges_as_a_self_issuer_noop(monkeypatch):
    """The guard-order regression itself: a testnet-scoped self-issuer
    obligation reaching a mainnet worker must FAIL, not return the "noop" that
    process_one would record as a settled burn."""
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")

    result = _run(
        xrpl_ops.prepare_sponsored_burn(
            "fm-review", network="testnet", source_account="rIssuer", issuer="rIssuer"
        )
    )

    assert result.state == "failed"
    assert "network does not match" in (result.error or "")


def test_mainnet_campaign_rejects_self_issuer_topology(tmp_path, monkeypatch):
    account = xrpl_ops.Wallet.create().classic_address
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", account)
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", account)

    with pytest.raises(ValueError, match="signing account distinct from the token issuer"):
        sponsored_mint.start_campaign(
            str(tmp_path / "app.db"), network="mainnet", actor="admin", now=100
        )


def test_active_mainnet_campaign_stops_admitting_after_self_issuer_misconfiguration(
    tmp_path, monkeypatch
):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    _ready_history(history, network="mainnet", now=101)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rDistinctSigner")
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rDistinctIssuer")
    sponsored_mint.start_campaign(db, network="mainnet", actor="admin", now=100)

    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rDistinctSigner")
    result = sponsored_mint.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="rNewcomer",
        session_id="session",
        now=101,
    )

    assert result.sponsored is False
    assert result.reason == "invalid_topology"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM free_mint_claims").fetchone()[0] == 0


def test_concurrent_mint_offer_and_burn_share_transaction_account_coordinator(monkeypatch):
    """All backend tx builders serialize on Account, even with a regular key."""

    transaction_account = xrpl_ops.Wallet.create().classic_address
    regular_key = xrpl_ops.Wallet.create().classic_address
    token_issuer = xrpl_ops.Wallet.create().classic_address
    destination = xrpl_ops.Wallet.create().classic_address
    fake_wallet = SimpleNamespace(classic_address=regular_key)
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0
    signed_counter = 0

    class FakeSigned:
        def __init__(self, tx_hash: str):
            self._tx_hash = tx_hash

        def get_hash(self):
            return self._tx_hash

        def blob(self):
            return f"blob-{self._tx_hash}"

    def enter_critical_section():
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)

    def leave_critical_section():
        nonlocal active
        with state_lock:
            active -= 1

    def fake_sign(_tx, _client, _wallet):
        nonlocal signed_counter
        enter_critical_section()
        try:
            with state_lock:
                signed_counter += 1
                tx_hash = f"{signed_counter:064X}"
            return FakeSigned(tx_hash)
        finally:
            leave_critical_section()

    def fake_submit(signed, _client, _wallet, *, autofill=True):
        enter_critical_section()
        try:
            return SimpleNamespace(
                result={
                    "validated": True,
                    "hash": signed.get_hash() if isinstance(signed, FakeSigned) else "ADMIN-BURN",
                    "meta": {
                        "TransactionResult": "tesSUCCESS",
                        "offer_id": "E" * 64,
                    },
                }
            )
        finally:
            leave_critical_section()

    async def ledger_index(_client):
        return 500

    monkeypatch.setattr(config, "SIGNING_ACCOUNT", transaction_account)
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", token_issuer)
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(xrpl_ops.Wallet, "from_seed", lambda _seed: fake_wallet)
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", lambda _url: object())
    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", ledger_index)
    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", fake_sign)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)

    import surfaces.discord_bot.admin as discord_admin

    monkeypatch.setattr(discord_admin, "submit_and_wait", fake_submit)

    async def run_all():
        return await asyncio.gather(
            xrpl_ops.prepare_sponsored_mint(
                "https://cdn.example/nft.json",
                1,
                transaction_account,
                campaign="claim-1",
            ),
            xrpl_ops.create_nft_offer(
                "A" * 64,
                destination,
                campaign="claim-1",
            ),
            xrpl_ops.prepare_sponsored_burn(
                "claim-1",
                source_account=transaction_account,
                network="testnet",
                issuer=token_issuer,
            ),
            xrpl_ops.buy_and_burn(
                config.TOKEN_CURRENCY_HEX,
                token_issuer,
                "1",
            ),
            discord_admin.burn_nft("B" * 64),
        )

    mint, offer, burn, legacy_burn, admin_burn = _run(run_all())

    assert mint.state == "prepared"
    assert offer == "E" * 64
    assert burn.state == "prepared"
    assert legacy_burn is not None
    assert regular_key != transaction_account
    assert admin_burn is True
    assert maximum_active == 1
