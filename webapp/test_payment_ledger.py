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
    # The fixture payments use a fixed 2025 ripple-epoch date; a real 30-day
    # TTL would expire them, so tests widen it and narrow it per-case.
    monkeypatch.setattr(xrpl_ops.config, "MINT_CREDIT_TTL_SECONDS", 10**10)
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


def test_failed_tec_payment_never_matches(ledger_db, monkeypatch):
    """A validated tec... payment has no delivered_amount; the DeliverMax
    fallback must not let it satisfy a wait (CodeRabbit critical on #197 —
    a tecPATH_DRY 'payment' would otherwise buy a mint with no funds moved)."""
    entry = copy.deepcopy(STREAM_MSG)
    entry["tx_json"]["date"] = 800000000
    entry["meta"] = {"TransactionResult": "tecPATH_DRY"}  # no delivered_amount
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _backfill_ws([entry]))
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment("rDest", "rSender", timeout_seconds=1, not_before=tx_unix - 60)
    )
    assert paid is False


def test_try_consume_fails_closed_on_unopenable_db(monkeypatch):
    monkeypatch.setattr(payment_ledger, "_db_path", lambda: "/nonexistent-dir/ledger.db")
    assert payment_ledger.try_consume("HZ", "rSender", "rDest") is False


def test_credit_scan_pages_past_five_pages(ledger_db, monkeypatch):
    """A credit deeper than 100 issuer transactions must still be found:
    pagination follows the marker to the floor instead of a 5-page cap
    (Greptile P1 / CodeRabbit major on #197)."""
    target = copy.deepcopy(STREAM_MSG)
    target["tx_json"]["date"] = 800000000

    def filler_page(page):
        # Newest-first across pages: each page is strictly older filler.
        date = 800009000 - page * 100
        return [
            {"validated": True, "tx_json": {"TransactionType": "AccountSet", "date": date - i}}
            for i in range(20)
        ]

    class FakeWS:
        def __init__(self, url):
            self.last_marker = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, req):
            pass

        async def request(self, req):
            # Pagination is driven by the marker the caller sends back, so a
            # regression that re-requests page 1 can never "reach" page 8.
            marker = getattr(req, "marker", None)
            assert marker == self.last_marker, "request must forward the previous marker"
            page = 1 if marker is None else marker["p"] + 1

            class R:
                # 7 pages of unrelated traffic, the credit on page 8.
                result = (
                    {"transactions": filler_page(page), "marker": {"p": page}}
                    if page < 8
                    else {"transactions": [target]}
                )

            self.last_marker = R.result.get("marker")
            return R()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", FakeWS)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    monkeypatch.setattr(payment_ledger, "bootstrap_floor", lambda: 800000000 - 3600)
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment(
            "rDest", "rSender", timeout_seconds=1, not_before=tx_unix + 3600, allow_credit=True
        )
    )
    assert paid is True


def test_expired_credit_is_not_spendable(ledger_db, monkeypatch):
    """A credit older than MINT_CREDIT_TTL_SECONDS is refund territory, not
    mintable — the TTL is also what bounds the backfill scan depth."""
    entry = copy.deepcopy(STREAM_MSG)
    entry["tx_json"]["date"] = 800000000
    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", _backfill_ws([entry]))
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    monkeypatch.setattr(xrpl_ops.config, "MINT_CREDIT_TTL_SECONDS", 60)
    monkeypatch.setattr(payment_ledger, "bootstrap_floor", lambda: 0.0)
    loop = asyncio.get_event_loop()
    tx_unix = 800000000 + xrpl_ops.RIPPLE_EPOCH_OFFSET

    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment(
            "rDest", "rSender", timeout_seconds=1, not_before=tx_unix + 3600, allow_credit=True
        )
    )
    assert paid is False


def test_credit_scan_aborts_when_pages_stop_progressing(ledger_db, monkeypatch):
    """A server that keeps returning markers without reaching older history
    must not loop the scan forever — the progress guard aborts it."""
    filler = [
        {"validated": True, "tx_json": {"TransactionType": "AccountSet", "date": 800009000}}
    ] * 20

    class FakeWS:
        def __init__(self, url):
            self.requests = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, req):
            pass

        async def request(self, req):
            self.requests += 1
            assert self.requests < 10, "progress guard failed to stop the scan"

            class R:
                result = {"transactions": filler, "marker": {"p": 1}}

            return R()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", FakeWS)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_CURRENCY_HEX", CUR)
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuer")
    monkeypatch.setattr(payment_ledger, "bootstrap_floor", lambda: 0.0)
    loop = asyncio.get_event_loop()

    paid = loop.run_until_complete(
        xrpl_ops.wait_for_payment(
            "rDest", "rSender", timeout_seconds=1, not_before=2**33, allow_credit=True
        )
    )
    assert paid is False
