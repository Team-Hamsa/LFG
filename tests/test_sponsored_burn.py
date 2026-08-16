import asyncio
import base64
import sqlite3

import pytest

from lfg_core import config, memos, sponsored_burn, sponsored_mint, xrpl_ops
from tests.sponsored_helpers import prepare_and_forward, ready_history


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def obligation(tmp_path, monkeypatch, wallet="rBurnWallet"):
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    new_archive = not (tmp_path / "history.db").exists()
    ready_history(history, network="mainnet")
    if new_archive:
        sponsored_mint.start_campaign(db, network="mainnet", actor="42", now=100)
    result = sponsored_mint.reserve_if_eligible(
        db, history, network="mainnet", wallet=wallet, session_id=wallet, now=101
    )
    prepare_and_forward(
        sponsored_mint,
        db,
        network="mainnet",
        wallet=wallet,
        session_id=wallet,
        tx_hash=f"MINT-{wallet}",
        now=102,
    )
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "7.25")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rSnapSource")
    sponsored_mint.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet=wallet,
        session_id=wallet,
        mint_tx_hash=f"MINT-{wallet}",
        nft_id=f"NFT-{wallet}",
        now=103,
    )
    assert result.claim is not None
    return db, result.claim.id


def preparation(tx_hash):
    async def prepare(*args, **kwargs):
        return xrpl_ops.BurnPreparation("prepared", tx_hash, f"BLOB-{tx_hash}", None, 100)

    return prepare


def rows(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM free_mint_burns ORDER BY id").fetchall()


def test_memo_encodes_full_claim_uuid_repeat_callback_is_idempotent_and_memo_is_unique(
    tmp_path, monkeypatch
):
    db, claim_id = obligation(tmp_path, monkeypatch)
    row = rows(db)[0]
    encoded = row["memo_id"].removeprefix("fm-")
    decoded = base64.b32decode(encoded.upper() + "=" * ((8 - len(encoded) % 8) % 8)).hex()
    assert decoded == claim_id
    assert len(row["memo_id"]) <= 32
    sponsored_mint.record_minted_and_enqueue_burn(
        db,
        network="mainnet",
        wallet="rBurnWallet",
        session_id="rBurnWallet",
        mint_tx_hash="MINT-rBurnWallet",
        nft_id="NFT-rBurnWallet",
        now=104,
    )
    obligation(tmp_path, monkeypatch, "rSecond")
    burns = rows(db)
    assert len(burns) == 2
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE free_mint_burns SET memo_id = ? WHERE id = ?",
            (burns[0]["memo_id"], burns[1]["id"]),
        )


def test_scope_drift_and_submit_outcomes_and_snapshot_lifecycle(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "99")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", "rChanged")
    calls = []

    async def success(memo_id, *, amount, source_account):
        calls.append((memo_id, amount, source_account))
        return xrpl_ops.BurnSubmission("validated", "HASH", None)

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=preparation("HASH"),
            submit=success,
            reconcile=None,
            now=200,
        )
    )
    row = rows(db)[0]
    assert calls == []
    assert (row["status"], row["tx_hash"], row["attempt_count"]) == ("failed_terminal", None, 1)
    assert "source account" in row["last_error"]

    db2, _ = obligation(tmp_path / "failed", monkeypatch)

    async def failed(*args, **kwargs):
        return xrpl_ops.BurnSubmission("failed", None, "tecUNFUNDED_PAYMENT")

    assert run(
        sponsored_burn.process_one(
            db2,
            prepare=preparation("FAILED"),
            submit=failed,
            reconcile=None,
            now=200,
        )
    )
    row = rows(db2)[0]
    assert row["status"] == "pending"
    assert 200 < row["next_attempt_at"] <= 200 + sponsored_burn.MAX_BACKOFF_SECONDS
    assert row["lease_until"] is None

    db3, _ = obligation(tmp_path / "unknown", monkeypatch)

    async def unknown(*args, **kwargs):
        return xrpl_ops.BurnSubmission("indeterminate", "UNKNOWN", "timeout after submit")

    assert run(
        sponsored_burn.process_one(
            db3,
            prepare=preparation("UNKNOWN"),
            submit=unknown,
            reconcile=None,
            now=200,
        )
    )
    assert rows(db3)[0]["status"] == "indeterminate"


