"""BRIX claim payment helper + claim state machine (#48).

Env pins come from the repo-root conftest.py (#323) — no per-file preamble.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from lfg_core import brix_drip, config, xrpl_ops

# The distributor address MUST be the one this seed signs for; a mismatch is
# now refused up front, and a fixture that faked it would test a configuration
# the code rejects.
DISTRIBUTOR_SEED = "sEdTM1uX8pu2do5XvTnutH6HsouMaM2"
DISTRIBUTOR = str(xrpl_ops.Wallet.from_seed(DISTRIBUTOR_SEED).classic_address)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "history_test.db")
    c.row_factory = sqlite3.Row
    brix_drip.ensure_schema(c)
    yield c
    c.close()


def _accrue(conn, wallet, count, epoch_base=10):
    brix_drip.record_accruals(
        conn,
        [
            brix_drip.Accrual(f"2026-08-{epoch_base + i:02d}", f"NFT_{wallet}_{i}", wallet, 1)
            for i in range(count)
        ],
    )


# --- Task 5: send_brix_claim ----------------------------------------------


class _Captured:
    tx = None


@pytest.fixture()
def capture_payment(monkeypatch):
    """Intercept the built Payment without touching the network."""
    captured = _Captured()

    async def fake_submit(tx, wallet, client, label, **kwargs):
        captured.tx = tx
        return {"hash": "TXHASH", "meta": {"TransactionResult": "tesSUCCESS"}}

    async def fake_ledger(client):
        return 1000

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", fake_ledger)
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_SEED", DISTRIBUTOR_SEED, raising=False)
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_ADDRESS", DISTRIBUTOR, raising=False)
    return captured


def test_send_brix_claim_builds_a_tagged_memoed_brix_payment(capture_payment):
    result = asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42))
    tx = capture_payment.tx

    assert tx.destination == "rAlice"
    assert tx.account == DISTRIBUTOR  # built for the account that signs it
    assert tx.amount.currency == config.BRIX_CURRENCY_HEX
    assert tx.amount.issuer == config.BRIX_ISSUER
    assert tx.amount.value == "5"
    # Hackathon-mandatory: the exact assigned tag, asserted as a literal.
    # Comparing against config.SOURCE_TAG would pass for any value the
    # constant happens to hold and could never catch a changed tag.
    assert tx.source_tag == 2606160021
    assert result.state == "confirmed"
    assert result.tx_hash == "TXHASH"


def test_send_brix_claim_memo_identifies_the_claim_on_chain(capture_payment):
    asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42))
    decoded = [bytes.fromhex(m.memo_data).decode() for m in capture_payment.tx.memos]
    assert any(d == "lfg:brix_claim:42" for d in decoded)


def test_send_brix_claim_always_sets_and_returns_last_ledger_sequence(capture_payment):
    """Spec 5.3: LastLedgerSequence is what makes 'definitively failed'
    decidable during recovery, so it must be pinned BEFORE submit and returned
    on every path — including the unknown one."""
    result = asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42))
    assert capture_payment.tx.last_ledger_sequence == result.last_ledger_seq
    assert result.last_ledger_seq > 1000


def test_send_brix_claim_maps_definitive_failure(monkeypatch, capture_payment):
    async def fail(tx, wallet, client, label, **kwargs):
        capture_payment.tx = tx
        return None

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fail)
    result = asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42))
    assert result.state == "failed"
    assert result.tx_hash is None
    assert result.last_ledger_seq is not None


def test_send_brix_claim_maps_indeterminate_to_unknown(monkeypatch, capture_payment):
    async def indeterminate(tx, wallet, client, label, **kwargs):
        capture_payment.tx = tx
        raise xrpl_ops.IndeterminateResultError("timeout after submit")

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", indeterminate)
    result = asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42))
    assert result.state == "unknown"
    assert result.last_ledger_seq is not None


# --- Task 6: claim state machine ------------------------------------------


def test_open_claim_binds_every_unclaimed_row_atomically(conn):
    _accrue(conn, "rAlice", 3)
    claim_id, amount = brix_drip.open_claim(conn, "rAlice")
    assert amount == 3
    assert brix_drip.claimable(conn, "rAlice") == 0
    bound = conn.execute(
        "SELECT COUNT(*) FROM brix_accruals WHERE claim_id = ?", (claim_id,)
    ).fetchone()[0]
    assert bound == 3


def test_open_claim_rejects_an_empty_balance(conn):
    with pytest.raises(brix_drip.NothingToClaim):
        brix_drip.open_claim(conn, "rNobody")


def test_open_claim_rejects_a_second_open_claim(conn):
    _accrue(conn, "rAlice", 2)
    brix_drip.open_claim(conn, "rAlice")
    _accrue(conn, "rAlice", 2, epoch_base=20)
    with pytest.raises(brix_drip.ClaimInFlight):
        brix_drip.open_claim(conn, "rAlice")


def test_settle_claim_confirmed_records_hash_and_keeps_rows_bound(conn):
    _accrue(conn, "rAlice", 2)
    claim_id, _ = brix_drip.open_claim(conn, "rAlice")
    brix_drip.settle_claim(conn, claim_id, "confirmed", tx_hash="HASH")
    row = conn.execute("SELECT * FROM brix_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    assert row["state"] == "confirmed"
    assert row["tx_hash"] == "HASH"
    assert brix_drip.claimable(conn, "rAlice") == 0


def test_settle_claim_failed_unbinds_and_restores_the_balance(conn):
    _accrue(conn, "rAlice", 2)
    claim_id, _ = brix_drip.open_claim(conn, "rAlice")
    brix_drip.settle_claim(conn, claim_id, "failed")
    assert brix_drip.claimable(conn, "rAlice") == 2
    # And the wallet is free to try again.
    brix_drip.open_claim(conn, "rAlice")


def test_settle_claim_unknown_leaves_everything_bound(conn):
    """The ambiguous window: funds can never be double-paid because the rows
    stay bound and the unique index blocks a second open claim."""
    _accrue(conn, "rAlice", 2)
    claim_id, _ = brix_drip.open_claim(conn, "rAlice")
    brix_drip.settle_claim(conn, claim_id, "unknown")
    assert brix_drip.claimable(conn, "rAlice") == 0
    row = conn.execute("SELECT state FROM brix_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    assert row["state"] == "submitted"
    with pytest.raises(brix_drip.ClaimInFlight):
        brix_drip.open_claim(conn, "rAlice")


def test_record_submission_persists_hash_and_last_ledger_seq(conn):
    _accrue(conn, "rAlice", 1)
    claim_id, _ = brix_drip.open_claim(conn, "rAlice")
    brix_drip.record_submission(conn, claim_id, tx_hash="H", last_ledger_seq=4242)
    row = conn.execute("SELECT * FROM brix_claims WHERE claim_id = ?", (claim_id,)).fetchone()
    assert row["state"] == "submitted"
    assert row["tx_hash"] == "H"
    assert row["last_ledger_seq"] == 4242


def test_concurrent_open_claim_lets_exactly_one_wallet_claim_win(tmp_path):
    """Two racing claims must never both bind the same accrual rows."""
    path = tmp_path / "race.db"
    setup = sqlite3.connect(path)
    brix_drip.ensure_schema(setup)
    _accrue(setup, "rAlice", 5)
    setup.close()

    results: list[object] = []
    barrier = threading.Barrier(2)

    def attempt():
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
        barrier.wait()
        try:
            results.append(brix_drip.open_claim(c, "rAlice"))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if isinstance(r, tuple)]
    losses = [r for r in results if isinstance(r, brix_drip.ClaimInFlight)]
    assert len(wins) == 1, results
    assert len(losses) == 1, results
    assert wins[0][1] == 5


# --- Task 7: recovery ------------------------------------------------------


def _stale_claim(conn, claim_id=1, last_ledger_seq=500, state="submitted"):
    _accrue(conn, "rAlice", 2)
    conn.execute(
        "UPDATE brix_accruals SET claim_id = ? WHERE owner = 'rAlice'",
        (claim_id,),
    )
    conn.execute(
        "INSERT INTO brix_claims (claim_id, wallet, amount, state, last_ledger_seq)"
        " VALUES (?, 'rAlice', 2, ?, ?)",
        (claim_id, state, last_ledger_seq),
    )
    conn.commit()


def test_recover_confirms_a_claim_whose_memo_tx_is_on_ledger(conn):
    _stale_claim(conn)
    outcomes = brix_drip.recover(
        conn,
        finder=lambda claim_id: "FOUNDHASH",
        validated_ledger_index=600,
    )
    assert outcomes == {1: "confirmed"}
    row = conn.execute("SELECT * FROM brix_claims WHERE claim_id = 1").fetchone()
    assert row["state"] == "confirmed"
    assert row["tx_hash"] == "FOUNDHASH"


def test_recover_fails_a_claim_only_once_last_ledger_sequence_has_passed(conn):
    _stale_claim(conn, last_ledger_seq=500)
    outcomes = brix_drip.recover(conn, finder=lambda claim_id: None, validated_ledger_index=600)
    assert outcomes == {1: "failed"}
    assert brix_drip.claimable(conn, "rAlice") == 2


def test_recover_leaves_a_claim_alone_while_its_tx_could_still_validate(conn):
    """Absence from account_tx is NOT proof of failure — until the validated
    ledger passes LastLedgerSequence the transaction can still land."""
    _stale_claim(conn, last_ledger_seq=500)
    outcomes = brix_drip.recover(conn, finder=lambda claim_id: None, validated_ledger_index=400)
    assert outcomes == {}
    row = conn.execute("SELECT state FROM brix_claims WHERE claim_id = 1").fetchone()
    assert row["state"] == "submitted"
    assert brix_drip.claimable(conn, "rAlice") == 0


def test_recover_leaves_a_claim_alone_when_last_ledger_seq_is_unknown(conn):
    _stale_claim(conn, last_ledger_seq=None)
    outcomes = brix_drip.recover(conn, finder=lambda claim_id: None, validated_ledger_index=10**9)
    assert outcomes == {}


def test_recover_never_guesses_when_the_lookup_itself_fails(conn):
    _stale_claim(conn, last_ledger_seq=500)

    def broken(claim_id):
        raise RuntimeError("account_tx unavailable")

    outcomes = brix_drip.recover(conn, finder=broken, validated_ledger_index=600)
    assert outcomes == {}
    row = conn.execute("SELECT state FROM brix_claims WHERE claim_id = 1").fetchone()
    assert row["state"] == "submitted"


def test_recover_ignores_already_terminal_claims(conn):
    _stale_claim(conn, state="confirmed")
    calls: list[int] = []
    outcomes = brix_drip.recover(
        conn,
        finder=lambda claim_id: calls.append(claim_id),
        validated_ledger_index=10**9,
    )
    assert outcomes == {}
    assert calls == []


# --- review hardening: find_claim_payment must authenticate the payout ------


def _tx_entry(
    account=DISTRIBUTOR,
    destination="rAlice",
    value="5",
    currency=None,
    result="tesSUCCESS",
    validated=True,
    tx_type="Payment",
    memo="lfg:brix_claim:42",
    tx_hash="REALHASH",
):
    from lfg_core import config as cfg

    return {
        "hash": tx_hash,
        "validated": validated,
        "meta": {"TransactionResult": result},
        "tx": {
            "TransactionType": tx_type,
            "Account": account,
            "Destination": destination,
            "Amount": {
                "currency": currency or cfg.BRIX_CURRENCY_HEX,
                "issuer": cfg.BRIX_ISSUER,
                "value": value,
            },
            "Memos": [{"Memo": {"MemoData": memo.encode().hex().upper()}}],
        },
    }


@pytest.fixture()
def account_tx(monkeypatch):
    """Serve a canned account_tx page and pin the distributor config."""
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_ADDRESS", DISTRIBUTOR, raising=False)
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_SEED", DISTRIBUTOR_SEED, raising=False)
    box = {"entries": []}

    class _Resp:
        def __init__(self, result):
            self.result = result

    def fake_request(self, request):
        return _Resp({"transactions": box["entries"], "marker": None})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request, raising=False)
    return box


def _find(claim_id=42, wallet="rAlice", amount=5):
    return asyncio.run(xrpl_ops.find_claim_payment(claim_id, wallet=wallet, amount=amount))


def test_find_claim_payment_accepts_a_genuine_payout(account_tx):
    account_tx["entries"] = [_tx_entry()]
    assert _find() == "REALHASH"


def test_find_claim_payment_rejects_a_forged_memo_from_a_stranger(account_tx):
    """SECURITY: anyone can send the distributor a transaction carrying a
    guessed `lfg:brix_claim:<n>` memo. Trusting it would mark an unpaid claim
    confirmed and permanently swallow the holder's accruals."""
    account_tx["entries"] = [_tx_entry(account="rAttacker", tx_hash="FORGED")]
    assert _find() is None


