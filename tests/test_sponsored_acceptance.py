"""Validated SourceTag acceptance and archive-backed campaign metrics."""

import asyncio
import os
import sqlite3
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
os.environ.setdefault("XRPL_NETWORK", "mainnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import config, history_store
from lfg_core import sponsored_mint as sm
from tests.sponsored_helpers import ready_history


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _paths(tmp_path):
    app = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    ready_history(history, network="mainnet")
    return app, history


def _claim(app, wallet, *, network="mainnet", status="offered", offer_id="SELL-OFFER"):
    campaign = sm.start_campaign(app, network=network, actor="admin", now=100)
    with sqlite3.connect(app) as conn:
        conn.execute(
            """
            INSERT INTO free_mint_claims (
                id, network, wallet, campaign_id, session_id, status,
                reserved_at, reservation_expires_at, offer_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 100, NULL, ?, 100, 100)
            """,
            (
                f"claim-{wallet}",
                network,
                wallet,
                campaign.campaign_id,
                f"session-{wallet}",
                status,
                offer_id,
            ),
        )


def _accept(
    wallet="rSponsored",
    *,
    tx_hash="A" * 64,
    offer_id="SELL-OFFER",
    tag=config.SOURCE_TAG,
    result="tesSUCCESS",
):
    return {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": wallet,
        "SourceTag": tag,
        "hash": tx_hash,
        "date": 800_000_000,
        "validated": True,
        **({"NFTokenSellOffer": offer_id} if offer_id is not None else {}),
    }, {"TransactionResult": result}


def test_observer_records_only_a_valid_mainnet_sponsored_acceptance(tmp_path, monkeypatch):
    app, _ = _paths(tmp_path)
    _claim(app, "rSponsored")
    monkeypatch.setattr(
        sm, "db_path", SimpleNamespace(app_db_path=lambda network: app), raising=False
    )
    tx, meta = _accept()

    assert sm.observe_sponsored_acceptance(tx, meta, network="mainnet") is True
    with sqlite3.connect(app) as conn:
        row = conn.execute(
            "SELECT status, accept_tx_hash, tagged_at FROM free_mint_claims WHERE wallet=?",
            ("rSponsored",),
        ).fetchone()
    assert row == ("accepted", tx["hash"], 1_746_684_800)


def test_observer_ignores_invalid_or_unrelated_acceptances(tmp_path, monkeypatch):
    app, _ = _paths(tmp_path)
    _claim(app, "rSponsored")
    _claim(app, config.SIGNING_ACCOUNT)
    monkeypatch.setattr(
        sm, "db_path", SimpleNamespace(app_db_path=lambda network: app), raising=False
    )

    cases = [
        _accept(tag=config.SOURCE_TAG + 1),
        _accept(result="tecNO_ENTRY"),
        _accept(wallet=config.SIGNING_ACCOUNT),
        _accept(wallet="rUnrelated"),
    ]
    unvalidated_tx, unvalidated_meta = _accept()
    unvalidated_tx["validated"] = False
    cases.append((unvalidated_tx, unvalidated_meta))
    for tx, meta in cases:
        assert sm.observe_sponsored_acceptance(tx, meta, network="mainnet") is False

    tx, meta = _accept(tx_hash="B" * 64)
    assert sm.observe_sponsored_acceptance(tx, meta, network="devnet") is False
    with sqlite3.connect(app) as conn:
        rows = conn.execute(
            "SELECT wallet, status, accept_tx_hash FROM free_mint_claims ORDER BY wallet"
        ).fetchall()
    assert rows == [(config.SIGNING_ACCOUNT, "offered", None), ("rSponsored", "offered", None)]


def test_observer_records_valid_testnet_acceptance_in_testnet_app_db(tmp_path, monkeypatch):
    main_app = str(tmp_path / "main-app.db")
    test_app = str(tmp_path / "test-app.db")
    _claim(main_app, "rSponsored", network="mainnet")
    _claim(test_app, "rSponsored", network="testnet")
    paths = {"mainnet": main_app, "testnet": test_app}
    monkeypatch.setattr(
        sm,
        "db_path",
        SimpleNamespace(app_db_path=lambda network: paths[network]),
        raising=False,
    )
    tx, meta = _accept()

    assert sm.observe_sponsored_acceptance(tx, meta, network="testnet") is True
    with sqlite3.connect(main_app) as conn:
        main_status = conn.execute(
            "SELECT status FROM free_mint_claims WHERE wallet = 'rSponsored'"
        ).fetchone()[0]
    with sqlite3.connect(test_app) as conn:
        test_status = conn.execute(
            "SELECT status FROM free_mint_claims WHERE wallet = 'rSponsored'"
        ).fetchone()[0]
    assert main_status == "offered"
    assert test_status == "accepted"


def test_observer_requires_the_claims_exact_locked_sell_offer(tmp_path, monkeypatch):
    app, _ = _paths(tmp_path)
    _claim(app, "rExact", offer_id="OFFER-EXACT")
    _claim(app, "rWrong", offer_id="OFFER-WRONG")
    _claim(app, "rMissing", offer_id="OFFER-MISSING")
    _claim(app, "rNull", offer_id=None)
    monkeypatch.setattr(sm, "db_path", SimpleNamespace(app_db_path=lambda network: app))

    cases = [
        ("rWrong", "OTHER-OFFER", False),
        ("rMissing", None, False),
        ("rNull", "OFFER-NULL", False),
        ("rUnrelated", "OFFER-EXACT", False),
        ("rExact", "OFFER-EXACT", True),
    ]
    for wallet, offer_id, expected in cases:
        tx, meta = _accept(wallet=wallet, tx_hash=f"{wallet:0<64}", offer_id=offer_id)
        assert sm.observe_sponsored_acceptance(tx, meta, network="mainnet") is expected

    with sqlite3.connect(app) as conn:
        accepted = conn.execute(
            "SELECT wallet FROM free_mint_claims WHERE status='accepted'"
        ).fetchall()
    assert accepted == [("rExact",)]


@pytest.mark.parametrize(
    ("page_validated", "entry_validated", "stored"),
    [
        (True, None, True),
        (None, True, True),
        (True, True, True),
        (None, None, False),
        ("yes", None, False),
        (None, "yes", False),
        (False, True, False),
        (True, False, False),
    ],
)
@pytest.mark.parametrize("source", ("account", "nft"))
def test_backfill_requires_explicit_non_conflicting_validation(
    tmp_path, page_validated, entry_validated, stored, source
):
    from scripts import backfill_history as backfill

    conn = history_store.init_history_db(str(tmp_path / f"{source}.db"))
    tx, meta = _accept(wallet="rNoClaim")
    entry = {"tx": tx, "meta": meta}
    if entry_validated is not None:
        entry["validated"] = entry_validated
    response = {"transactions": [entry]}
    if page_validated is not None:
        response["validated"] = page_validated

    async def request_fn(_request):
        return response

    if source == "account":
        count = _run(
            backfill.backfill_account_tx(conn, request_fn, "rIssuer", "source", network="mainnet")
        )
    else:
        count = _run(backfill.backfill_nft_history(conn, request_fn, "NFT", network="mainnet"))
    assert count == int(stored)
    assert conn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == int(stored)


def test_status_counts_distinct_non_project_tagged_archive_accounts(tmp_path, monkeypatch):
    app, history = _paths(tmp_path)
    _claim(app, "rAccepted")
    monkeypatch.setattr(
        sm, "db_path", SimpleNamespace(app_db_path=lambda network: app), raising=False
    )
    tx, meta = _accept(wallet="rAccepted")
    assert sm.observe_sponsored_acceptance(tx, meta, network="mainnet") is True

    with sqlite3.connect(history) as conn:
        for tx_hash, account in (
            ("one", "rOne"),
            ("two", "rOne"),
            ("three", "rTwo"),
            ("mint", config.SIGNING_ACCOUNT),
            ("burn", config.TOKEN_ISSUER_ADDRESS),
        ):
            conn.execute(
                "INSERT INTO xrpl_txs VALUES (?, 1, 1, ?, ?, ?, ?)",
                (tx_hash, "Payment", account, config.SOURCE_TAG, "{}"),
            )

    status = sm.campaign_status(app, history, network="mainnet", now=101)
    assert status.accepted == status.tagged_sponsored_wallets == 1
    assert status.unique_tagged_wallets == 2


def test_status_does_not_count_an_unrecorded_accepted_claim(tmp_path):
    app, history = _paths(tmp_path)
    _claim(app, "rUnverified", status="accepted")

    status = sm.campaign_status(app, history, network="mainnet", now=101)
    assert status.accepted == status.tagged_sponsored_wallets == 0


def test_backfill_replay_observes_a_new_archive_row_once(tmp_path, monkeypatch):
    from scripts import backfill_history as backfill

    app, history = _paths(tmp_path)
    _claim(app, "rReplay")
    monkeypatch.setattr(sm, "db_path", SimpleNamespace(app_db_path=lambda network: app))
    conn = history_store.init_history_db(history)
    tx, meta = _accept(wallet="rReplay")
    tx["meta"] = meta

    assert backfill.store_raw_tx(conn, tx, network="mainnet") is True
    assert backfill.store_raw_tx(conn, tx, network="mainnet") is False
    with sqlite3.connect(app) as app_conn:
        accepted = app_conn.execute(
            "SELECT status, accept_tx_hash FROM free_mint_claims WHERE wallet=?", ("rReplay",)
        ).fetchone()
        audits = app_conn.execute(
            "SELECT count(*) FROM free_mint_audit WHERE action='claim_accepted'"
        ).fetchone()[0]
    assert accepted == ("accepted", tx["hash"])
    assert audits == 1


def test_backfill_refuses_an_explicitly_unvalidated_page(tmp_path, monkeypatch):
    from scripts import backfill_history as backfill

    app, history = _paths(tmp_path)
    _claim(app, "rUnvalidatedPage")
    monkeypatch.setattr(sm, "db_path", SimpleNamespace(app_db_path=lambda network: app))
    conn = history_store.init_history_db(history)
    tx, meta = _accept(wallet="rUnvalidatedPage")
    tx["meta"] = meta

    async def request_fn(_request):
        return {"validated": False, "transactions": [{"tx": tx, "meta": meta}]}

    assert (
        _run(backfill.backfill_account_tx(conn, request_fn, "rIssuer", "source", network="mainnet"))
        == 0
    )
    assert conn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 0
    with sqlite3.connect(app) as app_conn:
        assert app_conn.execute(
            "SELECT status FROM free_mint_claims WHERE wallet=?", ("rUnvalidatedPage",)
        ).fetchone() == ("offered",)


def test_backfill_keeps_raw_history_and_retries_acceptance_projection_on_duplicate(
    tmp_path, monkeypatch
):
    from scripts import backfill_history as backfill

    _app, history = _paths(tmp_path)
    conn = history_store.init_history_db(history)
    tx, meta = _accept()
    tx["meta"] = meta
    calls = {"count": 0}

    def fail_once(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("busy")
        return True

    monkeypatch.setattr(sm, "observe_sponsored_acceptance", fail_once)
    with pytest.raises(sqlite3.OperationalError, match="busy"):
        backfill.store_raw_tx(conn, tx, network="mainnet")
    assert conn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1

    assert backfill.store_raw_tx(conn, tx, network="mainnet") is False
    assert conn.execute("SELECT count(*) FROM xrpl_txs").fetchone()[0] == 1
    assert calls["count"] == 2
