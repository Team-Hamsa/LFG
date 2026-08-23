# Sponsored single-mint admission and irreversible pipeline boundaries.
import logging
import os
import sqlite3
from types import SimpleNamespace

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import importlib  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402

from lfg_core import (  # noqa: E402
    bulk_mint_flow,
    config,
    headroom,
    history_store,
    memos,
    mint_flow,
    sponsored_burn,
    sponsored_mint,
    supply,
)
from lfg_service import app as server  # noqa: E402
from tests import sponsored_helpers  # noqa: E402
from tests.sponsored_helpers import prepare_and_forward, ready_history  # noqa: E402

# Well-formed placeholder classic addresses (not the operator's real wallets):
# the readiness audit validates the *shape* of SPONSORED_MINT_EXCLUDED_WALLETS,
# not specific identities (#334).
_PLACEHOLDER_EXCLUSIONS = (
    "raJ1Aqkhf19P7cyUc33MMVAzgvHPvtNFC",
    "rBcktgVfNjHmxNAQDEE66ztz4qZkdngdm",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _PostRequest:
    """Small mutable request double without aiohttp's app-key warnings."""

    headers: dict[str, str] = {}

    def __init__(self, body=None):
        self._body = body or {}
        self._store = {}

    async def json(self):
        return self._body

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value


def _post_request(path="/api/mint", body=None):
    del path
    return _PostRequest(body)


async def _allocate_4000():
    return 4000


@pytest.fixture(autouse=True)
def _clean_destination_preflight(monkeypatch):
    """#388/#408: handle_mint_start now pre-flights the destination wallet with
    an account_info before admitting a mint. With no ledger to ask, the real
    helper reports UNRESOLVED — which correctly declines sponsorship, and would
    quietly turn every test here into a paid-path test (and issue a real
    mainnet RPC call besides). Pin a healthy wallet; the pre-flight's own
    behaviour is covered in tests/test_sponsored_preflight.py."""
    sponsored_helpers.stub_clean_preflight(monkeypatch, server)


@pytest.fixture(autouse=True)
def _service_env(tmp_path, monkeypatch):
    app_db = tmp_path / "app.db"
    history_dbs = {
        "mainnet": tmp_path / "history-mainnet.db",
        "testnet": tmp_path / "history-testnet.db",
    }
    for network, path in history_dbs.items():
        ready_history(str(path), network=network)

    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server.config, "XRPL_NETWORK", "mainnet")
    monkeypatch.setattr(server, "mint_sessions", {})
    monkeypatch.setattr(server, "bulk_sessions", {})
    monkeypatch.setattr(server, "_sponsored_recovery_ready", True, raising=False)
    monkeypatch.setattr(server.db_path, "app_db_path", lambda network=None: str(app_db))
    monkeypatch.setattr(
        server.history_store, "history_db_path", lambda network: str(history_dbs[network])
    )
    monkeypatch.setattr(supply, "current_supply", lambda network: 0)
    monkeypatch.setattr(
        headroom.nft_index, "index_db_path", lambda network: str(tmp_path / "idx.db")
    )
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(bulk_mint_flow.config, "DB_PATH", str(tmp_path / "payments.db"))

    async def _no_push(_user):
        return None

    monkeypatch.setattr(server, "_push_token", _no_push)
    return SimpleNamespace(
        app_db=str(app_db),
        history_db=str(history_dbs["mainnet"]),
        history_dbs={key: str(value) for key, value in history_dbs.items()},
    )


def _admitted():
    return sponsored_mint.ReservationResult(
        True, "reserved", SimpleNamespace(id="claim-test", status="reserved")
    )


def _not_admitted(reason):
    return sponsored_mint.ReservationResult(False, reason, None)


def _paid_prepare():
    async def prepare(self):
        self.pay_with = "XRP"
        self.pay_amount = "10"
        self.payment_link = "https://xumm.app/sign/paid"
        self.payment_uuid = "paid"

    return prepare


def _sponsored_session():
    return mint_flow.MintSession("dev", "rNEW", platform="discord", sponsored=True)


def _reserve_recovery_claim(paths, wallet, session_id, *, network="mainnet", now=101):
    result = sponsored_mint.reserve_if_eligible(
        paths.app_db,
        paths.history_dbs[network],
        network=network,
        wallet=wallet,
        session_id=session_id,
        now=now,
    )
    assert result.sponsored
    assert result.claim is not None
    return result.claim


def test_mint_start_does_not_promise_free_before_reservation_commits(monkeypatch):
    observed = {}
    launched = []

    def reserve(*args, **kwargs):
        (session,) = server.mint_sessions.values()
        observed["before_commit"] = session.to_dict()
        return _admitted()

    async def forbidden_prepare(_self):
        raise AssertionError("paid preparation must not run after admission")

    async def fake_wrapper(session):
        launched.append(session.id)

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", reserve)
    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", forbidden_prepare)
    monkeypatch.setattr(server, "_run_mint_session_and_publish", fake_wrapper)

    async def scenario():
        response = await server.handle_mint_start(_post_request())
        session = next(iter(server.mint_sessions.values()))
        await session.task
        return response, session

    response, session = _run(scenario())
    body = json.loads(response.body)

    assert observed["before_commit"]["sponsored"] is False
    assert body["sponsored"] is True
    assert body["pay_with"] == "SPONSORED"
    assert body["pay_amount"] == "0"
    assert body["payment_link"] is None
    assert session.payment_uuid is None
    assert launched == [session.id]


def test_mint_start_does_not_rebind_a_live_sessions_reservation(_service_env, monkeypatch):
    sponsored_mint.start_campaign(
        _service_env.app_db,
        network="mainnet",
        actor="test",
        now=100,
    )
    old_session = mint_flow.MintSession(
        "other-user",
        server.mock_economy.DEV_OWNER,
        platform="discord",
    )
    server.mint_sessions[old_session.id] = old_session
    reserved = sponsored_mint.reserve_if_eligible(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
        wallet=old_session.wallet_address,
        session_id=old_session.id,
        now=101,
    )
    assert reserved.sponsored

    rebind_calls = []

    def forbidden_rebind(*args, **kwargs):
        rebind_calls.append((args, kwargs))
        raise AssertionError("a live session still owns this reservation")

    async def prepare_paid(self):
        self.pay_with = "XRP"
        self.pay_amount = "10"
        self.payment_link = "https://xumm.app/sign/paid"
        self.payment_uuid = "paid"

    async def finish(_session):
        return None

    monkeypatch.setattr(server.sponsored_mint, "rebind_reservation", forbidden_rebind)
    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", prepare_paid)
    monkeypatch.setattr(server, "_run_mint_session_and_publish", finish)

    async def scenario():
        response = await server.handle_mint_start(_post_request())
        new_session = next(
            session for session in server.mint_sessions.values() if session.id != old_session.id
        )
        await new_session.task
        return response, new_session

    response, new_session = _run(scenario())
    body = json.loads(response.body)
    with sqlite3.connect(_service_env.app_db) as conn:
        owner = conn.execute(
            "SELECT session_id FROM free_mint_claims WHERE wallet = ?",
            (old_session.wallet_address,),
        ).fetchone()[0]

    assert rebind_calls == []
    assert owner == old_session.id
    assert old_session.state == mint_flow.AWAITING_PAYMENT
    assert new_session.sponsored is False
    assert body["pay_with"] == "XRP"


def test_cancel_during_headroom_reservation_cleans_inserted_session_and_grant(
    _service_env, monkeypatch
):
    """A grant committed by the worker after cancellation cannot leak."""
    entered = threading.Event()
    proceed = threading.Event()
    committed = threading.Event()
    real_reserve = headroom.try_reserve

    def delayed_headroom(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        try:
            return real_reserve(*args, **kwargs)
        finally:
            committed.set()

    def forbidden_sponsorship(*args, **kwargs):
        raise AssertionError("cancelled headroom admission reached sponsorship")

    monkeypatch.setattr(server.headroom, "try_reserve", delayed_headroom)
    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", forbidden_sponsorship)

    async def scenario():
        task = asyncio.create_task(server.handle_mint_start(_post_request()))
        assert await asyncio.to_thread(entered.wait, 5)
        session = next(iter(server.mint_sessions.values()))
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(committed.wait, 5)
        return session

    session = _run(scenario())
    sponsored_mint.ensure_schema(_service_env.app_db)
    with sqlite3.connect(_service_env.app_db) as conn:
        reserved_claims = conn.execute(
            "SELECT COUNT(*) FROM free_mint_claims WHERE status = 'reserved'"
        ).fetchone()[0]
    assert session.state == mint_flow.CANCELLED
    assert session.id not in server.mint_sessions
    assert (
        server._active_session(
            server.mint_sessions,
            mint_flow.TERMINAL_STATES,
            session.discord_id,
            session.platform,
        )
        is None
    )
    assert headroom.reserved_for(_service_env.app_db, f"mint:{session.id}") == 0
    assert reserved_claims == 0


def test_cancel_during_sponsored_reservation_cleans_session_claim_and_headroom(
    _service_env, monkeypatch
):
    """Cancellation cannot orphan a claim whose worker commits afterward."""
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    entered = threading.Event()
    proceed = threading.Event()
    committed = threading.Event()
    real_reserve = sponsored_mint.reserve_if_eligible

    def delayed_reserve(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        try:
            kwargs["now"] = 101
            return real_reserve(*args, **kwargs)
        finally:
            committed.set()

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", delayed_reserve)

    async def scenario():
        task = asyncio.create_task(server.handle_mint_start(_post_request()))
        assert await asyncio.to_thread(entered.wait, 5)
        session = next(iter(server.mint_sessions.values()))
        task.cancel()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(committed.wait, 5)
        return session

    session = _run(scenario())
    with sqlite3.connect(_service_env.app_db) as conn:
        status = conn.execute(
            "SELECT status FROM free_mint_claims WHERE session_id = ?",
            (session.id,),
        ).fetchone()[0]
    assert status == "released"
    assert session.state == mint_flow.CANCELLED
    assert session.id not in server.mint_sessions
    assert (
        server._active_session(
            server.mint_sessions,
            mint_flow.TERMINAL_STATES,
            session.discord_id,
            session.platform,
        )
        is None
    )
    assert headroom.outstanding(_service_env.app_db) == 0


def test_startup_rebuild_counts_uncertain_and_confirmed_sponsored_claims(_service_env, monkeypatch):
    """Crash recovery must retain irreversible sponsored work while pruning stale rows."""
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    uncertain = mint_flow.MintSession("u1", "rUNCERTAIN", sponsored=True)
    confirmed = mint_flow.MintSession("u2", "rCONFIRMED", sponsored=True)
    for session in (uncertain, confirmed):
        result = sponsored_mint.reserve_if_eligible(
            _service_env.app_db,
            _service_env.history_db,
            network="mainnet",
            wallet=session.wallet_address,
            session_id=session.id,
            now=101,
        )
        assert result.sponsored
        assert headroom.try_reserve(_service_env.app_db, f"mint:{session.id}", 1, "mainnet") == 1

    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=uncertain.wallet_address,
        session_id=uncertain.id,
        now=102,
    )
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=confirmed.wallet_address,
        session_id=confirmed.id,
        tx_hash="MINTTX",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=confirmed.wallet_address,
        session_id=confirmed.id,
        mint_tx_hash="MINTTX",
        nft_id="NFT-CONFIRMED",
        now=103,
    )
    assert headroom.try_reserve(_service_env.app_db, "mint:stale", 1, "mainnet") == 1

    monkeypatch.setattr(bulk_mint_flow, "load_all_resumable", lambda: [])
    monkeypatch.setattr(
        server, "_recover_sponsored_mint_submissions", lambda *a, **k: asyncio.sleep(0)
    )
    monkeypatch.setattr(
        server, "_recover_sponsored_offers", lambda *args, **kwargs: asyncio.sleep(0)
    )
    _run(server.resume_bulk_jobs())

    assert headroom.reserved_for(_service_env.app_db, "mint:stale") == 0
    assert headroom.reserved_for(_service_env.app_db, f"mint:{uncertain.id}") == 1
    assert headroom.reserved_for(_service_env.app_db, f"mint:{confirmed.id}") == 0
    assert headroom.outstanding(_service_env.app_db) == 2