def test_reconciliation_found_absent_and_failure(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE free_mint_burns SET status = 'indeterminate'")
    submitted = []

    async def submit(*args, **kwargs):
        submitted.append(args)
        return xrpl_ops.BurnSubmission("validated", "RETRY", None)

    async def found(*args, **kwargs):
        assert kwargs == {
            "amount": "7.25",
            "source_account": "rSnapSource",
            "signed_tx_hash": None,
            "network": "mainnet",
            "issuer": config.TOKEN_ISSUER_ADDRESS,
            "currency": config.TOKEN_CURRENCY_HEX,
            "source_tag": config.SOURCE_TAG,
        }
        return xrpl_ops.BurnReconciliation(True, "FOUND", None)

    assert run(sponsored_burn.process_one(db, submit=submit, reconcile=found, now=200))
    assert not submitted
    assert (rows(db)[0]["status"], rows(db)[0]["tx_hash"]) == ("burned", "FOUND")

    db2, _ = obligation(tmp_path / "absent", monkeypatch)
    with sqlite3.connect(db2) as conn:
        conn.execute("UPDATE free_mint_burns SET status = 'indeterminate'")

    async def absent(*args, **kwargs):
        return xrpl_ops.BurnReconciliation(True, None, None)

    assert run(sponsored_burn.process_one(db2, submit=submit, reconcile=absent, now=200))
    assert rows(db2)[0]["status"] == "pending"
    assert not submitted
    assert run(
        sponsored_burn.process_one(
            db2, prepare=preparation("RETRY"), submit=submit, reconcile=absent, now=201
        )
    )
    assert rows(db2)[0]["status"] == "burned"

    db3, _ = obligation(tmp_path / "rpc", monkeypatch)
    with sqlite3.connect(db3) as conn:
        conn.execute("UPDATE free_mint_burns SET status = 'indeterminate'")

    async def incomplete(*args, **kwargs):
        return xrpl_ops.BurnReconciliation(False, None, "RPC unavailable")

    assert run(sponsored_burn.process_one(db3, submit=submit, reconcile=incomplete, now=200))
    row = rows(db3)[0]
    assert row["status"] == "indeterminate"
    assert row["next_attempt_at"] > 200


def test_stale_lease_reconciles_and_concurrent_workers_lease_once(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE free_mint_burns SET status='submitting', lease_until=199, attempt_count=1"
        )

    async def no_submit(*args, **kwargs):
        raise AssertionError("never blindly resubmit a crashed lease")

    async def found(*args, **kwargs):
        return xrpl_ops.BurnReconciliation(True, "CRASHED", None)

    assert run(sponsored_burn.process_one(db, submit=no_submit, reconcile=found, now=200))
    assert rows(db)[0]["tx_hash"] == "CRASHED"

    db2, _ = obligation(tmp_path / "race", monkeypatch)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        count = 0

        async def submit(*args, **kwargs):
            nonlocal count
            count += 1
            entered.set()
            await release.wait()
            return xrpl_ops.BurnSubmission("validated", "ONLY", None)

        first = asyncio.create_task(
            sponsored_burn.process_one(
                db2,
                prepare=preparation("ONLY"),
                submit=submit,
                reconcile=None,
                now=200,
            )
        )
        await entered.wait()
        second = await sponsored_burn.process_one(db2, submit=submit, reconcile=None, now=200)
        release.set()
        return await first, second, count

    assert run(scenario()) == (True, False, 1)


class Response:
    def __init__(self, result):
        self.result = result


def entry(memo_id, tx_hash="MATCH"):
    return {
        "validated": True,
        "hash": tx_hash,
        "tx_json": {
            "TransactionType": "Payment",
            "Account": "rSnapSource",
            "Destination": "rLfgoIssuer",
            "Amount": {"currency": "LFGOHEX", "issuer": "rLfgoIssuer", "value": "7.25"},
            "SourceTag": config.SOURCE_TAG,
            "Memos": memos.build_memos_json(
                memos.INITIATOR_BACKEND,
                memos.PLATFORM_BACKEND,
                memos.ACTION_SPONSORED_MINT_BURN,
                memo_id,
            ),
        },
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": {
                "currency": "LFGOHEX",
                "issuer": "rLfgoIssuer",
                "value": "7.25",
            },
        },
    }