def test_find_claim_payment_rejects_a_payout_to_the_wrong_wallet(account_tx):
    account_tx["entries"] = [_tx_entry(destination="rSomeoneElse")]
    assert _find() is None


def test_find_claim_payment_rejects_a_failed_transaction(account_tx):
    account_tx["entries"] = [_tx_entry(result="tecPATH_DRY")]
    assert _find() is None


def test_find_claim_payment_rejects_an_unvalidated_transaction(account_tx):
    account_tx["entries"] = [_tx_entry(validated=False)]
    assert _find() is None


def test_find_claim_payment_rejects_a_short_payment(account_tx):
    """A payout for less than the claim must not confirm the full amount."""
    account_tx["entries"] = [_tx_entry(value="1")]
    assert _find(amount=5) is None


def test_find_claim_payment_rejects_the_wrong_currency(account_tx):
    account_tx["entries"] = [_tx_entry(currency="4C46474F00000000000000000000000000000000")]
    assert _find() is None


def test_find_claim_payment_rejects_a_non_payment(account_tx):
    account_tx["entries"] = [_tx_entry(tx_type="NFTokenMint")]
    assert _find() is None


def test_find_claim_payment_falls_back_to_the_seed_derived_address(account_tx, monkeypatch):
    """An operator who sets only BRIX_DISTRIBUTOR_SEED must not end up with
    claims that can be submitted but never recovered."""
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_ADDRESS", None, raising=False)
    account_tx["entries"] = [_tx_entry(account=DISTRIBUTOR)]
    assert _find() == "REALHASH"