def test_startup_snapshot_failure_skips_rebuild_and_preserves_existing_rows(
    _service_env, monkeypatch
):
    claimant = "mint:must-survive"
    assert headroom.try_reserve(_service_env.app_db, claimant, 1, "mainnet") == 1
    rebuild_calls = []

    def snapshot_failure(*args, **kwargs):
        raise RuntimeError("sponsored snapshot unavailable")

    def forbidden_rebuild(*args, **kwargs):
        rebuild_calls.append((args, kwargs))

    monkeypatch.setattr(bulk_mint_flow, "load_all_resumable", lambda: [])
    monkeypatch.setattr(server.sponsored_mint, "headroom_snapshots", snapshot_failure)
    monkeypatch.setattr(server.headroom, "rebuild", forbidden_rebuild)

    _run(server._start_bulk_resume({}))

    assert rebuild_calls == []
    assert headroom.reserved_for(_service_env.app_db, claimant) == 1


def test_failed_startup_recovery_disables_sponsorship_but_keeps_paid_mint_available(
    _service_env, monkeypatch
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)

    async def failed_resume():
        raise RuntimeError("recovery unavailable")

    async def prepare(self):
        await _paid_prepare()(self)

    async def finish(session):
        return None

    monkeypatch.setattr(server, "resume_bulk_jobs", failed_resume)
    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", prepare)
    monkeypatch.setattr(server, "_run_mint_session_and_publish", finish)

    _run(server._start_bulk_resume({}))

    async def scenario():
        response = await server.handle_mint_start(_post_request())
        session = next(iter(server.mint_sessions.values()))
        await session.task
        return response, session

    response, session = _run(scenario())
    with sqlite3.connect(_service_env.app_db) as conn:
        claim_count = conn.execute("SELECT COUNT(*) FROM free_mint_claims").fetchone()[0]

    assert server._sponsored_recovery_ready is False
    assert response.status == 200
    assert session.sponsored is False
    assert session.pay_with == "XRP"
    assert claim_count == 0


def test_successful_startup_recovery_enables_sponsorship_gate(monkeypatch):
    events = []

    async def successful_resume():
        events.append("recovered")

    monkeypatch.setattr(server, "_sponsored_recovery_ready", False, raising=False)
    monkeypatch.setattr(server, "resume_bulk_jobs", successful_resume)

    _run(server._start_bulk_resume({}))

    assert events == ["recovered"]
    assert server._sponsored_recovery_ready is True


def test_offer_recovery_failure_still_reattaches_resumable_paid_jobs(_service_env, monkeypatch):
    job = SimpleNamespace(id="paid-job", network="mainnet", task=None)
    events = []

    def recovery(*args, **kwargs):
        return SimpleNamespace(held_minting=(), missing_offers=(), debt_count=0)

    async def failed_offers(*args, **kwargs):
        raise RuntimeError("aggregate offer recovery failure")

    async def run_job(resumed):
        events.append(resumed.id)

    monkeypatch.setattr(bulk_mint_flow, "load_all_resumable", lambda: [job])
    monkeypatch.setattr(bulk_mint_flow, "headroom_snapshot", lambda resumed: None)
    monkeypatch.setattr(bulk_mint_flow, "run_bulk_mint_job", run_job)
    monkeypatch.setattr(server.sponsored_mint, "recover_incomplete_claims", recovery)
    monkeypatch.setattr(server.sponsored_mint, "headroom_snapshots", lambda *args, **kwargs: [])
    monkeypatch.setattr(server.headroom, "rebuild", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_recover_sponsored_offers", failed_offers)

    async def scenario():
        with pytest.raises(RuntimeError, match="aggregate offer recovery failure"):
            await server.resume_bulk_jobs()
        await asyncio.sleep(0)

    _run(scenario())

    assert server.bulk_sessions[job.id] is job
    assert events == [job.id]


def test_paid_rebuild_and_attachment_precede_network_sponsored_recovery(_service_env, monkeypatch):
    job = SimpleNamespace(id="paid-before-sponsored", network="mainnet", task=None)
    events = []

    def paid_snapshot(resumed):
        assert resumed is job
        events.append("paid_snapshot")
        return None

    def sponsored_snapshots(*args, **kwargs):
        assert events == ["paid_snapshot"]
        events.append("sponsored_snapshot")
        return []

    def rebuild(*args, **kwargs):
        assert events == ["paid_snapshot", "sponsored_snapshot"]
        events.append("rebuild")

    async def run_job(resumed):
        assert resumed is job
        events.append("paid_run")

    async def mint_recovery(*args, **kwargs):
        assert server.bulk_sessions[job.id] is job
        assert job.task is not None
        await asyncio.sleep(0)
        assert "paid_run" in events
        events.append("mint_recovery")

    def claim_recovery(*args, **kwargs):
        events.append("claim_recovery")
        return SimpleNamespace(missing_offers=(), held_minting=(), debt_count=0)

    async def projection_recovery(*args, **kwargs):
        events.append("projection_recovery")

    async def offer_recovery(*args, **kwargs):
        events.append("offer_recovery")

    monkeypatch.setattr(bulk_mint_flow, "load_all_resumable", lambda: [job])
    monkeypatch.setattr(bulk_mint_flow, "headroom_snapshot", paid_snapshot)
    monkeypatch.setattr(bulk_mint_flow, "run_bulk_mint_job", run_job)
    monkeypatch.setattr(server.sponsored_mint, "headroom_snapshots", sponsored_snapshots)
    monkeypatch.setattr(server.headroom, "rebuild", rebuild)
    monkeypatch.setattr(server, "_recover_sponsored_mint_submissions", mint_recovery)
    monkeypatch.setattr(server.sponsored_mint, "recover_incomplete_claims", claim_recovery)
    monkeypatch.setattr(server, "_recover_sponsored_nft_records", projection_recovery)
    monkeypatch.setattr(server, "_recover_sponsored_offers", offer_recovery)

    _run(server.resume_bulk_jobs())

    assert events == [
        "paid_snapshot",
        "sponsored_snapshot",
        "rebuild",
        "paid_run",
        "mint_recovery",
        "claim_recovery",
        "projection_recovery",
        "offer_recovery",
    ]


def test_startup_offer_recovery_creates_and_persists_one_locked_offer(_service_env, monkeypatch):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rRECIPIENT", "offer-repair")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-OFFER-REPAIR",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-OFFER-REPAIR",
        nft_id="NFT-OFFER-REPAIR",
        now=103,
    )
    calls = []

    async def no_offers(nft_id, *, raise_on_error):
        calls.append(("lookup", nft_id, raise_on_error))
        return []

    async def create_offer(nft_id, wallet, *, platform):
        calls.append(("create", nft_id, wallet, platform))
        return "OFFER-RECOVERED"

    monkeypatch.setattr(server.xrpl_ops, "get_nft_sell_offers", no_offers)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)

    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id FROM free_mint_claims WHERE id = ?", (claim.id,)
        ).fetchone()
    assert calls == [
        ("lookup", "NFT-OFFER-REPAIR", True),
        ("create", "NFT-OFFER-REPAIR", "rRECIPIENT", memos.PLATFORM_BACKEND),
    ]
    assert row == ("offered", "OFFER-RECOVERED")


def test_startup_offer_recovery_builds_valid_memos_with_the_real_offer_builder(
    _service_env, monkeypatch
):
    """Regression: every other offer-recovery test monkeypatches create_nft_offer
    away, so nothing validated the `platform` value the recovery path actually
    passes. `"discord"` is a service *surface* name, not a member of the closed
    memo platform enum, so build_memo_models raised ValueError, create_nft_offer
    swallowed it into None, recovery raised, and _sponsored_recovery_ready was
    pinned False on every subsequent boot. Run the REAL create_nft_offer and stub
    only the XRPL submit beneath it, so the memo builder genuinely executes."""
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rRECIPIENT", "offer-memos")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-OFFER-MEMOS",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-OFFER-MEMOS",
        nft_id="NFT-OFFER-MEMOS",
        now=103,
    )

    async def no_offers(nft_id, *, raise_on_error):
        return []

    captured: dict = {}

    async def fake_confirm(tx, wallet, client, label, **kwargs):
        captured["tx"] = tx
        return {"hash": "HASH", "meta": {"TransactionResult": "tesSUCCESS", "offer_id": "OFFERID"}}

    monkeypatch.setattr(server.xrpl_ops, "get_nft_sell_offers", no_offers)
    monkeypatch.setattr(server.xrpl_ops, "_submit_and_confirm", fake_confirm)

    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    # The offer was actually built — a ValueError out of the memo builder would
    # have left captured empty and returned None.
    tx = captured["tx"]
    decoded = {
        bytes.fromhex(m.memo_type).decode(): bytes.fromhex(m.memo_data).decode() for m in tx.memos
    }
    assert decoded["platform"] in memos._PLATFORMS
    assert decoded["platform"] == memos.PLATFORM_BACKEND
    assert decoded["initiator"] == memos.INITIATOR_BACKEND
    assert tx.source_tag == config.SOURCE_TAG

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id FROM free_mint_claims WHERE id = ?", (claim.id,)
        ).fetchone()
    assert row == ("offered", "OFFERID")


def test_startup_offer_recovery_reuses_live_locked_offer_without_duplicate_submit(
    _service_env, monkeypatch
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rRECIPIENT", "offer-reconcile")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-OFFER-RECONCILE",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-OFFER-RECONCILE",
        nft_id="NFT-OFFER-RECONCILE",
        now=103,
    )

    async def existing_offer(nft_id, *, raise_on_error):
        assert nft_id == "NFT-OFFER-RECONCILE"
        assert raise_on_error is True
        return [
            {
                "offer_index": "OFFER-LIVE",
                "amount": "0",
                "destination": "rRECIPIENT",
                "flags": server.xrpl_ops.LSF_SELL_NFTOKEN,
                "owner": "rBOT",
                "expiration": None,
            }
        ]

    async def forbidden_create(*args, **kwargs):
        raise AssertionError("live locked offer must be reconciled, not duplicated")

    monkeypatch.setattr(server.xrpl_ops, "get_nft_sell_offers", existing_offer)
    monkeypatch.setattr(server.xrpl_ops, "bot_wallet_address", lambda: "rBOT")
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", forbidden_create)

    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id FROM free_mint_claims WHERE id = ?", (claim.id,)
        ).fetchone()
    assert row == ("offered", "OFFER-LIVE")


