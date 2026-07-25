import asyncio
import sqlite3
from typing import get_args

import pytest

from lfg_core import config, memos, sponsored_burn, sponsored_mint, xrpl_ops
from tests.sponsored_helpers import prepare_and_forward, ready_history


def run(coro):
    return asyncio.run(coro)


class Response:
    def __init__(self, result):
        self.result = result


def obligation(
    tmp_path,
    monkeypatch,
    *,
    wallet="rReviewWallet",
    source="rSnapSource",
    issuer=None,
    network="mainnet",
):
    monkeypatch.setattr(config, "XRPL_NETWORK", network)
    monkeypatch.setattr(config, "MINT_PRICE_LFGO", "7.25")
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", source)
    if issuer is not None:
        monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", issuer)
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    if not (tmp_path / "history.db").exists():
        ready_history(history, network=network)
        sponsored_mint.start_campaign(db, network=network, actor="42", now=100)
    result = sponsored_mint.reserve_if_eligible(
        db, history, network=network, wallet=wallet, session_id=wallet, now=101
    )
    assert result.claim is not None
    prepare_and_forward(
        sponsored_mint,
        db,
        network=network,
        wallet=wallet,
        session_id=wallet,
        tx_hash=f"MINT-{wallet}",
        now=102,
    )
    sponsored_mint.record_minted_and_enqueue_burn(
        db,
        network=network,
        wallet=wallet,
        session_id=wallet,
        mint_tx_hash=f"MINT-{wallet}",
        nft_id=f"NFT-{wallet}",
        now=103,
    )
    return db, result.claim.id


def burn_row(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM free_mint_burns").fetchone()
    assert row is not None
    return row


def history_entry(memo_id, *, delivered="7.25", tx_hash="MATCH"):
    return {
        "validated": True,
        "hash": tx_hash,
        "tx_json": {
            "TransactionType": "Payment",
            "Account": "rSnapSource",
            "Destination": "rLfgoIssuer",
            "Amount": {
                "currency": "LFGOHEX",
                "issuer": "rLfgoIssuer",
                "value": "7.25",
            },
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
                "value": delivered,
            },
        },
    }