def test_distributor_address_refuses_a_seed_address_mismatch(monkeypatch):
    """A configured address that disagrees with the signing seed would build
    every Payment for one account and sign it with another — each payout fails
    or goes indeterminate. Refuse up front, not one stuck claim at a time."""
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_SEED", DISTRIBUTOR_SEED, raising=False)
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_ADDRESS", "rSomeOtherAccount", raising=False)
    with pytest.raises(RuntimeError, match="does not match"):
        xrpl_ops.distributor_address()


def test_send_brix_claim_keeps_the_deadline_when_submission_raises(monkeypatch, capture_payment):
    """An unexpected exception must not escape carrying the LastLedgerSequence
    with it — a claim recorded without one is a claim recovery can never
    resolve, stranding the holder's balance permanently."""

    async def boom(tx, wallet, client, label, **kwargs):
        capture_payment.tx = tx
        raise RuntimeError("connection reset mid-submit")

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", boom)
    result = asyncio.run(xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42))
    assert result.state == "unknown"
    assert result.last_ledger_seq is not None


def test_find_claim_payment_raises_rather_than_reporting_a_hashless_payout_absent(account_tx):
    """The payout is real and validated; we just cannot name it. Returning
    None would read as "absent", and past LastLedgerSequence that is treated as
    proof of failure — the accruals would unbind and the BRIX be paid twice."""
    entry = _tx_entry()
    entry.pop("hash")
    entry["tx"].pop("hash", None)
    account_tx["entries"] = [entry]
    with pytest.raises(RuntimeError, match="no usable hash"):
        _find()