def test_startup_offer_recovery_replays_archived_acceptance_for_offered_claim(
    _service_env, monkeypatch
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rACCEPTED", "offer-accepted")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-OFFER-ACCEPTED",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-OFFER-ACCEPTED",
        nft_id="NFT-OFFER-ACCEPTED",
        now=103,
    )
    sponsored_mint.record_offer(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        offer_id="OFFER-ACCEPTED",
        now=104,
    )
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    accept_tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": claim.wallet,
        "SourceTag": sponsored_mint.config.SOURCE_TAG,
        "NFTokenSellOffer": "OFFER-ACCEPTED",
        "hash": "ACCEPT-TX",
        "validated": True,
        "date": 53,
        "meta": {"TransactionResult": "tesSUCCESS"},
    }
    hconn = history_store.init_history_db(_service_env.history_db)
    history_store.insert_tx(
        hconn,
        tx_hash="ACCEPT-TX",
        ledger_index=2,
        close_time=3990,
        tx_type="NFTokenAcceptOffer",
        account=claim.wallet,
        source_tag=sponsored_mint.config.SOURCE_TAG,
        raw_json=json.dumps(accept_tx),
    )
    hconn.commit()
    hconn.close()

    async def forbidden(*args, **kwargs):
        raise AssertionError("archived acceptance must be projected before offer lookup")

    monkeypatch.setattr(server.time, "time", lambda: 4000)
    monkeypatch.setattr(server.xrpl_ops, "get_nft_sell_offers", forbidden)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", forbidden)

    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id, accept_tx_hash FROM free_mint_claims WHERE id = ?",
            (claim.id,),
        ).fetchone()
    assert row == ("accepted", "OFFER-ACCEPTED", "ACCEPT-TX")


def test_startup_offer_recovery_correlates_acceptance_before_offer_persistence(
    _service_env, monkeypatch
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rCRASHED", "offer-crash")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-OFFER-CRASH",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-OFFER-CRASH",
        nft_id="NFT-OFFER-CRASH",
        now=103,
    )
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    accept_tx = {
        "TransactionType": "NFTokenAcceptOffer",
        "Account": claim.wallet,
        "SourceTag": sponsored_mint.config.SOURCE_TAG,
        "NFTokenSellOffer": "OFFER-CRASH",
        "hash": "ACCEPT-CRASH-TX",
        "validated": True,
        "date": 53,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                {
                    "DeletedNode": {
                        "LedgerEntryType": "NFTokenOffer",
                        "LedgerIndex": "OFFER-CRASH",
                        "FinalFields": {
                            "NFTokenID": "NFT-OFFER-CRASH",
                            "Owner": sponsored_mint.config.SIGNING_ACCOUNT,
                            "Destination": claim.wallet,
                            "Amount": "0",
                            "Flags": 1,
                        },
                    }
                }
            ],
        },
    }
    hconn = history_store.init_history_db(_service_env.history_db)
    history_store.insert_tx(
        hconn,
        tx_hash="ACCEPT-CRASH-TX",
        ledger_index=2,
        close_time=3990,
        tx_type="NFTokenAcceptOffer",
        account=claim.wallet,
        source_tag=sponsored_mint.config.SOURCE_TAG,
        raw_json=json.dumps(accept_tx),
    )
    hconn.commit()
    hconn.close()

    async def forbidden(*args, **kwargs):
        raise AssertionError("validated transfer must be recovered before creating another offer")

    monkeypatch.setattr(server.time, "time", lambda: 4000)
    monkeypatch.setattr(server.xrpl_ops, "get_nft_sell_offers", forbidden)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", forbidden)

    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id, accept_tx_hash FROM free_mint_claims WHERE id = ?",
            (claim.id,),
        ).fetchone()
    assert row == ("accepted", "OFFER-CRASH", "ACCEPT-CRASH-TX")


def test_startup_replays_missing_canonical_nft_projection_idempotently(
    _service_env,
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rPROJECT", "projection-crash")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-PROJECTION-CRASH",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-PROJECTION-CRASH",
        nft_id="NFT-PROJECTION-CRASH",
        now=103,
    )

    _run(server._recover_sponsored_nft_records(_service_env.app_db, network="mainnet"))
    with sqlite3.connect(_service_env.app_db) as conn:
        # The live path stores the platform user ID, which is intentionally
        # distinct from the only identity retained by the claim journal.
        conn.execute("UPDATE LFG SET discord_id = 'actual-discord-user'")
    _run(server._recover_sponsored_nft_records(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        rows = conn.execute(
            """
            SELECT nft_number, nft_id, owner_address, metadata_url, image_url,
                   Body, Hat, network, body_type
            FROM LFG
            """
        ).fetchall()
    assert rows == [
        (
            1,
            "NFT-PROJECTION-CRASH",
            "rPROJECT",
            "https://cdn.example/1.json",
            "https://cdn.example/1.png",
            "Alien",
            "Cap",
            "mainnet",
            "Alien",
        )
    ]


def test_startup_offer_recovery_does_not_create_after_malformed_lookup(_service_env, monkeypatch):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rRECIPIENT", "offer-malformed")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-OFFER-MALFORMED",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-OFFER-MALFORMED",
        nft_id="NFT-OFFER-MALFORMED",
        now=103,
    )

    class MalformedResponse:
        result = {}

    class MalformedClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, request):
            return MalformedResponse()

    create_calls = []

    async def create_offer(*args, **kwargs):
        create_calls.append((args, kwargs))
        return "OFFER-MUST-NOT-BE-CREATED"

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", MalformedClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)

    # Per-claim failures are logged + persisted on the claim, never raised: one
    # undeliverable claim must not pin sponsored admission OFF campaign-wide.
    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id, last_error FROM free_mint_claims WHERE id = ?",
            (claim.id,),
        ).fetchone()
    assert create_calls == []
    assert row[0:2] == ("minted", None)
    assert "malformed nft_sell_offers response" in row[2]


@pytest.mark.parametrize(
    "field,value",
    [("amount", 0), ("destination", ["rRECIPIENT"])],
)
def test_offer_recovery_does_not_create_for_malformed_classification_fields(
    _service_env, monkeypatch, field, value
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, f"rRECIPIENT-{field}", f"offer-malformed-{field}")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash=f"TX-MALFORMED-{field}",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash=f"TX-MALFORMED-{field}",
        nft_id=f"NFT-MALFORMED-{field}",
        now=103,
    )
    offer = {
        "nft_offer_index": f"OFFER-{field}",
        "amount": "0",
        "destination": claim.wallet,
        "flags": server.xrpl_ops.LSF_SELL_NFTOKEN,
        "owner": "rBOT",
    }
    offer[field] = value

    class MalformedResponse:
        result = {"offers": [offer]}

    class MalformedClient:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, request):
            return MalformedResponse()

    create_calls = []

    async def create_offer(*args, **kwargs):
        create_calls.append((args, kwargs))
        return "OFFER-MUST-NOT-BE-CREATED"

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", MalformedClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)

    # Per-claim failures are logged + persisted on the claim, never raised: one
    # undeliverable claim must not pin sponsored admission OFF campaign-wide.
    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id, last_error FROM free_mint_claims WHERE id = ?",
            (claim.id,),
        ).fetchone()
    assert create_calls == []
    assert row[0:2] == ("minted", None)
    assert "malformed nft_sell_offers response" in row[2]


def test_offer_recovery_aggregates_failure_after_processing_remaining_claims(
    _service_env, monkeypatch
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claims = []
    for wallet, session_id, now in (
        ("rFAIL-FIRST", "offer-fail-first", 101),
        ("rSUCCEED-SECOND", "offer-succeed-second", 102),
        ("rFAIL-THIRD", "offer-fail-third", 103),
    ):
        claim = _reserve_recovery_claim(_service_env, wallet, session_id, now=now)
        prepare_and_forward(
            sponsored_mint,
            _service_env.app_db,
            network="mainnet",
            wallet=wallet,
            session_id=session_id,
            tx_hash=f"TX-{session_id}",
            now=now + 10,
        )
        sponsored_mint.record_minted_and_enqueue_burn(
            _service_env.app_db,
            network="mainnet",
            wallet=wallet,
            session_id=session_id,
            mint_tx_hash=f"TX-{session_id}",
            nft_id=f"NFT-{session_id}",
            now=now + 20,
        )
        claims.append(claim)

    async def lookup(nft_id, *, raise_on_error):
        assert raise_on_error is True
        if nft_id in {"NFT-offer-fail-first", "NFT-offer-fail-third"}:
            raise RuntimeError(f"{nft_id} lookup malformed")
        return [
            {
                "offer_index": "OFFER-LIVE-SECOND",
                "amount": "0",
                "destination": "rSUCCEED-SECOND",
                "flags": server.xrpl_ops.LSF_SELL_NFTOKEN,
                "owner": "rBOT",
                "expiration": None,
            }
        ]

    async def forbidden_create(*args, **kwargs):
        raise AssertionError("mixed recovery must not create an unexpected offer")

    monkeypatch.setattr(server.xrpl_ops, "get_nft_sell_offers", lookup)
    monkeypatch.setattr(server.xrpl_ops, "bot_wallet_address", lambda: "rBOT")
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", forbidden_create)

    # Two failed claims: both are persisted with last_error and the sweep still
    # returns normally -- readiness is not the offer pass's to revoke.
    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT session_id, status, offer_id, last_error FROM free_mint_claims"
            )
        }
    assert rows["offer-fail-first"][0:2] == ("minted", None)
    assert "NFT-offer-fail-first lookup malformed" in rows["offer-fail-first"][2]
    assert rows["offer-succeed-second"] == (
        "offered",
        "OFFER-LIVE-SECOND",
        None,
    )
    assert rows["offer-fail-third"][0:2] == ("minted", None)
    assert "NFT-offer-fail-third lookup malformed" in rows["offer-fail-third"][2]


def test_restart_recovery_preserves_promised_reservations_for_rebind(_service_env, monkeypatch):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    stale = _reserve_recovery_claim(_service_env, "rSTALE", "stale")
    fresh = _reserve_recovery_claim(_service_env, "rFRESH", "fresh")
    with sqlite3.connect(_service_env.app_db) as conn:
        conn.execute(
            "UPDATE free_mint_claims SET reservation_expires_at = 5000 WHERE id = ?",
            (fresh.id,),
        )
    monkeypatch.setattr(sponsored_mint.time, "time", lambda: 4000)

    report = sponsored_mint.recover_incomplete_claims(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
    )

    with sqlite3.connect(_service_env.app_db) as conn:
        statuses = dict(conn.execute("SELECT id, status FROM free_mint_claims"))
    assert report.released_reservations == ()
    assert statuses[stale.id] == "reserved"
    assert statuses[fresh.id] == "reserved"


def test_restart_recovery_keeps_uncertain_minting_claim_held(_service_env, monkeypatch):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rUNCERTAIN", "uncertain")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        now=102,
    )
    monkeypatch.setattr(sponsored_mint.time, "time", lambda: 4000)

    report = sponsored_mint.recover_incomplete_claims(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
    )

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, released_at FROM free_mint_claims WHERE id = ?", (claim.id,)
        ).fetchone()
    assert report.held_minting == (claim.id,)
    assert report.recovered_mints == ()
    assert row == ("minting", None)