@pytest.mark.parametrize(
    "bad_entry",
    [
        "not-a-dict",
        {"validated": False, "tx_json": {}, "meta": {}},
        {"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}},
        {"validated": True, "tx_json": {"TransactionType": "Payment"}},
        {
            "validated": True,
            "tx_json": {"TransactionType": "Payment"},
            "meta": "not-a-dict",
        },
    ],
)
def test_reconciliation_malformed_or_unvalidated_entry_is_incomplete(monkeypatch, bad_entry):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response({"transactions": [bad_entry]}),
    )

    result = run(xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource"))

    assert result.complete is False
    assert result.tx_hash is None


def test_reconciliation_malformed_candidate_memos_and_marker_are_incomplete(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    malformed = history_entry(memo_id)
    malformed["tx_json"]["Memos"] = [{"Memo": {"MemoType": "not-hex"}}]
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response({"transactions": [malformed]}),
    )
    assert (
        run(
            xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource")
        ).complete
        is False
    )

    calls = []

    def bad_marker(self, req):
        calls.append(req.marker)
        return Response({"transactions": [], "marker": []})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", bad_marker)
    result = run(xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource"))
    assert result.complete is False
    assert calls == [None]


def test_reconciliation_rejects_partial_delivered_amount(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    partial = history_entry(memo_id, delivered="1.25")
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response({"transactions": [partial]}),
    )

    result = run(xrpl_ops.find_sponsored_burn(memo_id, amount="7.25", source_account="rSnapSource"))

    assert result == xrpl_ops.BurnReconciliation(True, None, None)


class Signed:
    def blob(self):
        return "ABCD"

    def get_hash(self):
        return "SIGNEDHASH"


@pytest.mark.parametrize(
    "result",
    [
        {"validated": True},
        {"validated": True, "meta": {}},
        {"validated": True, "meta": "not-a-dict"},
        "not-a-dict",
    ],
)
def test_forwarded_response_without_explicit_validated_engine_result_is_indeterminate(
    monkeypatch, result
):
    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", lambda tx, client, wallet: Signed())
    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", lambda blob: Signed())
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", lambda *args, **kwargs: Response(result))

    outcome = run(
        xrpl_ops.submit_sponsored_burn(
            "fm-a2345678901234567890123456",
            signed_tx_hash="SIGNEDHASH",
            signed_tx_blob="ABCD",
        )
    )

    assert outcome.state == "indeterminate"
    assert outcome.tx_hash == "SIGNEDHASH"


def test_only_explicit_validated_engine_failure_is_deterministic(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", lambda tx, client, wallet: Signed())
    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", lambda blob: Signed())
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

    outcome = run(
        xrpl_ops.submit_sponsored_burn(
            "fm-a2345678901234567890123456",
            signed_tx_hash="SIGNEDHASH",
            signed_tx_blob="ABCD",
        )
    )

    assert outcome == xrpl_ops.BurnSubmission("failed", "SIGNEDHASH", "tecUNFUNDED_PAYMENT")


def test_prepared_transaction_is_persisted_before_submit_and_overlap_reuses_identity(
    tmp_path, monkeypatch
):
    db, _ = obligation(tmp_path, monkeypatch)
    calls = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def prepare(*args, **kwargs):
        return xrpl_ops.BurnPreparation("prepared", "PERSISTEDHASH", "PERSISTEDBLOB", None, 100)

    async def submit(*args, **kwargs):
        calls.append((kwargs["signed_tx_hash"], kwargs["signed_tx_blob"]))
        first_entered.set()
        await release_first.wait()
        return xrpl_ops.BurnSubmission("validated", "PERSISTEDHASH", None)

    reconciliations = []

    async def reconcile(*args, **kwargs):
        assert kwargs["signed_tx_hash"] == "PERSISTEDHASH"
        reconciliations.append(kwargs["signed_tx_hash"])
        if len(reconciliations) > 1:
            return xrpl_ops.BurnReconciliation(True, "PERSISTEDHASH", None)
        return xrpl_ops.BurnReconciliation(True, None, None)

    async def still_live(tx_blob):
        assert tx_blob == "PERSISTEDBLOB"
        return None

    async def scenario():
        first = asyncio.create_task(
            sponsored_burn.process_one(
                db,
                prepare=prepare,
                submit=submit,
                reconcile=reconcile,
                now=200,
            )
        )
        await first_entered.wait()
        persisted = burn_row(db)
        assert persisted["signed_tx_hash"] == "PERSISTEDHASH"
        assert persisted["signed_tx_blob"] == "PERSISTEDBLOB"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE free_mint_burns SET lease_until = 200 WHERE id = ?",
                (persisted["id"],),
            )
        assert await sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=submit,
            reconcile=reconcile,
            identity_expired=still_live,
            now=201,
        )
        retained = burn_row(db)
        assert retained["status"] == "indeterminate"
        assert retained["signed_tx_hash"] == "PERSISTEDHASH"
        assert retained["signed_tx_blob"] == "PERSISTEDBLOB"
        release_first.set()
        await first
        assert await sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=submit,
            reconcile=reconcile,
            identity_expired=still_live,
            now=206,
        )

    run(scenario())

    assert calls == [("PERSISTEDHASH", "PERSISTEDBLOB")]
    assert reconciliations == ["PERSISTEDHASH", "PERSISTEDHASH"]
    assert burn_row(db)["status"] == "burned"


def test_self_issuer_obligation_is_explicitly_fulfilled_without_submission(tmp_path, monkeypatch):
    db, _ = obligation(
        tmp_path,
        monkeypatch,
        source="rIssuer",
        issuer="rIssuer",
        network="testnet",
    )
    submitted = False

    async def submit(*args, **kwargs):
        nonlocal submitted
        submitted = True
        raise AssertionError("self-issuer no-op must not submit")

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=xrpl_ops.prepare_sponsored_burn,
            network="testnet",
            submit=submit,
            reconcile=None,
            now=200,
        )
    )

    row = burn_row(db)
    assert submitted is False
    assert row["status"] == "burned"
    assert row["fulfillment"] == "self_issuer_noop"
    status = sponsored_mint.campaign_status(
        db,
        str(tmp_path / "history.db"),
        network="testnet",
        now=201,
    )
    assert status.burn_noop == 1


