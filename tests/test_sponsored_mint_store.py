import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import config
from lfg_core import sponsored_mint as sm
from tests.sponsored_helpers import prepare_and_forward, ready_history


def paths(tmp_path, *, network="mainnet"):
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    ready_history(history, network=network)
    return db, history


def insert_tagged(history, wallet):
    with sqlite3.connect(history) as conn:
        conn.execute(
            """
            INSERT INTO xrpl_txs (
                tx_hash, ledger_index, close_time, tx_type,
                account, source_tag, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (f"tx-{wallet}", 1, 1, "Payment", wallet, config.SOURCE_TAG, "{}"),
        )


def test_campaign_defaults_off_and_expires_at_3600_seconds(tmp_path):
    db, history = paths(tmp_path)
    sm.ensure_schema(db)
    assert sm.campaign_status(db, history, network="mainnet", now=100).state == "off"
    started = sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert started.enabled_until == 3700
    assert sm.campaign_status(db, history, network="mainnet", now=3699).state == "active"
    assert sm.campaign_status(db, history, network="mainnet", now=3700).state == "expired"


def test_known_tagged_and_project_wallets_are_not_eligible(tmp_path, monkeypatch):
    db, history = paths(tmp_path)
    insert_tagged(history, "rKnown")
    monkeypatch.setattr(config, "SPONSORED_MINT_EXCLUDED_WALLETS", ("rProject",))
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    assert not sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rKnown", session_id="s1", now=101
    ).sponsored
    assert not sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rProject", session_id="s2", now=101
    ).sponsored


def test_archive_missing_or_without_required_schema_fails_closed(tmp_path):
    db = str(tmp_path / "app.db")
    missing = str(tmp_path / "missing.db")
    malformed = str(tmp_path / "malformed.db")
    sqlite3.connect(malformed).close()
    sm.start_campaign(db, network="mainnet", actor="42", now=100)

    assert (
        sm.reserve_if_eligible(
            db, missing, network="mainnet", wallet="rNew", session_id="s1", now=101
        ).reason
        == "eligibility_unavailable"
    )
    assert (
        sm.reserve_if_eligible(
            db, malformed, network="mainnet", wallet="rNew", session_id="s2", now=101
        ).reason
        == "eligibility_unavailable"
    )
    assert not (tmp_path / "missing.db").exists()


def test_wallet_normalization_strips_whitespace_but_preserves_case(tmp_path):
    db, history = paths(tmp_path)
    insert_tagged(history, "rCaseSensitive")
    sm.start_campaign(db, network="mainnet", actor="42", now=100)

    assert not sm.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="  rCaseSensitive  ",
        session_id="s1",
        now=101,
    ).sponsored
    result = sm.reserve_if_eligible(
        db,
        history,
        network="mainnet",
        wallet="rcasesensitive",
        session_id="s2",
        now=101,
    )
    assert result.sponsored
    assert result.claim is not None
    assert result.claim.wallet == "rcasesensitive"


def test_testnet_campaign_reserves_unseen_wallet(tmp_path):
    db, history = paths(tmp_path, network="testnet")
    sm.start_campaign(db, network="testnet", actor="42", now=100)

    result = sm.reserve_if_eligible(
        db, history, network="testnet", wallet="rNew", session_id="s1", now=101
    )

    assert result.sponsored
    assert result.claim is not None
    assert result.claim.network == "testnet"
    assert sm.campaign_status(db, history, network="testnet", now=101).reserved == 1


def test_mainnet_tagged_history_does_not_make_testnet_wallet_ineligible(tmp_path):
    main_dir = tmp_path / "mainnet"
    test_dir = tmp_path / "testnet"
    main_dir.mkdir()
    test_dir.mkdir()
    main_db, main_history = paths(main_dir)
    test_db, test_history = paths(test_dir, network="testnet")
    insert_tagged(main_history, "rSameWallet")
    sm.start_campaign(main_db, network="mainnet", actor="42", now=100)
    sm.start_campaign(test_db, network="testnet", actor="42", now=100)

    main = sm.reserve_if_eligible(
        main_db,
        main_history,
        network="mainnet",
        wallet="rSameWallet",
        session_id="main-session",
        now=101,
    )
    test = sm.reserve_if_eligible(
        test_db,
        test_history,
        network="testnet",
        wallet="rSameWallet",
        session_id="test-session",
        now=101,
    )

    assert main.reason == "ineligible"
    assert test.sponsored
    with sqlite3.connect(main_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM free_mint_claims").fetchone()[0] == 0
    with sqlite3.connect(test_db) as conn:
        assert conn.execute("SELECT network, wallet FROM free_mint_claims").fetchone() == (
            "testnet",
            "rSameWallet",
        )


def test_campaign_and_claim_keys_do_not_leak_between_supported_networks(tmp_path):
    db, main_history = paths(tmp_path)
    test_history = str(tmp_path / "history-testnet.db")
    ready_history(test_history, network="testnet")
    sm.start_campaign(db, network="mainnet", actor="42", now=100)

    before_test_campaign = sm.reserve_if_eligible(
        db,
        test_history,
        network="testnet",
        wallet="rCrossNetwork",
        session_id="test-session",
        now=101,
    )
    sm.start_campaign(db, network="testnet", actor="42", now=102)
    main = sm.reserve_if_eligible(
        db,
        main_history,
        network="mainnet",
        wallet="rCrossNetwork",
        session_id="main-session",
        now=103,
    )
    test = sm.reserve_if_eligible(
        db,
        test_history,
        network="testnet",
        wallet="rCrossNetwork",
        session_id="test-session",
        now=103,
    )

    assert before_test_campaign.reason == "campaign_off"
    assert main.sponsored and test.sponsored
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT network, wallet FROM free_mint_claims ORDER BY network"
        ).fetchall()
    assert rows == [("mainnet", "rCrossNetwork"), ("testnet", "rCrossNetwork")]


def test_unsupported_networks_are_rejected_without_records(tmp_path):
    db, history = paths(tmp_path)

    result = sm.reserve_if_eligible(
        db, history, network="devnet", wallet="rNew", session_id="s1", now=101
    )

    assert result == sm.ReservationResult(False, "wrong_network", None)
    with pytest.raises(ValueError, match="unsupported sponsored mint network"):
        sm.start_campaign(db, network="devnet", actor="42", now=100)
    with pytest.raises(ValueError, match="unsupported sponsored mint network"):
        sm.campaign_status(db, history, network="devnet", now=100)
    assert not os.path.exists(db)


def test_110_concurrent_reservations_admit_exactly_persisted_cap(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)

    def reserve(index):
        return sm.reserve_if_eligible(
            db,
            history,
            network="mainnet",
            wallet=f"rNew{index}",
            session_id=f"s{index}",
            now=101,
        )

    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(reserve, range(110)))

    assert sum(result.sponsored for result in results) == 100
    assert sum(result.reason == "at_capacity" for result in results) == 10
    status = sm.campaign_status(db, history, network="mainnet", now=102)
    assert status.state == "at_capacity"
    assert status.reserved == 100


def test_duplicate_wallet_requests_create_one_claim(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)

    def reserve(index):
        return sm.reserve_if_eligible(
            db,
            history,
            network="mainnet",
            wallet=" rDuplicate ",
            session_id=f"s{index}",
            now=101,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(20)))

    assert sum(result.sponsored for result in results) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM free_mint_claims").fetchone()[0] == 1


def test_manual_stop_is_immediate_and_idempotent(tmp_path):
    db, history = paths(tmp_path)
    started = sm.start_campaign(db, network="mainnet", actor="42", now=100)
    stopped = sm.stop_campaign(db, network="mainnet", actor="42", now=101)
    stopped_again = sm.stop_campaign(db, network="mainnet", actor="42", now=102)

    assert stopped.campaign_id == started.campaign_id
    assert stopped.state == stopped_again.state == "stopped"
    assert not sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rLate", session_id="s1", now=103
    ).sponsored


def test_released_pre_mint_claim_reopens_capacity_and_rebinds_with_audit(tmp_path):
    db, history = paths(tmp_path)
    first_campaign = sm.start_campaign(db, network="mainnet", actor="42", now=100)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE free_mint_campaigns SET cap = 1 WHERE id = ?",
            (first_campaign.campaign_id,),
        )
    first = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rFirst", session_id="s1", now=101
    )
    assert first.sponsored
    assert (
        sm.reserve_if_eligible(
            db, history, network="mainnet", wallet="rBlocked", session_id="s2", now=101
        ).reason
        == "at_capacity"
    )
    assert sm.release_reservation(
        db,
        network="mainnet",
        wallet="rFirst",
        session_id="s1",
        reason="cancelled",
        now=102,
    )
    assert sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rBlocked", session_id="s2", now=103
    ).sponsored

    sm.stop_campaign(db, network="mainnet", actor="42", now=104)
    second_campaign = sm.start_campaign(db, network="mainnet", actor="42", now=105)
    assert sm.release_reservation(
        db,
        network="mainnet",
        wallet="rBlocked",
        session_id="s2",
        reason="cancelled",
        now=106,
    )
    reacquired = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rFirst", session_id="s3", now=107
    )
    assert reacquired.sponsored
    assert reacquired.claim is not None
    assert reacquired.claim.id == first.claim.id
    assert reacquired.claim.campaign_id == second_campaign.campaign_id
    assert reacquired.claim.session_id == "s3"
    assert reacquired.claim.created_at == first.claim.created_at
    assert second_campaign.campaign_id != first_campaign.campaign_id
    with sqlite3.connect(db) as conn:
        history_rows = conn.execute(
            "SELECT action, details FROM free_mint_audit "
            "WHERE action IN ('claim_released', 'claim_reacquired') "
            "AND campaign_id = ? AND details LIKE ? ORDER BY id",
            (first_campaign.campaign_id, "%rFirst%"),
        ).fetchall()
    assert [row[0] for row in history_rows] == ["claim_released", "claim_reacquired"]
    assert "s1" in history_rows[1][1]


def test_minting_claim_is_not_released_without_explicit_chain_proof(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rMinting", session_id="s1", now=101
    )
    first = prepare_and_forward(
        sm, db, network="mainnet", wallet="rMinting", session_id="s1", now=102
    )
    second = prepare_and_forward(
        sm, db, network="mainnet", wallet="rMinting", session_id="s1", now=103
    )

    assert first is not None and first.started
    assert second is not None and not second.started
    assert first.claim == second.claim
    assert first.claim.status == "minting"
    assert not sm.release_reservation(
        db,
        network="mainnet",
        wallet="rMinting",
        session_id="s1",
        reason="timeout",
        now=104,
    )


def test_confirmed_mint_enqueues_one_burn_and_remains_consumed_after_offer_failure(
    tmp_path,
):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rConsumed", session_id="s1", now=101
    )
    prepare_and_forward(
        sm,
        db,
        network="mainnet",
        wallet="rConsumed",
        session_id="s1",
        tx_hash="ABC",
        now=102,
    )
    first = sm.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet="rConsumed",
        session_id="s1",
        mint_tx_hash="ABC",
        nft_id="NFT1",
        now=103,
    )
    second = sm.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet="rConsumed",
        session_id="s1",
        mint_tx_hash="ABC",
        nft_id="NFT1",
        now=104,
    )
    failed_offer = sm.record_offer(
        db,
        network="mainnet",
        wallet="rConsumed",
        session_id="s1",
        offer_id=None,
        error="offer submission failed",
        now=105,
    )

    assert first == second
    assert failed_offer is not None and failed_offer.status == "minted"
    assert failed_offer.nft_id == "NFT1"
    with sqlite3.connect(db) as conn:
        burn_rows = conn.execute("SELECT count(*) FROM free_mint_burns").fetchone()[0]
    assert burn_rows == 1
    assert (
        sm.reserve_if_eligible(
            db, history, network="mainnet", wallet="rConsumed", session_id="s2", now=106
        ).reason
        == "already_consumed"
    )


def test_offer_and_acceptance_transitions_are_idempotent(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rAccepted", session_id="s1", now=101
    )
    prepare_and_forward(
        sm,
        db,
        network="mainnet",
        wallet="rAccepted",
        session_id="s1",
        tx_hash="ABC",
        now=102,
    )
    sm.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet="rAccepted",
        session_id="s1",
        mint_tx_hash="ABC",
        nft_id="NFT1",
        now=103,
    )
    offered = sm.record_offer(
        db,
        network="mainnet",
        wallet="rAccepted",
        session_id="s1",
        offer_id="OFFER1",
        now=104,
    )
    offered_again = sm.record_offer(
        db,
        network="mainnet",
        wallet="rAccepted",
        session_id="s1",
        offer_id="OFFER1",
        now=105,
    )
    accepted = sm.record_acceptance(db, "mainnet", "rAccepted", "ACCEPT1", 106, offer_id="OFFER1")
    accepted_again = sm.record_acceptance(
        db, "mainnet", "rAccepted", "ACCEPT1", 107, offer_id="OFFER1"
    )

    assert offered == offered_again
    assert offered is not None and offered.status == "offered"
    assert accepted == accepted_again
    assert accepted is not None and accepted.status == "accepted"
    assert accepted.accept_tx_hash == "ACCEPT1"
    assert accepted.tagged_at == 106


def test_existing_session_retry_remains_sponsored_after_campaign_stop(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    admitted = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryStop", session_id="s1", now=101
    )
    sm.stop_campaign(db, network="mainnet", actor="42", now=102)

    retried = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryStop", session_id="s1", now=103
    )

    assert retried == admitted


def test_existing_session_retry_remains_sponsored_after_campaign_expiry(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    admitted = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryExpiry", session_id="s1", now=101
    )

    retried = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryExpiry", session_id="s1", now=3700
    )

    assert retried == admitted


def test_existing_session_retry_remains_sponsored_when_archive_is_unavailable(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    admitted = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryArchive", session_id="s1", now=101
    )
    os.unlink(history)

    retried = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryArchive", session_id="s1", now=102
    )

    assert retried == admitted
    assert not os.path.exists(history)


def test_existing_session_retry_remains_sponsored_after_wallet_becomes_tagged(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    admitted = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryTagged", session_id="s1", now=101
    )
    insert_tagged(history, "rRetryTagged")

    retried = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryTagged", session_id="s1", now=102
    )

    assert retried == admitted


def test_existing_session_retry_remains_sponsored_after_wallet_becomes_excluded(
    tmp_path, monkeypatch
):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    admitted = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryExcluded", session_id="s1", now=101
    )
    monkeypatch.setattr(config, "SPONSORED_MINT_EXCLUDED_WALLETS", ("rRetryExcluded",))

    retried = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rRetryExcluded", session_id="s1", now=102
    )

    assert retried == admitted


def test_burn_obligation_snapshots_amount_and_source_account(tmp_path, monkeypatch):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)
    sm.reserve_if_eligible(db, history, network="mainnet", wallet="rDebt", session_id="s1", now=101)
    prepare_and_forward(
        sm,
        db,
        network="mainnet",
        wallet="rDebt",
        session_id="s1",
        tx_hash="ABC",
        now=102,
    )
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "7.25")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rSourceAtConfirmation")

    sm.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet="rDebt",
        session_id="s1",
        mint_tx_hash="ABC",
        nft_id="NFT1",
        now=103,
    )
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "99")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rDifferentLaterSource")

    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(free_mint_burns)")}
        assert {"amount", "source_account"} <= columns
        persisted = conn.execute("SELECT amount, source_account FROM free_mint_burns").fetchone()
    assert persisted == ("7.25", "rSourceAtConfirmation")
    assert sm.BurnObligation.__dataclass_fields__["amount"].type == "str"
    assert sm.BurnObligation.__dataclass_fields__["source_account"].type == "str"


def test_campaign_limits_ignore_environment_overrides():
    env = os.environ.copy()
    env["SPONSORED_MINT_DURATION_SECONDS"] = "17"
    env["SPONSORED_MINT_CAP"] = "23"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from lfg_core import config; "
                "print(config.SPONSORED_MINT_DURATION_SECONDS, config.SPONSORED_MINT_CAP)"
            ),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "3600 100"


def test_started_campaign_always_persists_hard_limits(tmp_path, monkeypatch):
    db, _ = paths(tmp_path)
    monkeypatch.setattr(config, "SPONSORED_MINT_DURATION_SECONDS", 17)
    monkeypatch.setattr(config, "SPONSORED_MINT_CAP", 23)

    started = sm.start_campaign(db, network="mainnet", actor="42", now=100)

    assert started.enabled_until == 3700
    assert started.cap == 100


def test_empty_normalized_identifiers_do_not_consume_capacity(tmp_path):
    db, history = paths(tmp_path)
    sm.start_campaign(db, network="mainnet", actor="42", now=100)

    blank_wallet = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="   ", session_id="s1", now=101
    )
    blank_session = sm.reserve_if_eligible(
        db, history, network="mainnet", wallet="rValid", session_id="   ", now=101
    )

    assert blank_wallet == sm.ReservationResult(False, "invalid_request", None)
    assert blank_session == sm.ReservationResult(False, "invalid_request", None)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM free_mint_claims").fetchone()[0] == 0


def test_audit_archive_reverify_writes_row(tmp_path):
    db = str(tmp_path / "app.db")
    sm.audit_archive_reverify(
        db, network="testnet", actor="admin:42", result="ok", now=1_800_000_000
    )
    sm.audit_archive_reverify(
        db, network="testnet", actor="admin:42", result="failed: genesis_mismatch"
    )
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT actor, action, result FROM free_mint_audit ORDER BY at"
        ).fetchall()
    assert ("admin:42", "archive_reverify", "ok") in rows
    assert ("admin:42", "archive_reverify", "failed: genesis_mismatch") in rows