def test_restart_recovery_preserves_mint_debt_and_queues_missing_offer(_service_env, monkeypatch):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    minted = _reserve_recovery_claim(_service_env, "rMINTED", "minted")
    offered = _reserve_recovery_claim(_service_env, "rOFFERED", "offered")
    for claim, nft_id in ((minted, "NFT-MINTED"), (offered, "NFT-OFFERED")):
        prepare_and_forward(
            sponsored_mint,
            _service_env.app_db,
            network="mainnet",
            wallet=claim.wallet,
            session_id=claim.session_id,
            tx_hash=f"TX-{nft_id}",
            now=102,
        )
        sponsored_mint.record_minted_and_enqueue_burn(
            _service_env.app_db,
            network="mainnet",
            wallet=claim.wallet,
            session_id=claim.session_id,
            mint_tx_hash=f"TX-{nft_id}",
            nft_id=nft_id,
            now=103,
        )
    sponsored_mint.record_offer(
        _service_env.app_db,
        network="mainnet",
        wallet=offered.wallet,
        session_id=offered.session_id,
        offer_id="OFFER-1",
        now=104,
    )
    monkeypatch.setattr(sponsored_mint.time, "time", lambda: 4000)

    report = sponsored_mint.recover_incomplete_claims(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
    )

    with sqlite3.connect(_service_env.app_db) as conn:
        statuses = dict(conn.execute("SELECT id, status FROM free_mint_claims"))
        debt = dict(conn.execute("SELECT claim_id, status FROM free_mint_burns"))
    assert report.missing_offers == (minted.id,)
    assert statuses == {minted.id: "minted", offered.id: "offered"}
    assert debt == {minted.id: "pending", offered.id: "pending"}


def test_restart_recovery_expires_campaign_without_reactivating(_service_env, monkeypatch):
    campaign = sponsored_mint.start_campaign(
        _service_env.app_db, network="mainnet", actor="test", now=100
    )
    assert campaign.state == "active"
    monkeypatch.setattr(sponsored_mint.time, "time", lambda: 4000)

    report = sponsored_mint.recover_incomplete_claims(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
    )

    assert report.campaign_state == "expired"
    status = sponsored_mint.campaign_status(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
        now=4000,
    )
    assert status.state == "expired"


def test_restart_recovery_leaves_expired_burn_lease_for_existing_worker(_service_env, monkeypatch):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rLEASED", "leased")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-LEASED",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-LEASED",
        nft_id="NFT-LEASED",
        now=103,
    )
    with sqlite3.connect(_service_env.app_db) as conn:
        conn.execute(
            """
            UPDATE free_mint_burns
            SET status = 'submitting', lease_until = 200, lease_token = 'dead-worker'
            WHERE claim_id = ?
            """,
            (claim.id,),
        )
    monkeypatch.setattr(sponsored_mint.time, "time", lambda: 4000)

    report = sponsored_mint.recover_incomplete_claims(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
    )
    leased = sponsored_burn._acquire(_service_env.app_db, 4000)

    assert len(report.reclaimable_burns) == 1
    assert leased is not None
    assert leased.id == report.reclaimable_burns[0]
    assert leased.previous_status == "submitting"


def test_restart_recovery_reports_only_requested_network_debt_and_leases(_service_env, monkeypatch):
    for network, wallet, session_id in (
        ("mainnet", "rMAIN", "main-debt"),
        ("testnet", "rTEST", "test-debt"),
    ):
        sponsored_mint.start_campaign(_service_env.app_db, network=network, actor="test", now=100)
        claim = _reserve_recovery_claim(_service_env, wallet, session_id, network=network)
        prepare_and_forward(
            sponsored_mint,
            _service_env.app_db,
            network=network,
            wallet=wallet,
            session_id=claim.session_id,
            tx_hash=f"TX-{network}",
            now=102,
        )
        sponsored_mint.record_minted_and_enqueue_burn(
            _service_env.app_db,
            network=network,
            wallet=wallet,
            session_id=claim.session_id,
            mint_tx_hash=f"TX-{network}",
            nft_id=f"NFT-{network}",
            now=103,
        )

    with sqlite3.connect(_service_env.app_db) as conn:
        conn.execute("UPDATE free_mint_burns SET status = 'submitting', lease_until = 0")
        main_burn_id = conn.execute(
            """
            SELECT b.id
            FROM free_mint_burns AS b
            JOIN free_mint_claims AS c ON c.id = b.claim_id
            WHERE c.network = 'mainnet'
            """
        ).fetchone()[0]

    monkeypatch.setattr(sponsored_mint.time, "time", lambda: 4000)
    report = sponsored_mint.recover_incomplete_claims(
        _service_env.app_db,
        _service_env.history_db,
        network="mainnet",
    )

    assert report.debt_count == 1
    assert report.reclaimable_burns == (main_burn_id,)


def test_readiness_audit_counts_debt_only_for_requested_network(_service_env, monkeypatch):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    for network, wallet, session_id in (
        ("mainnet", "rMAIN", "main-audit-debt"),
        ("testnet", "rTEST", "test-audit-debt"),
    ):
        sponsored_mint.start_campaign(_service_env.app_db, network=network, actor="test", now=100)
        claim = _reserve_recovery_claim(_service_env, wallet, session_id, network=network)
        prepare_and_forward(
            sponsored_mint,
            _service_env.app_db,
            network=network,
            wallet=wallet,
            session_id=claim.session_id,
            tx_hash=f"AUDIT-TX-{network}",
            now=102,
        )
        sponsored_mint.record_minted_and_enqueue_burn(
            _service_env.app_db,
            network=network,
            wallet=wallet,
            session_id=claim.session_id,
            mint_tx_hash=f"AUDIT-TX-{network}",
            nft_id=f"AUDIT-NFT-{network}",
            now=103,
        )

    hconn = history_store.init_history_db(_service_env.history_db)
    history_store.insert_tx(
        hconn,
        tx_hash="TX-FRESH",
        ledger_index=history_store.EARLIEST_AVAILABLE_LEDGER + 123,
        close_time=3990,
        tx_type="Payment",
        account="rUNIQUE",
        source_tag=sponsored_mint.config.SOURCE_TAG,
        raw_json="{}",
    )
    hconn.commit()
    hconn.close()
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    monkeypatch.setattr(
        audit.config,
        "SPONSORED_MINT_EXCLUDED_WALLETS",
        _PLACEHOLDER_EXCLUSIONS,
    )

    report = _run(
        audit.build_report(
            network="mainnet",
            app_db=_service_env.app_db,
            history_db=_service_env.history_db,
            now=4000,
            balance_fetch=lambda: Decimal("1000"),
        )
    )

    assert report["checks"]["debt"]["pending"] == 1
    assert report["checks"]["debt"]["amount"] == sponsored_mint.config.MINT_PRICE_LFGO


def test_readiness_audit_is_read_only_and_passes_a_safe_off_state(_service_env, monkeypatch):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    sponsored_mint.ensure_schema(_service_env.app_db)
    hconn = history_store.init_history_db(_service_env.history_db)
    history_store.insert_tx(
        hconn,
        tx_hash="TX-FRESH",
        ledger_index=history_store.EARLIEST_AVAILABLE_LEDGER + 123,
        close_time=3990,
        tx_type="Payment",
        account="rUNIQUE",
        source_tag=sponsored_mint.config.SOURCE_TAG,
        raw_json="{}",
    )
    hconn.commit()
    hconn.close()
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    monkeypatch.setattr(
        audit.config,
        "SPONSORED_MINT_EXCLUDED_WALLETS",
        _PLACEHOLDER_EXCLUSIONS,
    )

    with sqlite3.connect(_service_env.app_db) as conn:
        before = "\n".join(conn.iterdump())
    report = _run(
        audit.build_report(
            network="mainnet",
            app_db=_service_env.app_db,
            history_db=_service_env.history_db,
            now=4000,
            balance_fetch=lambda: asyncio.sleep(0, result=Decimal("100")),
        )
    )
    with sqlite3.connect(_service_env.app_db) as conn:
        after = "\n".join(conn.iterdump())

    assert report["ok"] is True
    assert report["checks"]["campaign"]["state"] == "off"
    assert report["checks"]["listener_freshness"]["age_seconds"] == 10
    assert report["checks"]["unique_count"]["count"] == 1
    assert before == after


def test_readiness_audit_fails_on_a_narrowed_baseline_source_sweep(_service_env, monkeypatch):
    # #331: a certification that swept fewer than the full source set must be
    # a FAIL in the readiness audit, not a warning — and the archive gate
    # itself must refuse the narrowed attestation.
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    sponsored_mint.ensure_schema(_service_env.app_db)
    ready_history(
        _service_env.history_db,
        network="mainnet",
        now=4000,
        close_time=3990,
        sources=("token_issuer", "signing"),
    )

    assert not sponsored_mint.archive_is_usable(
        _service_env.history_db, network="mainnet", now=4000
    )
    report = _run(
        audit.build_report(
            network="mainnet",
            app_db=_service_env.app_db,
            history_db=_service_env.history_db,
            now=4000,
            balance_fetch=lambda: asyncio.sleep(0, result=Decimal("100")),
        )
    )
    assert report["ok"] is False
    baseline = report["checks"]["baseline_sources"]
    assert baseline["ok"] is False
    assert baseline["swept"] == ["signing", "token_issuer"]
    assert baseline["missing"] == ["brix", "distributor", "issuer", "nfts"]
    assert report["checks"]["archive"]["ok"] is False


def test_readiness_audit_uses_configured_archive_lag_limit(_service_env, monkeypatch):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    sponsored_mint.ensure_schema(_service_env.app_db)
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    monkeypatch.setattr(
        audit.config,
        "SPONSORED_MINT_ARCHIVE_MAX_LAG_SECONDS",
        5,
    )

    report = _run(
        audit.build_report(
            network="mainnet",
            app_db=_service_env.app_db,
            history_db=_service_env.history_db,
            now=4000,
            balance_fetch=lambda: Decimal("100"),
        )
    )

    freshness = report["checks"]["listener_freshness"]
    assert freshness["max_age_seconds"] == 5
    assert freshness["ok"] is False


def _exclusions_report(_service_env, monkeypatch, configured):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    sponsored_mint.ensure_schema(_service_env.app_db)
    hconn = history_store.init_history_db(_service_env.history_db)
    history_store.insert_tx(
        hconn,
        tx_hash="TX-FRESH",
        ledger_index=history_store.EARLIEST_AVAILABLE_LEDGER + 123,
        close_time=3990,
        tx_type="Payment",
        account="rUNIQUE",
        source_tag=sponsored_mint.config.SOURCE_TAG,
        raw_json="{}",
    )
    hconn.commit()
    hconn.close()
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    monkeypatch.setattr(audit.config, "SPONSORED_MINT_EXCLUDED_WALLETS", configured)

    return audit, _run(
        audit.build_report(
            network="mainnet",
            app_db=_service_env.app_db,
            history_db=_service_env.history_db,
            now=4000,
            balance_fetch=lambda: Decimal("100"),
        )
    )


@pytest.mark.parametrize("configured", [(), ("",), ("  ",)])
def test_readiness_audit_rejects_empty_operator_exclusions(_service_env, monkeypatch, configured):
    # The safety intent of #334: an operator who never declared an exclusion
    # list must not reach a green preflight.
    audit, report = _exclusions_report(_service_env, monkeypatch, configured)

    assert report["ok"] is False
    exclusions = report["checks"]["exclusions"]
    assert exclusions["ok"] is False
    assert exclusions["configured"] == []
    assert audit.config.SIGNING_ACCOUNT in exclusions["effective"]
    assert audit.config.TOKEN_ISSUER_ADDRESS in exclusions["effective"]


