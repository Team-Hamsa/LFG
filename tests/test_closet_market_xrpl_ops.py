import asyncio

import pytest
from xrpl.models.transactions import EscrowCancel, EscrowFinish, Payment

from lfg_core import config, memos, xrpl_ops, xumm_ops


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tag_hex(tag):
    return tag.encode().hex().upper()


@pytest.fixture
def submit(monkeypatch):
    seen = {"result": {"hash": "H1", "meta": {"TransactionResult": "tesSUCCESS"}}, "raise": None}

    async def ledger(client):
        return seen.get("ledger", 1000)

    async def fake_submit(tx, wallet, client, label, **kwargs):
        seen["tx"] = tx
        if seen["raise"] is not None:
            raise seen["raise"]
        return seen["result"]

    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", ledger)
    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
    monkeypatch.setattr(config, "CLOSET_MARKET_LEDGER_MARGIN", 40)
    return seen


def test_app_brix_payment_shape(submit):
    out = _run(
        xrpl_ops.app_brix_payment(
            "rSeller", "9.3", "lfg:closet_forward:F1", memos.ACTION_CLOSET_FORWARD
        )
    )
    assert out == xrpl_ops.TxOutcome("confirmed", "H1", 1040)
    tx = submit["tx"]
    assert isinstance(tx, Payment)
    assert tx.account == config.SIGNING_ACCOUNT and tx.destination == "rSeller"
    assert tx.amount.currency == config.BRIX_CURRENCY_HEX and tx.amount.issuer == config.BRIX_ISSUER
    assert tx.amount.value == "9.3"
    assert tx.source_tag == config.SOURCE_TAG and tx.last_ledger_sequence == 1040
    assert tx.memos[-1].memo_data == _tag_hex("lfg:closet_forward:F1")
    assert len(tx.memos) > 1  # provenance memos precede the tag


def test_definitive_failure_and_unknown(submit):
    submit["result"] = None
    assert (
        _run(
            xrpl_ops.app_brix_payment("rS", "1", "lfg:closet_refund:X", memos.ACTION_CLOSET_REFUND)
        ).state
        == "failed"
    )
    submit["raise"] = xrpl_ops.IndeterminateResultError("lost")
    out = _run(
        xrpl_ops.app_brix_payment("rS", "1", "lfg:closet_refund:X", memos.ACTION_CLOSET_REFUND)
    )
    assert out == xrpl_ops.TxOutcome("unknown", None, 1040)


def test_max_last_ledger_seq_clamps_and_is_reported(submit):
    """Write-ahead intent (#443 review fix): a caller that already persisted a
    deadline before submitting must never have it silently extended."""
    out = _run(
        xrpl_ops.app_brix_payment(
            "rSeller",
            "9.3",
            "lfg:closet_forward:F1",
            memos.ACTION_CLOSET_FORWARD,
            max_last_ledger_seq=1020,
        )
    )
    assert out == xrpl_ops.TxOutcome("confirmed", "H1", 1020)
    assert submit["tx"].last_ledger_sequence == 1020

    out = _run(
        xrpl_ops.escrow_finish(
            "rBidder", 5, "A025", "A022", "lfg:closet_finish:F1", max_last_ledger_seq=1020
        )
    )
    assert out.last_ledger_seq == 1020 and submit["tx"].last_ledger_sequence == 1020

    out = _run(
        xrpl_ops.escrow_cancel("rBidder", 5, "lfg:closet_cancel:O1", max_last_ledger_seq=1020)
    )
    assert out.last_ledger_seq == 1020 and submit["tx"].last_ledger_sequence == 1020

    # A cap ABOVE the freshly-computed value is a no-op — it never extends the deadline.
    out = _run(
        xrpl_ops.app_brix_payment(
            "rS", "1", "lfg:closet_refund:X", memos.ACTION_CLOSET_REFUND, max_last_ledger_seq=5000
        )
    )
    assert out.last_ledger_seq == 1040 and submit["tx"].last_ledger_sequence == 1040


def test_unreadable_ledger_means_not_submitted(submit):
    submit["ledger"] = None
    with pytest.raises(xrpl_ops.TxNotSubmitted):
        _run(xrpl_ops.escrow_cancel("rBidder", 5, "lfg:closet_cancel:O1"))
    assert "tx" not in submit


def test_escrow_finish_and_cancel_shape(submit):
    _run(xrpl_ops.escrow_finish("rBidder", 5, "A025", "A022", "lfg:closet_finish:F1"))
    tx = submit["tx"]
    assert isinstance(tx, EscrowFinish)
    assert (tx.owner, tx.offer_sequence, tx.condition, tx.fulfillment) == (
        "rBidder",
        5,
        "A025",
        "A022",
    )
    assert tx.account == config.SIGNING_ACCOUNT and tx.source_tag == config.SOURCE_TAG
    _run(xrpl_ops.escrow_cancel("rBidder", 5, "lfg:closet_cancel:O1"))
    assert isinstance(submit["tx"], EscrowCancel) and submit["tx"].offer_sequence == 5