def test_untouched_legacy_memo_migrates_but_attempted_row_does_not(tmp_path, monkeypatch):
    db, claim_id = obligation(tmp_path, monkeypatch)
    old_memo = f"fm-{claim_id[:16]}"
    expected = sponsored_mint.sponsored_burn_memo_id(claim_id)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE free_mint_burns SET memo_id = ?", (old_memo,))

    sponsored_mint.ensure_schema(db)

    assert burn_row(db)["memo_id"] == expected

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE free_mint_burns SET memo_id = ?, attempt_count = 1, last_attempt_at = 200",
            (old_memo,),
        )
    sponsored_mint.ensure_schema(db)
    assert burn_row(db)["memo_id"] == old_memo


def test_retry_backoff_is_based_on_failure_completion_time(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    clock = {"now": 200}

    async def prepare(*args, **kwargs):
        return xrpl_ops.BurnPreparation("prepared", "CLOCKHASH", "CLOCKBLOB", None, 100)

    async def slow_failure(*args, **kwargs):
        clock["now"] = 230
        return xrpl_ops.BurnSubmission("failed", "CLOCKHASH", "tecUNFUNDED_PAYMENT")

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=slow_failure,
            reconcile=None,
            now=200,
            clock=lambda: clock["now"],
        )
    )

    assert burn_row(db)["next_attempt_at"] == 235


def test_unrelated_native_xrp_payment_does_not_block_later_exact_match(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    unrelated = history_entry(memo_id, tx_hash="XRP")
    unrelated["tx_json"]["Amount"] = "1000000"
    unrelated["meta"]["delivered_amount"] = "1000000"
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response(
            {"transactions": [unrelated, history_entry(memo_id, tx_hash="LATER")]}
        ),
    )

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
        )
    )

    assert result == xrpl_ops.BurnReconciliation(True, "LATER", None)


def test_malformed_entry_does_not_block_later_exact_match(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    malformed = {"validated": True, "tx_json": {"TransactionType": "Payment"}, "meta": None}
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response(
            {"transactions": [malformed, history_entry(memo_id, tx_hash="LATER")]}
        ),
    )

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
        )
    )

    assert result == xrpl_ops.BurnReconciliation(True, "LATER", None)


def test_malformed_only_history_scans_to_exhaustion_then_stays_incomplete(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    calls = []

    def paged(self, req):
        calls.append(req.marker)
        if req.marker is None:
            return Response({"transactions": ["malformed"], "marker": "next"})
        return Response({"transactions": []})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", paged)

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
        )
    )

    assert result.complete is False
    assert result.tx_hash is None
    assert calls == [None, "next"]


def test_validated_failure_retires_identity_before_next_preparation(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    preparations = []
    submissions = []

    async def prepare(*args, **kwargs):
        number = len(preparations) + 1
        identity = (f"HASH-{number}", f"BLOB-{number}")
        preparations.append(identity)
        return xrpl_ops.BurnPreparation("prepared", *identity, None, 100)

    async def submit(*args, **kwargs):
        identity = (kwargs["signed_tx_hash"], kwargs["signed_tx_blob"])
        submissions.append(identity)
        if len(submissions) == 1:
            return xrpl_ops.BurnSubmission("failed", identity[0], "tecUNFUNDED_PAYMENT")
        return xrpl_ops.BurnSubmission("validated", identity[0], None)

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=submit,
            reconcile=None,
            now=200,
        )
    )
    failed = burn_row(db)
    assert failed["status"] == "pending"
    assert failed["signed_tx_hash"] is None
    assert failed["signed_tx_blob"] is None

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=submit,
            reconcile=None,
            now=205,
        )
    )

    assert preparations == [("HASH-1", "BLOB-1"), ("HASH-2", "BLOB-2")]
    assert submissions == preparations
    assert burn_row(db)["status"] == "burned"