@pytest.mark.parametrize(
    ("configured", "expected_invalid"),
    [
        (("rARBITRARY",), ["rARBITRARY"]),
        ((_PLACEHOLDER_EXCLUSIONS[0], "not-an-address"), ["not-an-address"]),
    ],
)
def test_readiness_audit_rejects_malformed_operator_exclusions(
    _service_env, monkeypatch, configured, expected_invalid
):
    audit, report = _exclusions_report(_service_env, monkeypatch, configured)

    assert report["ok"] is False
    exclusions = report["checks"]["exclusions"]
    assert exclusions["ok"] is False
    assert exclusions["invalid"] == expected_invalid


@pytest.mark.parametrize(
    "configured",
    [
        (_PLACEHOLDER_EXCLUSIONS[0],),
        _PLACEHOLDER_EXCLUSIONS,
    ],
)
def test_readiness_audit_accepts_any_wellformed_nonempty_exclusion_list(
    _service_env, monkeypatch, configured
):
    # #334: the check is structural, not identity — any explicitly configured,
    # well-formed exclusion list passes; the operator reviews the reported sets.
    audit, report = _exclusions_report(_service_env, monkeypatch, configured)

    exclusions = report["checks"]["exclusions"]
    assert exclusions["ok"] is True
    assert exclusions["invalid"] == []
    assert sorted(configured) == exclusions["configured"]
    assert audit.config.SIGNING_ACCOUNT in exclusions["effective"]


def test_readiness_audit_rejects_config_network_mismatch_before_balance_rpc(
    _service_env, monkeypatch
):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    monkeypatch.setattr(audit.config, "XRPL_NETWORK", "testnet")
    balance_calls = []

    def forbidden_balance():
        balance_calls.append(True)
        raise AssertionError("config-bound RPC must not run for a mismatched network")

    with pytest.raises(ValueError, match="must match configured XRPL_NETWORK"):
        _run(
            audit.build_report(
                network="mainnet",
                app_db=_service_env.app_db,
                history_db=_service_env.history_db,
                balance_fetch=forbidden_balance,
            )
        )

    assert balance_calls == []


def test_readiness_audit_marks_testnet_self_issuer_balance_as_noop(_service_env, monkeypatch):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    account = audit.config.SIGNING_ACCOUNT
    monkeypatch.setattr(audit.config, "XRPL_NETWORK", "testnet")
    monkeypatch.setattr(audit.config, "SIGNING_ACCOUNT", account)
    monkeypatch.setattr(audit.config, "TOKEN_ISSUER_ADDRESS", account)
    monkeypatch.setattr(
        audit.config,
        "SPONSORED_MINT_EXCLUDED_WALLETS",
        _PLACEHOLDER_EXCLUSIONS,
    )
    sponsored_mint.ensure_schema(_service_env.app_db)
    history_db = _service_env.history_dbs["testnet"]
    ready_history(history_db, network="testnet", now=4000, close_time=3990)
    balance_calls = []

    def forbidden_balance():
        balance_calls.append(True)
        raise AssertionError("self-issuer testnet readiness must not query a trust line")

    report = _run(
        audit.build_report(
            network="testnet",
            app_db=_service_env.app_db,
            history_db=history_db,
            now=4000,
            balance_fetch=forbidden_balance,
        )
    )

    assert report["checks"]["balance"] == {
        "ok": True,
        "state": "not_applicable_testnet_self_issuer",
        "balance": None,
        "required": str(Decimal(audit.config.MINT_PRICE_LFGO) * 100),
        "possible_admissions": 100,
    }
    assert balance_calls == []


def test_readiness_audit_fails_for_debt_and_incomplete_claims(_service_env, monkeypatch):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _reserve_recovery_claim(_service_env, "rINCOMPLETE", "incomplete")
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash="TX-DEBT",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash="TX-DEBT",
        nft_id="NFT-DEBT",
        now=103,
    )
    hconn = history_store.init_history_db(_service_env.history_db)
    history_store.insert_tx(
        hconn,
        tx_hash="TX-FRESH",
        ledger_index=history_store.EARLIEST_AVAILABLE_LEDGER + 123,
        close_time=3990,
        tx_type="Payment",
        account="rUNIQUE",
        source_tag=sponsored_mint.config.SOURCE_TAG,
        raw_json="{}",
    )
    hconn.commit()
    hconn.close()
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    monkeypatch.setattr(
        audit.config,
        "SPONSORED_MINT_EXCLUDED_WALLETS",
        _PLACEHOLDER_EXCLUSIONS,
    )

    report = _run(
        audit.build_report(
            network="mainnet",
            app_db=_service_env.app_db,
            history_db=_service_env.history_db,
            now=4000,
            balance_fetch=lambda: asyncio.sleep(0, result=Decimal("1000")),
        )
    )

    assert report["ok"] is False
    assert report["checks"]["debt"]["ok"] is False
    assert report["checks"]["debt"]["pending"] == 1
    assert report["checks"]["incomplete_claims"]["ok"] is False
    assert report["checks"]["incomplete_claims"]["minted"] == 1


def test_readiness_help_lists_only_supported_campaign_networks(capsys):
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")

    try:
        with pytest.raises(SystemExit) as stopped:
            audit.main(["--help"])
    finally:
        # The real CLI correctly uses asyncio.run(), which clears pytest's
        # shared current-loop slot. Restore it for legacy tests in this process.
        asyncio.set_event_loop(asyncio.new_event_loop())

    assert stopped.value.code == 0
    assert "--network {mainnet,testnet}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "outcome",
    [
        _not_admitted("ineligible"),
        _not_admitted("campaign_off"),
        _not_admitted("eligibility_unavailable"),
        RuntimeError("reservation store unavailable"),
    ],
    ids=["known-wallet", "inactive", "history-failure", "store-failure"],
)
def test_non_admission_uses_existing_paid_preparation_path(monkeypatch, outcome):
    calls = []

    def reserve(*args, **kwargs):
        calls.append("reserve")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def prepare(self):
        calls.append("prepare")
        await _paid_prepare()(self)

    async def fake_wrapper(session):
        calls.append("launch")

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", reserve)
    monkeypatch.setattr(mint_flow.MintSession, "prepare_payment", prepare)
    monkeypatch.setattr(server, "_run_mint_session_and_publish", fake_wrapper)

    async def scenario():
        response = await server.handle_mint_start(_post_request())
        session = next(iter(server.mint_sessions.values()))
        await session.task
        return response, session

    response, session = _run(scenario())
    body = json.loads(response.body)

    assert response.status == 200
    assert calls == ["reserve", "prepare", "launch"]
    assert body["sponsored"] is False
    assert body["pay_with"] == "XRP"
    assert body["pay_amount"] == "10"
    assert body["payment_link"] == "https://xumm.app/sign/paid"
    assert session.payment_uuid == "paid"


def test_bulk_mint_never_consults_sponsorship(monkeypatch):
    async def prepare(self):
        self.pay_with = "XRP"
        self.unit_price = "10"
        self.pay_amount = "10"
        self.payment_link = "https://xumm.app/sign/bulk"
        self.payment_uuid = "bulk"

    async def run_job(_job):
        return None

    def forbidden(*args, **kwargs):
        raise AssertionError("bulk mint must not consult sponsored admission")

    monkeypatch.setattr(server.sponsored_mint, "reserve_if_eligible", forbidden)
    monkeypatch.setattr(bulk_mint_flow.BulkMintJob, "prepare_payment", prepare)
    monkeypatch.setattr(bulk_mint_flow, "persist", lambda job: True)
    monkeypatch.setattr(bulk_mint_flow, "run_bulk_mint_job", run_job)

    async def scenario():
        response = await server.handle_bulk_mint_start(
            _post_request("/api/mint/bulk", {"quantity": 1})
        )
        job = next(iter(server.bulk_sessions.values()))
        await job.task
        return response

    assert _run(scenario()).status == 200


def test_sponsored_payment_methods_never_construct_a_payment(monkeypatch):
    session = _sponsored_session()

    async def forbidden(*args, **kwargs):
        raise AssertionError("payment boundary was reached")

    monkeypatch.setattr(mint_flow.xrpl_ops, "get_trustline_balance", forbidden)
    monkeypatch.setattr(mint_flow.xumm_ops, "create_payment_payload", forbidden)

    with pytest.raises(RuntimeError, match="sponsored"):
        session._payment_params()
    with pytest.raises(RuntimeError, match="sponsored"):
        _run(session.prepare_payment())
    with pytest.raises(RuntimeError, match="sponsored"):
        _run(session.regenerate_payment())
    session.ensure_payment_fallback()

    assert session.pay_with == "SPONSORED"
    assert session.pay_amount == "0"
    assert session.payment_link is None
    assert session.payment_uuid is None


def _prepared(tx_hash="MINTTX1"):
    return mint_flow.xrpl_ops.MintPreparation(
        state="prepared",
        tx_hash=tx_hash,
        tx_blob=f"BLOB:{tx_hash}",
        error=None,
        signed_ledger_floor=1,
    )


async def _noop_sponsored_callback(*args):
    return None


def test_sponsored_session_skips_payment_and_orders_irreversible_callbacks(monkeypatch):
    events = []

    async def fake_mint_one_unit(**kwargs):
        assert events == []
        events.append("mint_one_unit")
        kwargs["on_state"](mint_flow.MINTING)
        await kwargs["on_mint_prepared"](
            4000, "https://cdn/4000.json", '{"attributes": []}', "Alien", _prepared()
        )
        await kwargs["on_mint_forwarded"]("MINTTX1")
        await kwargs["on_mint_confirmed"](4000, "NFT1", "MINTTX1", "https://cdn/NFT1.png")
        kwargs["on_state"](mint_flow.CREATING_OFFER)
        await kwargs["on_offer_created"]("OFFER1", None)
        events.append("offer_complete")
        return mint_flow.UnitResult(
            4000,
            "NFT1",
            "https://cdn/NFT1.png",
            "OFFER1",
            {"qr_url": "q", "xumm_url": "x", "uuid": "accept"},
            None,
        )

    async def record_mint(nft_number, nft_id, mint_tx_hash, image_url):
        events.append("record_mint")

    async def record_offer(offer_id, error):
        events.append(("record_offer", offer_id, error))

    async def record_prepared(*args):
        events.append("record_prepared")

    async def record_forwarded(tx_hash):
        events.append("record_forwarded")

    async def forbidden(*args, **kwargs):
        raise AssertionError("sponsored session reached a payment operation")

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
    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", forbidden)
    monkeypatch.setattr(mint_flow.xrpl_ops, "buy_and_burn", forbidden)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _allocate_4000)
    monkeypatch.setattr(mint_flow, "mint_one_unit", fake_mint_one_unit)

    session = _sponsored_session()
    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_mint=record_mint,
            on_sponsored_prepared=record_prepared,
            on_sponsored_forwarded=record_forwarded,
            on_sponsored_offer=record_offer,
        )
    )

    assert session.state == mint_flow.OFFER_READY
    assert session.nft_id == "NFT1"
    assert events == [
        "mint_one_unit",
        "record_prepared",
        "record_forwarded",
        "record_mint",
        ("record_offer", "OFFER1", None),
        "offer_complete",
    ]


