"""Fee-cover refund Payment, its recovery lookup, and the balance helper."""

from __future__ import annotations

import asyncio

import pytest

from lfg_core import config, memos, xrpl_ops

ACCEPT = "FED6256DC27A6D06C43E20ADA2B5D2FFF4AEAEE7560FADBF584CAE6396C1ACFA"
BUYER = "rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf"


def _run(coro):
    # A private loop: never disturbs (or depends on) the global event loop,
    # which asyncio.run() in other test modules leaves unset on Python 3.10.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decoded(tx):
    return [bytes.fromhex(m.memo_data).decode() for m in tx.memos]


class _Captured:
    tx = None


@pytest.fixture()
def capture(monkeypatch):
    box = _Captured()

    async def fake_submit(tx, wallet, client, label, **kwargs):
        box.tx = tx
        return {"hash": "PAYOUTHASH", "meta": {"TransactionResult": "tesSUCCESS"}}

    async def fake_ledger(client):
        return 1000

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", fake_ledger)
    return box


def test_fee_cover_is_a_valid_memo_action():
    models = memos.build_memo_models(
        memos.INITIATOR_BACKEND,
        memos.PLATFORM_BACKEND,
        memos.ACTION_FEE_COVER,
        campaign="fee-cover-7",
    )
    assert "fee-cover" in [bytes.fromhex(m.memo_data).decode() for m in models]


def test_send_fee_cover_refund_builds_a_tagged_xrp_payment_from_the_signer(capture):
    result = _run(xrpl_ops.send_fee_cover_refund(BUYER, 80_642, ACCEPT, campaign_id=7))
    tx = capture.tx
    assert tx.account == config.SIGNING_ACCOUNT
    assert tx.destination == BUYER
    assert tx.amount == "80642"
    assert tx.source_tag == 2606160021
    assert tx.last_ledger_sequence == 1000 + config.FEE_COVER_LEDGER_MARGIN
    decoded = _decoded(tx)
    assert f"lfg:fee_cover:{ACCEPT}" in decoded
    assert "fee-cover" in decoded and "fee-cover-7" in decoded
    assert (result.state, result.tx_hash, result.last_ledger_seq) == (
        "confirmed",
        "PAYOUTHASH",
        tx.last_ledger_sequence,
    )


def test_send_fee_cover_refund_never_outlives_the_recorded_deadline(capture):
    result = _run(
        xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7, max_last_ledger_seq=1005)
    )
    assert capture.tx.last_ledger_sequence == 1005 == result.last_ledger_seq


def test_send_fee_cover_refund_maps_failed_and_unknown(monkeypatch, capture):
    async def failed(tx, wallet, client, label, **kwargs):
        return None

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", failed)
    assert _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7)).state == "failed"

    async def indeterminate(tx, wallet, client, label, **kwargs):
        raise xrpl_ops.IndeterminateResultError("timeout")

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", indeterminate)
    unknown = _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7))
    assert unknown.state == "unknown" and unknown.last_ledger_seq is not None


def test_send_fee_cover_refund_refuses_before_submitting(monkeypatch, capture):
    async def no_ledger(client):
        return None

    with pytest.raises(xrpl_ops.ClaimNotSubmitted):
        _run(xrpl_ops.send_fee_cover_refund(BUYER, 0, ACCEPT, campaign_id=7))
    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", no_ledger)
    with pytest.raises(xrpl_ops.ClaimNotSubmitted):
        _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7))
    assert capture.tx is None


def test_send_fee_cover_refund_preflight_ledger_error_is_not_submitted(monkeypatch, capture):
    async def ledger_raises(client):
        raise ConnectionError("rippled unreachable")

    monkeypatch.setattr(xrpl_ops, "_current_validated_ledger_index", ledger_raises)
    with pytest.raises(xrpl_ops.ClaimNotSubmitted) as excinfo:
        _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7))
    assert isinstance(excinfo.value.__cause__, ConnectionError)
    assert capture.tx is None


def test_send_fee_cover_refund_bad_signing_seed_is_not_submitted(monkeypatch, capture):
    monkeypatch.setattr(config, "SEED", "not-a-seed")
    with pytest.raises(xrpl_ops.ClaimNotSubmitted) as excinfo:
        _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7))
    assert excinfo.value.__cause__ is not None
    assert capture.tx is None


def test_send_fee_cover_refund_payment_construction_error_is_not_submitted(monkeypatch, capture):
    def bad_payment(**kwargs):
        raise ValueError("invalid destination")

    monkeypatch.setattr(xrpl_ops, "Payment", bad_payment)
    with pytest.raises(xrpl_ops.ClaimNotSubmitted) as excinfo:
        _run(xrpl_ops.send_fee_cover_refund(BUYER, 1, ACCEPT, campaign_id=7))
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert capture.tx is None