class _Resp:
    def __init__(self, result, ok=True):
        self.result, self._ok = result, ok

    def is_successful(self):
        return self._ok


class _Client:
    responses: list = []

    def __init__(self, url):
        pass

    def request(self, req):
        return _Client.responses.pop(0)


def test_get_escrow(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _Client)
    _Client.responses = [
        _Resp({"node": {"Account": "rA"}}),
        _Resp({"error": "entryNotFound"}, ok=False),
        _Resp({"error": "tooBusy"}, ok=False),
    ]
    assert _run(xrpl_ops.get_escrow("rA", 5)) == {"Account": "rA"}
    assert _run(xrpl_ops.get_escrow("rA", 5)) is None
    with pytest.raises(RuntimeError):
        _run(xrpl_ops.get_escrow("rA", 5))


def test_find_app_txs_by_memo_pages_and_ignores_inbound(monkeypatch):
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _Client)
    tag = "lfg:closet_forward:F1"
    memo = [{"Memo": {"MemoData": _tag_hex(tag)}}]
    app = config.SIGNING_ACCOUNT
    mine = {
        "validated": True,
        "hash": "OK",
        "meta": {"TransactionResult": "tesSUCCESS"},
        "tx_json": {"Account": app, "Memos": memo},
    }
    inbound = {
        "validated": True,
        "hash": "EVIL",
        "meta": {"TransactionResult": "tesSUCCESS"},
        "tx_json": {"Account": "rEvil", "Memos": memo},
    }
    unvalidated = {"validated": False, "hash": "U", "tx_json": {"Account": app, "Memos": memo}}
    _Client.responses = [
        _Resp({"transactions": [inbound, unvalidated], "marker": "m", "ledger_index_max": 550}),
        _Resp({"transactions": [mine], "ledger_index_max": 700}),
    ]
    found = _run(xrpl_ops.find_app_txs_by_memo(tag, 500))
    assert [xrpl_ops.tx_entry_hash(e) for e in found] == ["OK"]
    assert xrpl_ops.tx_entry_result(found[0]) == "tesSUCCESS"


def test_find_app_txs_by_memo_raises_when_scan_lags_deadline(monkeypatch):
    """(#443 review fix) If the server answering account_tx never reported a
    ledger_index_max reaching min_ledger, its view of "nothing found" is too
    stale to trust — raise so the caller treats it as "wait", not "absent"
    (which would resubmit a tx that may have already landed)."""
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _Client)
    tag = "lfg:closet_forward:F1"
    _Client.responses = [_Resp({"transactions": [], "ledger_index_max": 400})]
    with pytest.raises(RuntimeError):
        _run(xrpl_ops.find_app_txs_by_memo(tag, 500))


def test_find_app_txs_by_memo_raises_when_ledger_index_max_absent(monkeypatch):
    """(#443 review fix round 2) No page reporting ledger_index_max at all is
    just as untrustworthy as an insufficient one — raise rather than treat
    the scan's silence as a genuine "nothing found"."""
    monkeypatch.setattr(xrpl_ops, "JsonRpcClient", _Client)
    tag = "lfg:closet_forward:F1"
    _Client.responses = [_Resp({"transactions": []})]  # no ledger_index_max key at all
    with pytest.raises(RuntimeError):
        _run(xrpl_ops.find_app_txs_by_memo(tag, 500))


def test_closet_payload_builders(monkeypatch):
    calls = []

    async def fake_create(txjson, options=None, user_token=None, memos_json=None, custom_meta=None):
        calls.append((txjson, options, user_token, memos_json))
        return {"uuid": "U", "xumm_url": "x", "qr_url": "q", "push": None}

    monkeypatch.setattr(xumm_ops, "_create_xumm_payload", fake_create)
    amount = {"currency": "BRIX", "issuer": "rI", "value": "10"}
    _run(
        xumm_ops.create_closet_bid_payload(
            "rBidder", amount, "rApp", "A025", 123, user_token="T", platform=memos.PLATFORM_WEBAPP
        )
    )
    txjson, options, token, memos_json = calls[0]
    assert txjson == {
        "TransactionType": "EscrowCreate",
        "Account": "rBidder",
        "Destination": "rApp",
        "Amount": amount,
        "Condition": "A025",
        "CancelAfter": 123,
    }
    assert token == "T" and options["expire"] == xumm_ops.DEFAULT_EXPIRE_MINUTES
    assert memos_json == memos.build_memos_json(
        memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_CLOSET_BID
    )
    _run(
        xumm_ops.create_closet_buy_payload(
            "rBuyer", amount, "rApp", "AB" * 32, send_max_drops="2500000"
        )
    )
    txjson = calls[1][0]
    assert (
        txjson["InvoiceID"] == "AB" * 32
        and txjson["SendMax"] == "2500000"
        and "Memos" not in txjson
    )
    _run(xumm_ops.create_closet_buy_payload("rBuyer", amount, "rApp", "AB" * 32))
    assert "SendMax" not in calls[2][0]