@pytest.mark.parametrize("prior_status", ["minting", "minted"])
def test_sponsored_runner_never_remints_preexisting_irreversible_claim(
    _service_env, monkeypatch, prior_status
):
    session = _sponsored_session()
    _reserve_claim(_service_env, session)
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=session.wallet_address,
        session_id=session.id,
        tx_hash="MINTTX1",
        now=102,
    )
    if prior_status == "minted":
        sponsored_mint.record_minted_and_enqueue_burn(
            _service_env.app_db,
            network="mainnet",
            wallet=session.wallet_address,
            session_id=session.id,
            mint_tx_hash="MINTTX1",
            nft_id="NFT1",
            now=103,
        )
    calls = []

    async def forbidden_mint(**kwargs):
        calls.append(kwargs)
        raise AssertionError("pre-existing irreversible claim was reminted")

    async def record_mint(*args):
        raise AssertionError("recovery state must not record a second mint")

    monkeypatch.setattr(mint_flow, "mint_one_unit", forbidden_mint)
    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_prepared=_noop_sponsored_callback,
            on_sponsored_forwarded=_noop_sponsored_callback,
            on_sponsored_mint=record_mint,
            on_sponsored_offer=_noop_sponsored_callback,
        )
    )

    assert calls == []
    assert session.state == mint_flow.FAILED
    assert session.error == f"sponsored mint recovery required: claim is {prior_status}"
    assert session.nft_id == ("NFT1" if prior_status == "minted" else None)


def _reserve_claim(paths, session):
    sponsored_mint.start_campaign(paths.app_db, network="mainnet", actor="test", now=100)
    result = sponsored_mint.reserve_if_eligible(
        paths.app_db,
        paths.history_db,
        network="mainnet",
        wallet=session.wallet_address,
        session_id=session.id,
        now=101,
    )
    assert result.sponsored
    assert result.claim is not None
    session.sponsored_claim_id = result.claim.id


async def _record_prepared(
    paths,
    session,
    nft_number,
    metadata_url,
    metadata_json,
    body_type,
    preparation,
    still_token=None,
):
    return await asyncio.to_thread(
        sponsored_mint.record_mint_prepared,
        paths.app_db,
        network="mainnet",
        wallet=session.claim_wallet,
        session_id=session.id,
        tx_hash=preparation.tx_hash,
        tx_blob=preparation.tx_blob,
        signed_ledger_floor=preparation.signed_ledger_floor,
        nft_number=nft_number,
        metadata_url=metadata_url,
        metadata_json=metadata_json,
        body_type=body_type,
        still_token=still_token,
    )


async def _record_forwarded(paths, session, tx_hash):
    return await asyncio.to_thread(
        sponsored_mint.mark_mint_forwarded,
        paths.app_db,
        network="mainnet",
        wallet=session.claim_wallet,
        session_id=session.id,
        tx_hash=tx_hash,
    )


async def _record_mint(paths, session, nft_number, nft_id, mint_tx_hash, image_url):
    return await asyncio.to_thread(
        sponsored_mint.record_minted_and_enqueue_burn,
        paths.app_db,
        network="mainnet",
        wallet=session.claim_wallet,
        session_id=session.id,
        mint_tx_hash=mint_tx_hash,
        nft_id=nft_id,
    )


async def _record_offer(paths, session, offer_id, error):
    return await asyncio.to_thread(
        sponsored_mint.record_offer,
        paths.app_db,
        network="mainnet",
        wallet=session.claim_wallet,
        session_id=session.id,
        offer_id=offer_id,
        error=error,
    )


@pytest.mark.parametrize("campaign_end", ["stop", "expire"])
def test_admitted_claim_survives_campaign_stop_or_expiry(_service_env, monkeypatch, campaign_end):
    session = _sponsored_session()
    _reserve_claim(_service_env, session)
    if campaign_end == "stop":
        sponsored_mint.stop_campaign(_service_env.app_db, network="mainnet", actor="test", now=102)
    else:
        status = sponsored_mint.campaign_status(
            _service_env.app_db, _service_env.history_db, network="mainnet", now=3700
        )
        assert status.state == "expired"

    durable_before_offer = []

    async def fake_mint_one_unit(**kwargs):
        kwargs["on_state"](mint_flow.MINTING)
        await kwargs["on_mint_prepared"](
            4000, "https://cdn/4000.json", '{"attributes": []}', "Alien", _prepared()
        )
        await kwargs["on_mint_forwarded"]("MINTTX1")
        await kwargs["on_mint_confirmed"](4000, "NFT1", "MINTTX1", "https://cdn/NFT1.png")
        with sqlite3.connect(_service_env.app_db) as conn:
            durable_before_offer.append(
                (
                    conn.execute(
                        "SELECT status, nft_id FROM free_mint_claims WHERE session_id = ?",
                        (session.id,),
                    ).fetchone(),
                    conn.execute("SELECT COUNT(*) FROM free_mint_burns").fetchone()[0],
                )
            )
        kwargs["on_state"](mint_flow.CREATING_OFFER)
        await kwargs["on_offer_created"]("OFFER1", None)
        return mint_flow.UnitResult(
            4000,
            "NFT1",
            "https://cdn/NFT1.png",
            "OFFER1",
            {"qr_url": "q", "xumm_url": "x", "uuid": "accept"},
            None,
        )

    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _allocate_4000)
    monkeypatch.setattr(mint_flow, "mint_one_unit", fake_mint_one_unit)

    async def record_mint(*args):
        result = await _record_mint(_service_env, session, *args)
        assert result is not None

    async def record_offer(*args):
        result = await _record_offer(_service_env, session, *args)
        assert result is not None

    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_mint=record_mint,
            on_sponsored_prepared=lambda *args: _record_prepared(_service_env, session, *args),
            on_sponsored_forwarded=lambda tx_hash: _record_forwarded(
                _service_env, session, tx_hash
            ),
            on_sponsored_offer=record_offer,
        )
    )

    assert durable_before_offer == [(("minted", "NFT1"), 1)]
    with sqlite3.connect(_service_env.app_db) as conn:
        claim = conn.execute(
            "SELECT status, nft_id, offer_id FROM free_mint_claims WHERE session_id = ?",
            (session.id,),
        ).fetchone()
        burns = conn.execute("SELECT COUNT(*) FROM free_mint_burns").fetchone()[0]
    assert claim == ("offered", "NFT1", "OFFER1")
    assert burns == 1
    assert session.state == mint_flow.OFFER_READY


def test_offer_failure_retains_consumed_claim_nft_and_burn(_service_env, monkeypatch):
    session = _sponsored_session()
    _reserve_claim(_service_env, session)

    async def fake_mint_one_unit(**kwargs):
        kwargs["on_state"](mint_flow.MINTING)
        await kwargs["on_mint_prepared"](
            4000, "https://cdn/4000.json", '{"attributes": []}', "Alien", _prepared()
        )
        await kwargs["on_mint_forwarded"]("MINTTX1")
        await kwargs["on_mint_confirmed"](4000, "NFT1", "MINTTX1", "https://cdn/NFT1.png")
        kwargs["on_state"](mint_flow.CREATING_OFFER)
        await kwargs["on_offer_created"](None, "offer creation failed")
        return mint_flow.UnitResult(
            4000,
            "NFT1",
            "https://cdn/NFT1.png",
            None,
            None,
            "offer creation failed",
        )

    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _allocate_4000)
    monkeypatch.setattr(mint_flow, "mint_one_unit", fake_mint_one_unit)

    async def record_mint(*args):
        assert await _record_mint(_service_env, session, *args) is not None

    async def record_offer(*args):
        assert await _record_offer(_service_env, session, *args) is not None

    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_mint=record_mint,
            on_sponsored_prepared=lambda *args: _record_prepared(_service_env, session, *args),
            on_sponsored_forwarded=lambda tx_hash: _record_forwarded(
                _service_env, session, tx_hash
            ),
            on_sponsored_offer=record_offer,
        )
    )

    with sqlite3.connect(_service_env.app_db) as conn:
        claim = conn.execute(
            "SELECT status, nft_id, offer_id, last_error "
            "FROM free_mint_claims WHERE session_id = ?",
            (session.id,),
        ).fetchone()
        burns = conn.execute("SELECT COUNT(*) FROM free_mint_burns").fetchone()[0]
    assert claim == ("minted", "NFT1", None, "offer creation failed")
    assert burns == 1
    assert session.state == mint_flow.FAILED
    assert session.nft_id == "NFT1"


def test_post_mint_persistence_failure_is_recovery_visible_and_never_releases(monkeypatch):
    calls = []

    async def fake_mint_one_unit(**kwargs):
        calls.append("mint")
        await kwargs["on_mint_prepared"](
            4000, "https://cdn/4000.json", '{"attributes": []}', "Alien", _prepared()
        )
        await kwargs["on_mint_forwarded"]("MINTTX1")

        await kwargs["on_mint_confirmed"](4000, "NFT1", "MINTTX1", "https://cdn/NFT1.png")
        raise AssertionError("the failing mint callback must stop offer work")

    async def persistence_failure(*args):
        calls.append("persist")
        raise RuntimeError("claim persistence failed after NFT confirmation")

    async def record_prepared(*args):
        calls.append("prepared")

    async def record_forwarded(*args):
        calls.append("forwarded")

    def forbidden_release(*args, **kwargs):
        raise AssertionError("a confirmed NFT claim must never be released")

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
    monkeypatch.setattr(mint_flow.sponsored_mint, "release_reservation", forbidden_release)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _allocate_4000)
    monkeypatch.setattr(mint_flow, "mint_one_unit", fake_mint_one_unit)

    session = _sponsored_session()
    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_prepared=record_prepared,
            on_sponsored_forwarded=record_forwarded,
            on_sponsored_mint=persistence_failure,
            on_sponsored_offer=_noop_sponsored_callback,
        )
    )

    assert calls == ["mint", "prepared", "forwarded", "persist"]
    assert session.state == mint_flow.FAILED
    assert session.nft_id == "NFT1"
    assert "claim persistence failed" in session.error


def test_live_definitive_mint_failure_restores_durable_promise(_service_env, monkeypatch):
    session = _sponsored_session()
    _reserve_claim(_service_env, session)

    async def fake_mint_one_unit(**kwargs):
        preparation = _prepared("DEFINITIVE-MINT-TX")
        await kwargs["on_mint_prepared"](
            4000,
            "https://cdn/4000.json",
            '{"attributes": []}',
            "Alien",
            preparation,
        )
        await kwargs["on_mint_forwarded"]("DEFINITIVE-MINT-TX")
        return mint_flow.UnitResult(
            4000,
            None,
            "https://cdn/4000.png",
            None,
            None,
            "tecNO_PERMISSION",
            mint_tx_hash="DEFINITIVE-MINT-TX",
            mint_definitively_failed=True,
        )

    async def forbidden(*args):
        raise AssertionError("a definitive mint failure must not reach post-mint work")

    monkeypatch.setattr(mint_flow, "_allocate_nft_number", _allocate_4000)
    monkeypatch.setattr(mint_flow, "mint_one_unit", fake_mint_one_unit)

    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_prepared=lambda *args: _record_prepared(_service_env, session, *args),
            on_sponsored_forwarded=lambda tx_hash: _record_forwarded(
                _service_env, session, tx_hash
            ),
            on_sponsored_mint=forbidden,
            on_sponsored_offer=forbidden,
        )
    )

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, mint_signed_tx_hash, mint_signed_tx_blob, "
            "mint_forwarded_at, last_error FROM free_mint_claims WHERE session_id=?",
            (session.id,),
        ).fetchone()
    assert row == ("reserved", None, None, None, "tecNO_PERMISSION")
    assert session.state == mint_flow.FAILED
    assert session.sponsorship_irreversible is False