def _entry(
    *,
    account=None,
    destination=BUYER,
    delivered="80642",
    result="tesSUCCESS",
    validated=True,
    tx_type="Payment",
    memo=f"lfg:fee_cover:{ACCEPT}",
    tx_hash="PAYOUTHASH",
):
    return {
        "hash": tx_hash,
        "validated": validated,
        "meta": {"TransactionResult": result, "delivered_amount": delivered},
        "tx": {
            "TransactionType": tx_type,
            "Account": account or config.SIGNING_ACCOUNT,
            "Destination": destination,
            "Amount": delivered,
            "Memos": [{"Memo": {"MemoData": memo.encode().hex().upper()}}],
        },
    }


@pytest.fixture()
def account_tx(monkeypatch):
    box = {"entries": [], "error": None}

    class _Resp:
        def __init__(self, result, ok=True):
            self.result = result
            self._ok = ok

        def is_successful(self):
            return self._ok

    def fake_request(self, request):
        box["ledger_index_min"] = request.ledger_index_min
        if box["error"] is not None:
            return _Resp(box["error"], ok=False)
        return _Resp({"transactions": box["entries"], "marker": None})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request, raising=False)
    return box


def _find(min_ledger=6000):
    return _run(
        xrpl_ops.find_fee_cover_payment(
            ACCEPT, destination=BUYER, drops=80_642, min_ledger=min_ledger
        )
    )


def test_find_fee_cover_payment_accepts_a_genuine_refund(account_tx):
    account_tx["entries"] = [_entry()]
    assert _find() == "PAYOUTHASH"


@pytest.mark.parametrize(
    "overrides",
    [
        {"account": "rStrangerSendingAForgedMemo"},
        {"destination": "rSomeoneElse"},
        {"delivered": "80641"},
        {"result": "tecUNFUNDED_PAYMENT"},
        {"validated": False},
        {"tx_type": "AccountSet"},
        {"memo": "lfg:fee_cover:OTHER"},
    ],
)
def test_find_fee_cover_payment_rejects_anything_not_our_refund(account_tx, overrides):
    account_tx["entries"] = [_entry(**overrides)]
    assert _find() is None


def test_find_fee_cover_payment_raises_on_an_account_tx_error_response(account_tx):
    """An error response carries no `transactions`: read as "absent", recovery
    would park a refund that may have landed and a requeue would pay it twice."""
    account_tx["error"] = {"error": "lgrIdxMalformed", "status": "error"}
    with pytest.raises(RuntimeError, match="account_tx error"):
        _find()


def test_get_xrp_balance_drops(monkeypatch):
    class _Resp:
        def __init__(self, ok, result):
            self._ok = ok
            self.result = result

        def is_successful(self):
            return self._ok

    responses = [
        _Resp(True, {"account_data": {"Balance": "215611627"}}),
        _Resp(False, {"error": "actNotFound"}),
    ]

    async def fake_request(self, request):
        return responses.pop(0)

    monkeypatch.setattr(xrpl_ops.AsyncJsonRpcClient, "request", fake_request, raising=False)
    assert _run(xrpl_ops.get_xrp_balance_drops("rIssuer")) == 215_611_627
    assert _run(xrpl_ops.get_xrp_balance_drops("rIssuer")) is None


def test_find_fee_cover_payment_scans_from_before_the_provisional_deadline(account_tx, monkeypatch):
    """`min_ledger` is the row's DEADLINE, and before the payout is recorded
    that deadline is the provisional `current + FEE_COVER_LEDGER_MARGIN * 10`.
    The scan floor has to clear that whole span: a margin raised past ~500
    would otherwise start the scan after the refund actually validated and
    report a payout that landed as absent — which past the deadline reads as
    "failed", and a requeue would pay it twice."""
    account_tx["entries"] = [_entry()]
    monkeypatch.setattr(xrpl_ops.config, "FEE_COVER_LEDGER_MARGIN", 40)
    _find()
    assert account_tx["ledger_index_min"] == 6000 - (5000 + 400)
    monkeypatch.setattr(xrpl_ops.config, "FEE_COVER_LEDGER_MARGIN", 2_000)
    _find(min_ledger=100_000)
    # The window grows with the margin, so the payout stays inside it.
    assert account_tx["ledger_index_min"] == 100_000 - (5000 + 20_000)


def test_find_claim_payment_keeps_the_plain_slack(monkeypatch):
    """The fee-cover widening is local to the fee-cover caller: the BRIX claim
    scan, whose deadline is `current + margin`, is unchanged."""
    seen = {}

    class _Resp:
        result = {"transactions": [], "marker": None}

        def is_successful(self):
            return True

    def fake_request(self, request):
        seen["min"] = request.ledger_index_min
        return _Resp()

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", fake_request, raising=False)
    monkeypatch.setattr(xrpl_ops, "distributor_address", lambda: config.SIGNING_ACCOUNT)
    _run(xrpl_ops.find_claim_payment(1, BUYER, 5, min_ledger=6000))
    assert seen["min"] == 6000 - 5000