def _set_indeterminate_identity(
    db, *, tx_hash="OLD-HASH", tx_blob="OLD-BLOB", signed_ledger_floor=100
):
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE free_mint_burns
            SET status = 'indeterminate', signed_tx_hash = ?, signed_tx_blob = ?,
                signed_ledger_floor = ?
            """,
            (tx_hash, tx_blob, signed_ledger_floor),
        )


def test_complete_absence_retains_not_yet_expired_identity(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    _set_indeterminate_identity(db)
    checked = []

    async def absent(*args, **kwargs):
        return xrpl_ops.BurnReconciliation(True, None, None)

    async def still_live(tx_blob):
        checked.append(tx_blob)
        return None

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=None,
            submit=None,
            reconcile=absent,
            identity_expired=still_live,
            now=200,
        )
    )

    row = burn_row(db)
    assert checked == ["OLD-BLOB"]
    assert row["status"] == "indeterminate"
    assert row["signed_tx_hash"] == "OLD-HASH"
    assert row["signed_tx_blob"] == "OLD-BLOB"


def test_incomplete_history_never_checks_expiry_or_rotates_identity(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    _set_indeterminate_identity(db)

    async def incomplete(*args, **kwargs):
        return xrpl_ops.BurnReconciliation(False, None, "RPC unavailable")

    async def must_not_check(tx_blob):
        raise AssertionError("incomplete history cannot authorize identity expiry")

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=None,
            submit=None,
            reconcile=incomplete,
            identity_expired=must_not_check,
            now=200,
        )
    )

    row = burn_row(db)
    assert row["status"] == "indeterminate"
    assert row["signed_tx_hash"] == "OLD-HASH"
    assert row["signed_tx_blob"] == "OLD-BLOB"


def test_expired_absent_identity_is_retired_then_reprepared(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    _set_indeterminate_identity(db)
    submitted = []

    async def absent(*args, **kwargs):
        return xrpl_ops.BurnReconciliation(True, None, None)

    async def expired(tx_blob):
        assert tx_blob == "OLD-BLOB"
        return 500

    async def prepare(*args, **kwargs):
        return xrpl_ops.BurnPreparation("prepared", "NEW-HASH", "NEW-BLOB", None, 501)

    async def submit(*args, **kwargs):
        submitted.append((kwargs["signed_tx_hash"], kwargs["signed_tx_blob"]))
        return xrpl_ops.BurnSubmission("validated", "NEW-HASH", None)

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=submit,
            reconcile=absent,
            identity_expired=expired,
            now=200,
        )
    )
    retired = burn_row(db)
    assert retired["status"] == "pending"
    assert retired["signed_tx_hash"] is None
    assert retired["signed_tx_blob"] is None
    assert retired["signed_ledger_floor"] is None

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=submit,
            reconcile=absent,
            identity_expired=expired,
            now=201,
        )
    )
    assert submitted == [("NEW-HASH", "NEW-BLOB")]
    assert burn_row(db)["status"] == "burned"


def test_post_expiry_scan_catches_transaction_that_landed_after_initial_absence(
    tmp_path, monkeypatch
):
    db, _ = obligation(tmp_path, monkeypatch)
    _set_indeterminate_identity(db)
    events = []
    landed = False
    preparations = []

    async def reconcile(*args, **kwargs):
        events.append("scan")
        if landed:
            return xrpl_ops.BurnReconciliation(True, "OLD-HASH", None)
        return xrpl_ops.BurnReconciliation(True, None, None)

    async def expired(tx_blob):
        nonlocal landed
        events.append("expiry")
        landed = True
        return 500

    async def prepare(*args, **kwargs):
        preparations.append(True)
        return xrpl_ops.BurnPreparation("prepared", "NEW-HASH", "NEW-BLOB", None, 501)

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=prepare,
            submit=None,
            reconcile=reconcile,
            identity_expired=expired,
            now=200,
        )
    )

    row = burn_row(db)
    assert events == ["scan", "expiry", "scan"]
    assert preparations == []
    assert row["status"] == "burned"
    assert row["tx_hash"] == "OLD-HASH"
    assert row["signed_tx_hash"] == "OLD-HASH"
    assert row["signed_tx_blob"] == "OLD-BLOB"


def test_incomplete_post_expiry_scan_retains_signed_identity(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    _set_indeterminate_identity(db)
    events = []
    scan_count = 0

    async def reconcile(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        events.append("scan")
        if scan_count == 1:
            return xrpl_ops.BurnReconciliation(True, None, None)
        return xrpl_ops.BurnReconciliation(False, None, "post-expiry RPC failure")

    async def expired(tx_blob):
        events.append("expiry")
        return 500

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=None,
            submit=None,
            reconcile=reconcile,
            identity_expired=expired,
            now=200,
        )
    )

    row = burn_row(db)
    assert events == ["scan", "expiry", "scan"]
    assert row["status"] == "indeterminate"
    assert row["signed_tx_hash"] == "OLD-HASH"
    assert row["signed_tx_blob"] == "OLD-BLOB"
    assert row["last_error"] == "post-expiry RPC failure"


def test_expiry_requires_decoded_last_ledger_sequence_below_validated_ledger(monkeypatch):
    class Prepared:
        last_ledger_sequence = 500

    ledger = {"ledger_index": 501}
    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", lambda blob: Prepared())
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response(ledger),
    )

    assert run(xrpl_ops.sponsored_burn_identity_expired("BLOB")) == 500
    ledger["ledger_index"] = 500
    assert run(xrpl_ops.sponsored_burn_identity_expired("BLOB")) is None
    ledger["ledger_index"] = 501
    ledger["validated"] = False
    assert run(xrpl_ops.sponsored_burn_identity_expired("BLOB")) is None


def test_burn_submission_public_states_are_exactly_three():
    states = set(get_args(xrpl_ops.BurnSubmission.__annotations__["state"]))

    assert states == {"validated", "failed", "indeterminate"}


@pytest.mark.parametrize(
    "range_metadata",
    [
        {"validated": False, "ledger_index_min": 100, "ledger_index_max": 500},
        {"validated": True},
        {"validated": True, "ledger_index_min": "100", "ledger_index_max": 500},
        {"validated": True, "ledger_index_min": 100, "ledger_index_max": 499},
        {"validated": True, "ledger_index_min": 101, "ledger_index_max": 500},
    ],
    ids=["unvalidated", "missing-range", "malformed-range", "lagging-max", "pruned-min"],
)
def test_bounded_reconciliation_requires_proof_of_the_full_validated_range(
    monkeypatch, range_metadata
):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")

    def request(self, req):
        assert req.ledger_index_min == 100
        assert req.ledger_index_max == 500
        return Response(
            {
                "account": "rSnapSource",
                "transactions": [],
                **range_metadata,
            }
        )

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request)

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
            required_ledger_min=100,
            required_ledger_max=500,
        )
    )

    assert result.complete is False
    assert result.tx_hash is None


def test_bounded_reconciliation_accepts_a_validated_covering_range(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response(
            {
                "account": "rSnapSource",
                "validated": True,
                "ledger_index_min": 90,
                "ledger_index_max": 510,
                "transactions": [],
            }
        ),
    )

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
            required_ledger_min=100,
            required_ledger_max=500,
        )
    )

    assert result == xrpl_ops.BurnReconciliation(True, None, None)


@pytest.mark.parametrize(
    "account_metadata",
    [
        {},
        {"account": 123},
        {"account": "rWrongSource"},
    ],
    ids=["missing", "non-string", "wrong"],
)
def test_bounded_reconciliation_requires_exact_response_account(monkeypatch, account_metadata):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: Response(
            {
                "validated": True,
                "ledger_index_min": 90,
                "ledger_index_max": 510,
                "transactions": [],
                **account_metadata,
            }
        ),
    )

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
            required_ledger_min=100,
            required_ledger_max=500,
        )
    )

    assert result.complete is False
    assert result.tx_hash is None


def test_bounded_reconciliation_validates_every_page_range(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    calls = []

    def request(self, req):
        calls.append(req.marker)
        if req.marker is None:
            return Response(
                {
                    "account": "rSnapSource",
                    "validated": True,
                    "ledger_index_min": 90,
                    "ledger_index_max": 510,
                    "transactions": [],
                    "marker": "next",
                }
            )
        return Response(
            {
                "account": "rSnapSource",
                "validated": True,
                "ledger_index_min": 101,
                "ledger_index_max": 510,
                "transactions": [],
            }
        )

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request)

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
            required_ledger_min=100,
            required_ledger_max=500,
        )
    )

    assert result.complete is False
    assert calls == [None, "next"]


def test_bounded_reconciliation_validates_account_on_every_page(monkeypatch):
    memo_id = "fm-a2345678901234567890123456"
    monkeypatch.setattr(config, "TOKEN_ISSUER_ADDRESS", "rLfgoIssuer")
    monkeypatch.setattr(config, "TOKEN_CURRENCY_HEX", "LFGOHEX")
    calls = []

    def request(self, req):
        calls.append(req.marker)
        account = "rSnapSource" if req.marker is None else "rWrongSource"
        result = {
            "account": account,
            "validated": True,
            "ledger_index_min": 90,
            "ledger_index_max": 510,
            "transactions": [],
        }
        if req.marker is None:
            result["marker"] = "next"
        return Response(result)

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request)

    result = run(
        xrpl_ops.find_sponsored_burn(
            memo_id,
            amount="7.25",
            source_account="rSnapSource",
            required_ledger_min=100,
            required_ledger_max=500,
        )
    )

    assert result.complete is False
    assert calls == [None, "next"]


def test_preparation_observes_validated_ledger_floor_before_signing(monkeypatch):
    events = []

    def request(self, req):
        events.append("floor")
        return Response({"validated": True, "ledger_index": 321})

    def sign(tx, client, wallet):
        events.append("sign")
        return Signed()

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request)
    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", sign)

    result = run(xrpl_ops.prepare_sponsored_burn("fm-a2345678901234567890123456"))

    assert result.state == "prepared"
    assert result.signed_ledger_floor == 321
    assert events == ["floor", "sign"]


def test_prepared_identity_persists_floor_before_submission(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    submit_entered = asyncio.Event()
    release_submit = asyncio.Event()

    async def prepare(*args, **kwargs):
        return xrpl_ops.BurnPreparation(
            "prepared",
            "FLOOR-HASH",
            "FLOOR-BLOB",
            None,
            signed_ledger_floor=321,
        )

    async def submit(*args, **kwargs):
        submit_entered.set()
        await release_submit.wait()
        return xrpl_ops.BurnSubmission("validated", "FLOOR-HASH", None)

    async def scenario():
        worker = asyncio.create_task(
            sponsored_burn.process_one(
                db,
                prepare=prepare,
                submit=submit,
                reconcile=None,
                now=200,
            )
        )
        await submit_entered.wait()
        assert burn_row(db)["signed_ledger_floor"] == 321
        release_submit.set()
        await worker

    run(scenario())


def test_legacy_identity_without_floor_never_rotates_on_absence(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE free_mint_burns
            SET status = 'indeterminate', signed_tx_hash = 'OLD-HASH',
                signed_tx_blob = 'OLD-BLOB', signed_ledger_floor = NULL
            """
        )

    async def absent(*args, **kwargs):
        return xrpl_ops.BurnReconciliation(True, None, None)

    async def must_not_check(tx_blob):
        raise AssertionError("an identity without a floor cannot prove an absence interval")

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=None,
            submit=None,
            reconcile=absent,
            identity_expired=must_not_check,
            now=200,
        )
    )

    row = burn_row(db)
    assert row["status"] == "indeterminate"
    assert row["signed_tx_hash"] == "OLD-HASH"
    assert row["signed_tx_blob"] == "OLD-BLOB"
    assert row["signed_ledger_floor"] is None