def test_service_wrapper_supplies_durable_sponsored_callbacks(monkeypatch):
    seen = {}

    async def fake_run(
        session,
        *,
        on_sponsored_prepared=None,
        on_sponsored_forwarded=None,
        on_sponsored_mint=None,
        on_sponsored_offer=None,
    ):
        seen["callbacks"] = tuple(
            callback is not None
            for callback in (
                on_sponsored_prepared,
                on_sponsored_forwarded,
                on_sponsored_mint,
                on_sponsored_offer,
            )
        )
        await on_sponsored_prepared(
            4000, "https://cdn/4000.json", '{"attributes": []}', "Alien", _prepared()
        )
        await on_sponsored_forwarded("MINTTX1")
        await on_sponsored_mint(4000, "NFT1", "MINTTX1", "https://cdn/NFT1.png")
        await on_sponsored_offer("OFFER1", None)
        session.state = mint_flow.OFFER_READY

    def record_prepared(*args, **kwargs):
        seen["prepared"] = kwargs
        return SimpleNamespace(mint_signed_tx_hash=kwargs["tx_hash"])

    def record_forwarded(*args, **kwargs):
        seen["forwarded"] = kwargs
        return SimpleNamespace(claim=SimpleNamespace(mint_signed_tx_hash=kwargs["tx_hash"]))

    def record_mint(*args, **kwargs):
        seen["mint"] = kwargs
        return SimpleNamespace(status="minted")

    def record_offer(*args, **kwargs):
        seen["offer"] = kwargs
        return SimpleNamespace(status="offered")

    monkeypatch.setattr(server.sponsored_mint, "record_mint_prepared", record_prepared)
    monkeypatch.setattr(server.sponsored_mint, "mark_mint_forwarded", record_forwarded)

    async def no_publish(_session):
        return None

    monkeypatch.setattr(server.mint_flow, "run_mint_session", fake_run)
    monkeypatch.setattr(server.sponsored_mint, "record_minted_and_enqueue_burn", record_mint)
    monkeypatch.setattr(server.sponsored_mint, "record_offer", record_offer)
    monkeypatch.setattr(server, "_publish_mint_terminal", no_publish)

    session = _sponsored_session()
    _run(server._run_mint_session_and_publish(session))

    assert seen["callbacks"] == (True, True, True, True)
    assert seen["mint"]["wallet"] == "rNEW"
    assert seen["mint"]["session_id"] == session.id
    assert seen["prepared"]["tx_hash"] == "MINTTX1"
    assert seen["forwarded"]["tx_hash"] == "MINTTX1"
    assert seen["mint"]["nft_id"] == "NFT1"
    assert seen["mint"]["mint_tx_hash"] == "MINTTX1"
    assert seen["offer"]["offer_id"] == "OFFER1"


# --- #330: persist the image-archive staging token on sponsored claims ------


_STILL_METADATA = json.dumps(
    {
        "name": "LFG #4000",
        "image": "https://cdn.example/4000/4000_0.png",
        "attributes": [{"trait_type": "Body", "value": "Alien"}],
    }
)


def _prepare_rebound_claim(paths, *, still_token, old="sess-old", new="sess-new"):
    """Reserve -> journal the prepared mint (optionally with a staging token)
    -> rebind to a fresh session id, mirroring a resumed sponsored mint."""
    sponsored_mint.start_campaign(paths.app_db, network="mainnet", actor="test", now=100)
    result = sponsored_mint.reserve_if_eligible(
        paths.app_db,
        paths.history_db,
        network="mainnet",
        wallet="rNEW",
        session_id=old,
        now=101,
    )
    assert result.sponsored and result.claim is not None
    sponsored_mint.record_mint_prepared(
        paths.app_db,
        network="mainnet",
        wallet="rNEW",
        session_id=old,
        tx_hash="A" * 64,
        tx_blob="BLOB:" + "A" * 64,
        signed_ledger_floor=500,
        nft_number=4000,
        metadata_url="https://cdn.example/4000/4000_0.json",
        metadata_json=_STILL_METADATA,
        body_type="Alien",
        still_token=still_token,
    )
    rebound = sponsored_mint.rebind_reservation(
        paths.app_db,
        network="mainnet",
        wallet="rNEW",
        expected_session_id=old,
        new_session_id=new,
    )
    assert rebound is not None
    return rebound


def test_prepared_journal_persists_composing_still_token(_service_env):
    rebound = _prepare_rebound_claim(_service_env, still_token="sess-old")
    assert rebound.session_id == "sess-new"
    # The composing session's staging token survives the rebind verbatim.
    assert rebound.mint_still_token == "sess-old"


def test_prepared_journal_still_token_forward_migrates(_service_env):
    _prepare_rebound_claim(_service_env, still_token="sess-old")
    with sqlite3.connect(_service_env.app_db) as conn:
        conn.execute("ALTER TABLE free_mint_claims DROP COLUMN mint_still_token")
    sponsored_mint.ensure_schema(_service_env.app_db)
    claim = sponsored_mint.claim_for_session(
        _service_env.app_db, network="mainnet", wallet="rNEW", session_id="sess-new"
    )
    assert claim is not None
    # Re-added by the self-migration; pre-migration rows read as NULL.
    assert claim.mint_still_token is None


def _stub_resume_success(monkeypatch):
    async def submit(**kwargs):
        return SimpleNamespace(state="validated", nft_id="NFTID1", error=None, tx_hash="A" * 64)

    async def create_offer(*args, **kwargs):
        return "OFFER1"

    async def accept_payload(*args, **kwargs):
        return {"qr_url": "q", "xumm_url": "x", "uuid": "u"}

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", submit)
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(mint_flow.xumm_ops, "create_accept_offer_payload", accept_payload)
    monkeypatch.setattr(mint_flow, "record_nft_mint", lambda **kwargs: True)
    monkeypatch.setattr(
        mint_flow,
        "rarity",
        SimpleNamespace(
            connect=lambda: SimpleNamespace(close=lambda: None),
            start_boost_clock=lambda *a, **k: None,
            recalculate_rarity=lambda conn: None,
            BODY_SENTINEL="_body",
            BODY_CATEGORY="Body",
        ),
    )


async def _resume_noop(*args, **kwargs):
    return None


def _run_resume(claim):
    return _run(
        mint_flow.mint_one_unit(
            discord_id="dev",
            wallet_address="rNEW",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4000,
            session_tag="sess-new",
            resume_prepared=claim,
            on_mint_forwarded=_resume_noop,
            on_mint_confirmed=_resume_noop,
            on_offer_created=_resume_noop,
        )
    )


def test_resumed_sponsored_mint_promotes_persisted_still(_service_env, tmp_path, monkeypatch):
    from lfg_core import image_archive

    archive = tmp_path / "images"
    monkeypatch.setenv("IMAGES_DIR", str(archive))
    claim = _prepare_rebound_claim(_service_env, still_token="sess-old")
    staged = image_archive.pending_still_path("mainnet", 4000, "sess-old")
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    with open(staged, "wb") as fh:
        fh.write(b"composed-still")

    _stub_resume_success(monkeypatch)
    res = _run_resume(claim)

    assert res.error is None and res.nft_id == "NFTID1"
    assert (archive / "4000.png").read_bytes() == b"composed-still"
    assert not os.path.exists(staged)


def test_resumed_sponsored_mint_null_token_skips_archive(_service_env, tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path / "images"))
    claim = _prepare_rebound_claim(_service_env, still_token=None)
    assert claim.mint_still_token is None

    calls = []
    monkeypatch.setattr(
        mint_flow.image_archive,
        "promote_still",
        lambda *args: calls.append(("promote", args)) or False,
    )
    monkeypatch.setattr(
        mint_flow.image_archive,
        "discard_still",
        lambda *args: calls.append(("discard", args)),
    )

    _stub_resume_success(monkeypatch)
    res = _run_resume(claim)

    # Pre-migration claim resumes fine but never touches the archive: a wrong
    # token must neither promote foreign staged art nor delete anything.
    assert res.error is None and res.nft_id == "NFTID1"
    assert calls == []


def test_resumed_sponsored_mint_definitive_failure_discards_staged_still(
    _service_env, tmp_path, monkeypatch
):
    from lfg_core import image_archive

    archive = tmp_path / "images"
    monkeypatch.setenv("IMAGES_DIR", str(archive))
    claim = _prepare_rebound_claim(_service_env, still_token="sess-old")
    staged = image_archive.pending_still_path("mainnet", 4000, "sess-old")
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    with open(staged, "wb") as fh:
        fh.write(b"composed-still")

    async def submit(**kwargs):
        return SimpleNamespace(state="failed", nft_id=None, error="tec failure", tx_hash=None)

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", submit)

    res = _run_resume(claim)

    assert res.mint_definitively_failed
    # The staged file is unreachable by any current-session discard; the
    # persisted token is the only key that can clean it up.
    assert not os.path.exists(staged)
    assert not (archive / "4000.png").exists()


# --- undeliverable destination (unfunded wallet) ---------------------------
#
# Prod incident 2026-08-17: a claim's destination account had never been
# funded, so it did not exist on-ledger. NFTokenCreateOffer against it can only
# ever return tecNO_DST, create_nft_offer collapses that to None (the #211
# contract), and recovery raised -- pinning _sponsored_recovery_ready False and
# disabling sponsored admission campaign-wide on EVERY boot. One undeliverable
# claim must never take free mints down for everyone.


def _minted_claim_awaiting_offer(_service_env, wallet, session, nft_id):
    claim = _reserve_recovery_claim(_service_env, wallet, session)
    prepare_and_forward(
        sponsored_mint,
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        tx_hash=f"TX-{nft_id}",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        _service_env.app_db,
        network="mainnet",
        wallet=claim.wallet,
        session_id=claim.session_id,
        mint_tx_hash=f"TX-{nft_id}",
        nft_id=nft_id,
        now=103,
    )
    return claim


class _NoSellOffers:
    result = {"offers": []}


class _NoSellOffersClient:
    def __init__(self, *args, **kwargs):
        pass

    def request(self, request):
        return _NoSellOffers()


def test_offer_recovery_skips_unfunded_destination_without_blocking_admission(
    _service_env, monkeypatch
):
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _minted_claim_awaiting_offer(
        _service_env, "rUNFUNDED", "offer-unfunded", "NFT-UNFUNDED"
    )

    create_calls = []

    async def create_offer(*args, **kwargs):
        create_calls.append((args, kwargs))
        return None

    async def account_exists(address):
        assert address == claim.wallet
        return False

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", _NoSellOffersClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(server.xrpl_ops, "account_exists", account_exists)

    # must NOT raise -- an undeliverable claim is skipped, not fatal
    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    # and must not waste a doomed tecNO_DST submission (which burns a fee)
    assert create_calls == []

    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id, last_error FROM free_mint_claims WHERE id = ?",
            (claim.id,),
        ).fetchone()
    # left recoverable on purpose: the NFT is still held by the issuer and the
    # user is still owed it, so a later boot re-attempts once they fund up.
    assert row[0:2] == ("minted", None)
    assert "unfunded" in row[2] or "does not exist" in row[2]


