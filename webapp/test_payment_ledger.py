"""Consumed-payment ledger tests (issue #196).

Every payment that satisfies a wait_for_payment call must be recorded as
consumed by tx hash, so a duplicate or late payment becomes a credit the
next session can consume instead of a silent burn — and so one on-ledger
payment can never satisfy two sessions.
"""

import asyncio
import copy

import pytest

from lfg_core import payment_ledger, xrpl_ops

CUR = "4C46474F00000000000000000000000000000000"

STREAM_MSG = {
    "type": "transaction",
    "validated": True,
    "tx_json": {
        "TransactionType": "Payment",
        "Account": "rSender",
        "Destination": "rDest",
        "DeliverMax": {"currency": CUR, "issuer": "rIssuer", "value": "1"},
        "hash": "LEDGERH1",
    },
    "meta": {"delivered_amount": {"currency": CUR, "issuer": "rIssuer", "value": "1"}},
}


@pytest.fixture()
def ledger_db(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(payment_ledger, "_db_path", lambda: path)
    # No real payment ever validates during a unit test: skip the grace sleep.
    monkeypatch.setattr(xrpl_ops.config, "PAYMENT_GRACE_SECONDS", 0)
    payment_ledger.init_ledger()
    return path


def _backfill_ws(entries):
    """FakeWS whose account_tx returns the given entries and whose stream
    never yields (so only the backfill path can match)."""

    class FakeWS:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, req):
            pass

        async def request(self, req):
            class R:
                result = {"transactions": entries}

            return R()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    return FakeWS


def test_try_consume_is_atomic_per_hash(ledger_db):
    assert payment_ledger.try_consume("HX", "rSender", "rDest") is True
    assert payment_ledger.try_consume("HX", "rSender", "rDest") is False
    assert payment_ledger.try_consume("HY", "rSender", "rDest") is True


def test_same_payment_cannot_satisfy_two_waits(ledger_db, monkeypatch):
    entry = copy.deepcopy(STREAM_MSG)
    entry["tx_json"]["date"] = 800000000
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _backfill_ws([entry]))
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    first = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix - 60)
    )
    assert first is True

    # The identical on-ledger payment is now consumed: a second session
    # polling the same window must NOT mint against it again.
    second = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix - 60)
    )
    assert second is False


def test_unconsumed_duplicate_becomes_credit(ledger_db, monkeypatch):
    """Two on-ledger payments, one already consumed: the next wait must
    match the *other* one instead of failing (the 60-paid/40-minted bug)."""
    e1 = copy.deepcopy(STREAM_MSG)
    e1["tx_json"]["date"] = 800000000
    e2 = copy.deepcopy(STREAM_MSG)
    e2["tx_json"]["date"] = 800000030
    e2["tx_json"]["hash"] = "LEDGERH2"
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _backfill_ws([e1, e2]))
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    assert payment_ledger.try_consume("LEDGERH1", "rSender", "rDest") is True
    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix - 60)
    )
    assert paid is True
    # ...and now both are consumed.
    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix - 60)
    )
    assert paid is False


def test_credit_window_reaches_before_session_start(ledger_db, monkeypatch):
    """allow_credit widens the backfill window so a payment made BEFORE the
    session started (e.g. it landed just after the previous session timed
    out) is honoured instead of burned."""
    entry = copy.deepcopy(STREAM_MSG)
    entry["tx_json"]["date"] = 800000000
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _backfill_ws([entry]))
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    # Session starts an hour after the payment: plain wait rejects it...
    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix + 3600)
    )
    assert paid is False
    # ...but with allow_credit the unconsumed payment is found and consumed.
    monkeypatch.setattr(payment_ledger, "bootstrap_floor", lambda: tx_unix - 3600)
    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment(
            "rDest", "rSender", timeout_seconds=1, not_before=tx_unix + 3600, allow_credit=True
        )
    )
    assert paid is True


def test_bootstrap_floor_blocks_predeploy_credits(ledger_db, monkeypatch):
    """Payments validated before the ledger was bootstrapped predate consumed
    tracking and must never be spendable as credits."""
    entry = copy.deepcopy(STREAM_MSG)
    entry["tx_json"]["date"] = 800000000
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _backfill_ws([entry]))
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    monkeypatch.setattr(payment_ledger, "bootstrap_floor", lambda: tx_unix + 60)
    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment(
            "rDest", "rSender", timeout_seconds=1, not_before=tx_unix + 3600, allow_credit=True
        )
    )
    assert paid is False