def test_find_claim_payment_bounds_the_scan_by_the_claim_deadline(account_tx, monkeypatch):
    """Unbounded, a never-paid claim pages the distributor's whole history —
    once per open claim, growing forever as payouts accumulate."""
    seen = {}

    class _Resp:
        def __init__(self, result):
            self.result = result

    def capture(self, request):
        seen["min"] = request.ledger_index_min
        return _Resp({"transactions": [], "marker": None})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", capture, raising=False)
    asyncio.run(xrpl_ops.find_claim_payment(42, wallet="rAlice", amount=5, min_ledger=100000))
    assert seen["min"] == 100000 - xrpl_ops._CLAIM_SCAN_LEDGER_SLACK


def test_send_brix_claim_never_outlives_the_recorded_deadline(capture_payment):
    """The stored deadline must stay an upper bound at every instant. A payment
    allowed to outlive it could still validate after recovery had declared the
    claim failed and unbound the accruals — paying the same BRIX twice."""
    result = asyncio.run(
        xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42, max_last_ledger_seq=1010)
    )
    assert result.last_ledger_seq == 1010
    assert capture_payment.tx.last_ledger_sequence == 1010


def test_send_brix_claim_keeps_its_own_deadline_when_it_is_tighter(capture_payment):
    result = asyncio.run(
        xrpl_ops.send_brix_claim("rAlice", 5, claim_id=42, max_last_ledger_seq=10**9)
    )
    assert result.last_ledger_seq == 1000 + config.BRIX_CLAIM_LEDGER_MARGIN