def test_offer_recovery_still_fails_closed_when_account_lookup_is_indeterminate(
    _service_env, monkeypatch
):
    # Only a DEFINITIVE actNotFound may downgrade the failure. A lookup that
    # merely failed says nothing about deliverability, so the old fail-closed
    # behavior must survive -- otherwise an RPC blip silently drops a claim.
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _minted_claim_awaiting_offer(_service_env, "rINDETERMINATE", "offer-indet", "NFT-INDET")

    async def create_offer(*args, **kwargs):
        return None

    async def account_exists(address):
        return None  # lookup failed -- unknown, not "absent"

    async def disallows(address):
        return None  # flag lookup also unknown -- must stay fail-closed

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", _NoSellOffersClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(server.xrpl_ops, "account_exists", account_exists)
    monkeypatch.setattr(server.xrpl_ops, "disallows_incoming_nft_offers", disallows)

    # Per-claim failures are logged + persisted on the claim, never raised: one
    # undeliverable claim must not pin sponsored admission OFF campaign-wide.
    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        status = conn.execute(
            "SELECT status FROM free_mint_claims WHERE id = ?", (claim.id,)
        ).fetchone()[0]
    assert status == "minted"


def test_offer_recovery_one_undeliverable_claim_does_not_block_a_deliverable_one(
    _service_env, monkeypatch
):
    # The blast-radius regression itself: the campaign must keep working for
    # everybody else while one claim sits undeliverable.
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    bad = _minted_claim_awaiting_offer(_service_env, "rUNFUNDED2", "offer-bad", "NFT-BAD")
    good = _minted_claim_awaiting_offer(_service_env, "rFUNDED", "offer-good", "NFT-GOOD")

    async def create_offer(nft_id, destination, *args, **kwargs):
        assert destination == good.wallet
        return "OFFER-GOOD"

    async def account_exists(address):
        return address != bad.wallet

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", _NoSellOffersClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(server.xrpl_ops, "account_exists", account_exists)

    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    with sqlite3.connect(_service_env.app_db) as conn:
        rows = dict(
            conn.execute(
                "SELECT id, offer_id FROM free_mint_claims WHERE id IN (?, ?)",
                (bad.id, good.id),
            ).fetchall()
        )
    assert rows[good.id] == "OFFER-GOOD"
    assert rows[bad.id] is None


# --- xrpl_ops.account_exists three-way contract ----------------------------


class _AccountInfoResponse:
    def __init__(self, ok, result):
        self._ok = ok
        self.result = result

    def is_successful(self):
        return self._ok


def _account_client(response):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def request(self, request):
            if isinstance(response, Exception):
                raise response
            return response

    return _Client


@pytest.mark.parametrize(
    "response,expected",
    [
        (_AccountInfoResponse(True, {"account_data": {"Balance": "10000000"}}), True),
        (_AccountInfoResponse(False, {"error": "actNotFound"}), False),
        # anything short of a definitive actNotFound is UNKNOWN, not absent
        (_AccountInfoResponse(False, {"error": "tooBusy"}), None),
        (_AccountInfoResponse(False, {}), None),
        (RuntimeError("connection reset"), None),
    ],
)
def test_account_exists_three_way_contract(monkeypatch, response, expected):
    monkeypatch.setattr(server.xrpl_ops, "AsyncJsonRpcClient", _account_client(response))
    assert _run(server.xrpl_ops.account_exists("rSOMEBODY")) is expected


@pytest.mark.parametrize("outcome", ["returns_none", "raises"])
def test_undeliverable_note_failure_does_not_disable_admission(
    _service_env, monkeypatch, caplog, outcome
):
    # CodeRabbit (PR #387) rightly flagged that the record_offer result was
    # discarded. It is now validated -- but a failure must NOT raise: the write
    # is only an explanatory breadcrumb, the claim is already 'minted' and stays
    # 'minted' either way, and raising would re-disable admission
    # campaign-wide over a failed log string. That is exactly the blast radius
    # this branch removes.
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _minted_claim_awaiting_offer(
        _service_env, "rUNFUNDED3", f"offer-note-{outcome}", f"NFT-NOTE-{outcome}"
    )

    record_calls = []

    def broken_record_offer(*args, **kwargs):
        record_calls.append((args, kwargs))
        if outcome == "raises":
            raise sqlite3.OperationalError("database is locked")
        return None

    async def account_exists(address):
        return False

    async def create_offer(*args, **kwargs):
        raise AssertionError("must not submit a doomed offer")

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", _NoSellOffersClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(server.xrpl_ops, "account_exists", account_exists)
    monkeypatch.setattr(server.sponsored_mint, "record_offer", broken_record_offer)

    with caplog.at_level(logging.ERROR):
        # the sweep still completes -> startup stays ready -> admission stays live
        _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    # the diagnostic write must actually be ATTEMPTED, with the right payload --
    # otherwise this test would still pass if the call were dropped entirely
    assert len(record_calls) == 1
    _, kwargs = record_calls[0]
    assert kwargs["wallet"] == claim.wallet
    assert kwargs["offer_id"] is None
    assert kwargs["error"].startswith("destination account does not exist on-ledger")

    # and its failure must be visible to an operator, not swallowed
    assert any(
        rec.levelname == "ERROR" and "note not persisted" in rec.getMessage()
        for rec in caplog.records
    )

    with sqlite3.connect(_service_env.app_db) as conn:
        status = conn.execute(
            "SELECT status FROM free_mint_claims WHERE id = ?", (claim.id,)
        ).fetchone()[0]
    # still recoverable on the next boot, which is the property that matters
    assert status == "minted"


def test_undeliverable_claim_leaves_startup_recovery_ready(_service_env, monkeypatch):
    # The boundary that actually matters. _recover_sponsored_offers not raising
    # is only a proxy; what the outage came down to is _sponsored_recovery_ready
    # being pinned False, which is what gates sponsored admission. Drive the real
    # startup entry point and assert the gate itself.
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    _minted_claim_awaiting_offer(_service_env, "rUNFUNDED4", "offer-ready", "NFT-READY")

    async def account_exists(address):
        return False

    async def create_offer(*args, **kwargs):
        raise AssertionError("must not submit a doomed offer")

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", _NoSellOffersClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(server.xrpl_ops, "account_exists", account_exists)
    monkeypatch.setattr(server, "_sponsored_recovery_ready", False, raising=False)

    async def resume():
        await server._recover_sponsored_offers(_service_env.app_db, network="mainnet")

    monkeypatch.setattr(server, "resume_bulk_jobs", resume)

    _run(server._start_bulk_resume({}))

    # before the fix this was False -> sponsored admission disabled for everyone
    assert server._sponsored_recovery_ready is True


def test_offer_recovery_skips_offer_blocked_destination_without_blocking_admission(
    _service_env, monkeypatch
):
    # prod 2026-08-20: a funded wallet with lsfDisallowIncomingNFTokenOffer set.
    # NFTokenCreateOffer against it can only ever return tecNO_PERMISSION, so
    # it is skipped (no doomed fee-burning submit) and left 'minted' for a
    # later boot once the holder clears the flag.
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _minted_claim_awaiting_offer(_service_env, "rBLOCKED", "offer-blocked", "NFT-BLOCKED")

    create_calls = []

    async def create_offer(*args, **kwargs):
        create_calls.append((args, kwargs))
        return None

    async def account_exists(address):
        return True  # funded -- the unfunded branch must NOT fire

    async def disallows(address):
        assert address == claim.wallet
        return True

    monkeypatch.setattr(server.xrpl_ops, "JsonRpcClient", _NoSellOffersClient)
    monkeypatch.setattr(server.xrpl_ops, "create_nft_offer", create_offer)
    monkeypatch.setattr(server.xrpl_ops, "account_exists", account_exists)
    monkeypatch.setattr(server.xrpl_ops, "disallows_incoming_nft_offers", disallows)

    _run(server._recover_sponsored_offers(_service_env.app_db, network="mainnet"))

    assert create_calls == []
    with sqlite3.connect(_service_env.app_db) as conn:
        row = conn.execute(
            "SELECT status, offer_id, last_error FROM free_mint_claims WHERE id = ?",
            (claim.id,),
        ).fetchone()
    assert row[0:2] == ("minted", None)
    assert "lsfDisallowIncomingNFTokenOffer" in row[2]


@pytest.mark.parametrize(
    "result, expected",
    [
        (
            {"account_flags": {"disallowIncomingNFTokenOffer": True}, "account_data": {"Flags": 0}},
            True,
        ),
        (
            {
                "account_flags": {"disallowIncomingNFTokenOffer": False},
                "account_data": {"Flags": 0},
            },
            False,
        ),
        ({"account_data": {"Flags": 0x04000000}}, True),
        ({"account_data": {"Flags": 0x00800000}}, False),
        ({}, None),
    ],
)
def test_disallows_incoming_nft_offers_three_way_contract(monkeypatch, result, expected):
    class Resp:
        def __init__(self, r):
            self.result = r

        def is_successful(self):
            return True

    class Client:
        def __init__(self, *a, **k):
            pass

        async def request(self, req):
            return Resp(result)

    monkeypatch.setattr(server.xrpl_ops, "AsyncJsonRpcClient", Client)
    assert _run(server.xrpl_ops.disallows_incoming_nft_offers("rX")) is expected


def test_readiness_audit_tolerates_undeliverable_minted_claims(_service_env, monkeypatch):
    # A 'minted' claim parked because its destination is unfunded / blocks
    # incoming NFT offers is the user's to fix, not a campaign blocker: the
    # audit reports it separately and still passes the incomplete-claims check.
    audit = importlib.import_module("scripts.audit_sponsored_mint_readiness")
    sponsored_mint.start_campaign(_service_env.app_db, network="mainnet", actor="test", now=100)
    claim = _minted_claim_awaiting_offer(_service_env, "rPARKED", "parked", "NFT-PARKED")
    with sqlite3.connect(_service_env.app_db) as conn:
        conn.execute(
            "UPDATE free_mint_claims SET last_error = ? WHERE id = ?",
            (sponsored_mint.UNDELIVERABLE_OFFER_BLOCKED, claim.id),
        )
        conn.commit()
    sponsored_mint.stop_campaign(_service_env.app_db, network="mainnet", actor="test", now=200)
    hconn = history_store.init_history_db(_service_env.history_db)
    history_store.insert_tx(
        hconn,
        tx_hash="TX-FRESH",
        ledger_index=history_store.EARLIEST_AVAILABLE_LEDGER + 123,
        close_time=3990,
        tx_type="Payment",
        account="rUNIQUE",
        source_tag=sponsored_mint.config.SOURCE_TAG,
        raw_json="{}",
    )
    hconn.commit()
    hconn.close()
    ready_history(_service_env.history_db, network="mainnet", now=4000, close_time=3990)
    monkeypatch.setattr(audit.config, "SPONSORED_MINT_EXCLUDED_WALLETS", _PLACEHOLDER_EXCLUSIONS)

    report = _run(
        audit.build_report(
            network="mainnet",
            app_db=_service_env.app_db,
            history_db=_service_env.history_db,
            now=4000,
            balance_fetch=lambda: asyncio.sleep(0, result=Decimal("1000")),
        )
    )

    assert report["checks"]["incomplete_claims"]["ok"] is True
    assert report["checks"]["incomplete_claims"]["minted"] == 0
    assert report["checks"]["incomplete_claims"]["minted_undeliverable"] == 1