def test_history_reconciliation_proves_exact_semantics_and_rpc_failure_is_incomplete(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    bad = []
    for key, value in (
        ("Account", "rOther"),
        ("Destination", "rOther"),
        ("SourceTag", config.SOURCE_TAG + 1),
        ("TransactionType", "TrustSet"),
    ):
        candidate = entry(memo_id, f"BAD-{key}")
        candidate["tx_json"][key] = value
        bad.append(candidate)
    candidate = entry(memo_id, "BAD-AMOUNT")
    candidate["tx_json"]["Amount"]["value"] = "7.2501"
    bad.append(candidate)
    candidate = entry(memo_id, "BAD-ENGINE")
    candidate["meta"]["TransactionResult"] = "tecUNFUNDED_PAYMENT"
    bad.append(candidate)
    for initiator, action, campaign, label in (
        (
            memos.INITIATOR_USER,
            memos.ACTION_SPONSORED_MINT_BURN,
            memo_id,
            "INITIATOR",
        ),
        (memos.INITIATOR_BACKEND, memos.ACTION_BUY_AND_BURN, memo_id, "ACTION"),
        (
            memos.INITIATOR_BACKEND,
            memos.ACTION_SPONSORED_MINT_BURN,
            "fm-z2345678901234567890123456",
            "CAMPAIGN",
        ),
    ):
        candidate = entry(memo_id, f"BAD-{label}")
        candidate["tx_json"]["Memos"] = memos.build_memos_json(
            initiator, memos.PLATFORM_BACKEND, action, campaign
        )
        bad.append(candidate)

    def request(self, req):
        assert req.account == "rSnapSource"
        return Response({"transactions": [*bad, entry(memo_id)]})

    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request)
    result = run(xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource"))
    assert result == xrpl_ops.BurnReconciliation(True, "MATCH", None)

    def timeout(self, req):
        raise TimeoutError("account_tx timed out")

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", timeout)
    result = run(xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource"))
    assert not result.complete
    assert result.tx_hash is None


class Signed:
    def blob(self):
        return "ABCD"

    def get_hash(self):
        return "SIGNEDHASH"


def test_submit_sponsored_burn_classifies_validated_failure_and_uncertainty(monkeypatch):
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response({"validated": True, "ledger_index": 100}),
    )
    monkeypatch.setattr(
        xrpl_ops,
        "autofill_and_sign",
        lambda tx, client, wallet: Signed(),
    )
    monkeypatch.setattr(
        xrpl_ops,
        "submit_and_wait",
        lambda *args, **kwargs: Response(
            {
                "validated": True,
                "hash": "SIGNEDHASH",
                "meta": {"TransactionResult": "tecUNFUNDED_PAYMENT"},
            }
        ),
    )
    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", lambda blob: Signed())
    failed = run(xrpl_ops.submit_sponsored_burn("fm-a2345678901234567890123456"))
    assert failed == xrpl_ops.BurnSubmission("failed", "SIGNEDHASH", "tecUNFUNDED_PAYMENT")

    def timeout(*args, **kwargs):
        raise TimeoutError("lost connection after submit")

    async def not_found(*args, **kwargs):
        return None

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", timeout)
    monkeypatch.setattr(xrpl_ops, "_confirm_by_hash", not_found)
    unknown = run(xrpl_ops.submit_sponsored_burn("fm-a2345678901234567890123456"))
    assert unknown.state == "indeterminate"
    assert unknown.tx_hash == "SIGNEDHASH"

    def signing_failed(*args, **kwargs):
        raise ValueError("cannot sign")

    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", signing_failed)
    deterministic = run(xrpl_ops.submit_sponsored_burn("fm-a2345678901234567890123456"))
    assert deterministic.state == "failed"


def test_history_reconciliation_pages_to_exhaustion_before_proving_absence(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    calls = []

    def paged_match(self, req):
        calls.append(req.marker)
        if req.marker is None:
            return Response({"transactions": [], "marker": "page-2"})
        return Response({"transactions": [entry(memo_id)]})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", paged_match)
    found = run(xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource"))
    assert found == xrpl_ops.BurnReconciliation(True, "MATCH", None)
    assert calls == [None, "page-2"]

    def paged_absence(self, req):
        if req.marker is None:
            return Response({"transactions": [], "marker": "last"})
        return Response({"transactions": []})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", paged_absence)
    absent = run(xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource"))
    assert absent == xrpl_ops.BurnReconciliation(True, None, None)


def test_task4_database_is_forward_migrated_with_crash_lease_columns(tmp_path):
    db = str(tmp_path / "legacy.db")
    sponsored_mint.ensure_schema(db)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE free_mint_burns DROP COLUMN lease_until")
        conn.execute("ALTER TABLE free_mint_burns DROP COLUMN lease_token")
        conn.execute("ALTER TABLE free_mint_burns DROP COLUMN signed_ledger_floor")

    sponsored_mint.ensure_schema(db)

    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(free_mint_burns)")}
    assert {"lease_until", "lease_token", "signed_ledger_floor"} <= columns


def test_run_worker_survives_idle_poll_timeout(tmp_path, monkeypatch):
    """The idle sleep raises asyncio.TimeoutError, which on Python < 3.11 is
    NOT the builtin TimeoutError — an `except TimeoutError` alone lets the
    worker task die silently on its first idle second (staging 2026-07-31)."""
    calls = 0

    async def idle_pass(*args, **kwargs):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(sponsored_burn, "process_one", idle_pass)
    monkeypatch.setattr(sponsored_burn, "POLL_SECONDS", 0.01)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(sponsored_burn.run_worker(str(tmp_path / "app.db"), stop))
        await asyncio.sleep(0.2)
        alive = not task.done()
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        return alive

    assert run(scenario())
    assert calls > 1


def test_heartbeat_survives_poll_timeout_and_extends_lease(tmp_path, monkeypatch):
    """Same Python 3.10 asyncio.TimeoutError pitfall as run_worker: the lease
    heartbeat must outlive its first poll timeout and keep extending."""
    db, _claim_id = obligation(tmp_path, monkeypatch)
    acquired = sponsored_burn._acquire(db, 200, "mainnet")
    assert acquired is not None
    monkeypatch.setattr(sponsored_burn, "LEASE_SECONDS", 0.03)

    async def scenario():
        stopped = asyncio.Event()
        task = asyncio.create_task(sponsored_burn._heartbeat(db, acquired, stopped))
        await asyncio.sleep(0.2)
        alive = not task.done()
        stopped.set()
        await asyncio.wait_for(task, timeout=2)
        return alive

    assert run(scenario())
    with sqlite3.connect(db) as conn:
        lease_until = conn.execute(
            "SELECT lease_until FROM free_mint_burns WHERE id = ?", (acquired.id,)
        ).fetchone()[0]
    assert lease_until >= int(__import__("time").time()) - 1