def test_pruned_post_expiry_history_cannot_authorize_a_second_burn(tmp_path, monkeypatch):
    db, _ = obligation(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE free_mint_burns
            SET status = 'indeterminate', signed_tx_hash = 'OLD-HASH',
                signed_tx_blob = 'OLD-BLOB', signed_ledger_floor = 100
            """
        )
    history_requests = []

    def pruned_history(self, req):
        history_requests.append((req.ledger_index_min, req.ledger_index_max))
        # A burn at ledger 150 is no longer returned: this server only retains 200+.
        return Response(
            {
                "account": "rSnapSource",
                "validated": True,
                "ledger_index_min": 200,
                "ledger_index_max": 500,
                "transactions": [],
            }
        )

    async def expired(tx_blob):
        return 500

    async def must_not_prepare(*args, **kwargs):
        raise AssertionError("pruned history must not authorize a replacement identity")

    async def must_not_submit(*args, **kwargs):
        raise AssertionError("pruned history must not permit a possible double burn")

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", pruned_history)

    assert run(
        sponsored_burn.process_one(
            db,
            prepare=must_not_prepare,
            submit=must_not_submit,
            reconcile=xrpl_ops.find_sponsored_burn,
            identity_expired=expired,
            now=200,
        )
    )

    row = burn_row(db)
    assert history_requests == [(-1, -1), (100, 500)]
    assert row["status"] == "indeterminate"
    assert row["signed_tx_hash"] == "OLD-HASH"
    assert row["signed_ledger_floor"] == 100
