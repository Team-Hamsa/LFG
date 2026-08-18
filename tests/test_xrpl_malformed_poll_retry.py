# #385: rippled sometimes answers HTTP 200 with a JSON body missing the
# `result` key; xrpl-py surfaces that as KeyError('result') during the Tx
# confirm-poll. The poll is a pure read — transient garbage must be retried a
# bounded number of times BEFORE declaring the outcome indeterminate, while
# persistent garbage still fails closed (never blind-resubmit the write).

import asyncio

import pytest

import lfg_core.xrpl_ops as xrpl_ops


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, result: dict) -> None:
        self.result = result


class _Signed:
    def __init__(self, tx_hash: str = "TXHASH") -> None:
        self._hash = tx_hash

    def get_hash(self) -> str:
        return self._hash


def _stub_sign(monkeypatch, tx_hash: str = "TXHASH") -> None:
    monkeypatch.setattr(
        xrpl_ops, "autofill_and_sign", lambda tx, client, wallet, **k: _Signed(tx_hash)
    )


def _no_sleep(monkeypatch):
    async def fast_sleep(_delay):
        return None

    monkeypatch.setattr(xrpl_ops.asyncio, "sleep", fast_sleep)


def _validated(tx_hash: str = "TXHASH") -> _Resp:
    return _Resp({"hash": tx_hash, "validated": True, "meta": {"TransactionResult": "tesSUCCESS"}})


def _malformed_submit(tx, client, wallet, **k):
    raise KeyError("result")  # xrpl-py json_to_response on a result-less 200 body


# --- transient malformed poll bodies must NOT strand the tx as indeterminate ---


def test_transient_malformed_poll_recovers_to_committed(monkeypatch):
    _stub_sign(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", _malformed_submit)

    calls = {"n": 0}

    def flaky_then_ok(self, req):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise KeyError("result")
        return _validated()

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", flaky_then_ok)
    assert _run(xrpl_ops.modify_nft("NFTID", "rOwner", "https://x/new.json")) == "TXHASH"
    assert calls["n"] == 3


def test_transient_malformed_then_pending_then_validated(monkeypatch):
    # Malformed bodies interleaved with a not-yet-found response still land on
    # the validated outcome within the widened confirm window.
    _stub_sign(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", _malformed_submit)

    responses = iter(
        [
            KeyError("result"),
            _Resp({"error": "txnNotFound"}),
            KeyError("result"),
            _validated(),
        ]
    )

    def scripted(self, req):
        item = next(responses)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", scripted)
    assert _run(xrpl_ops.burn_nft("NFTID", owner="rOwner")) == "TXHASH"


# --- persistent malformed bodies still fail closed as indeterminate ---


def test_persistent_malformed_poll_exhausts_and_stays_indeterminate(monkeypatch):
    _stub_sign(monkeypatch)
    _no_sleep(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", _malformed_submit)

    calls = {"n": 0}

    def always_malformed(self, req):
        calls["n"] += 1
        raise KeyError("result")

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", always_malformed)
    with pytest.raises(xrpl_ops.IndeterminateResultError):
        _run(xrpl_ops.modify_nft("NFTID", "rOwner", "https://x/new.json"))
    # Bounded: 6 confirm attempts x (1 + _MALFORMED_POLL_RETRIES) reads.
    assert calls["n"] == 6 * (1 + xrpl_ops._MALFORMED_POLL_RETRIES)


def test_non_malformed_lookup_error_is_not_retried_by_the_read_helper(monkeypatch):
    # Only the result-less-200 shape gets the read-retry budget; other lookup
    # failures keep the existing _confirm_by_hash attempt semantics.
    _stub_sign(monkeypatch)
    _no_sleep(monkeypatch)

    def submit_boom(tx, client, wallet, **k):
        raise TimeoutError("submission timed out")

    calls = {"n": 0}

    def request_boom(self, req):
        calls["n"] += 1
        raise ConnectionError("unreachable")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", submit_boom)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request_boom)
    with pytest.raises(xrpl_ops.IndeterminateResultError):
        _run(xrpl_ops.burn_nft("NFTID", owner="rOwner"))
    assert calls["n"] == 3  # one per _confirm_by_hash attempt, no extra reads


# --- the malformed-shape classifier ---


def test_malformed_classifier_matches_chained_keyerror():
    inner = KeyError("result")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert xrpl_ops._is_malformed_result_error(outer)
    assert xrpl_ops._is_malformed_result_error(KeyError("result"))
    assert not xrpl_ops._is_malformed_result_error(KeyError("other"))
    assert not xrpl_ops._is_malformed_result_error(TimeoutError("x"))


def test_malformed_classifier_walks_context_when_cause_points_elsewhere():
    # __cause__ leads to a dead end while __context__ carries the KeyError:
    # both links must be traversed at every hop.
    target = KeyError("result")
    dead_end = ValueError("unrelated")
    outer = RuntimeError("wrapped")
    outer.__cause__ = dead_end
    outer.__context__ = target
    assert xrpl_ops._is_malformed_result_error(outer)


def test_malformed_classifier_survives_exception_cycles():
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__context__ = b
    b.__context__ = a
    assert not xrpl_ops._is_malformed_result_error(a)


def test_confirm_attempts_widen_only_for_malformed_shape():
    assert xrpl_ops._confirm_attempts_for(KeyError("result")) == 6
    assert xrpl_ops._confirm_attempts_for(TimeoutError("x")) == 3


# --- sponsored paths get the same widened confirm window (fail-closed intact) ---


def _capture_confirm_attempts(monkeypatch):
    seen = {}

    async def fake_confirm(client, tx_hash, attempts=3):
        seen["attempts"] = attempts
        return None

    monkeypatch.setattr(xrpl_ops, "_confirm_by_hash", fake_confirm)
    return seen


def test_sponsored_mint_widens_confirm_on_malformed_submit(monkeypatch):
    seen = _capture_confirm_attempts(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", _malformed_submit)
    signed = _Signed("SPONSHASH")
    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", classmethod(lambda cls, blob: signed))
    sub = _run(xrpl_ops.submit_sponsored_mint(signed_tx_blob="BLOB", signed_tx_hash="SPONSHASH"))
    assert sub.state == "indeterminate"  # still fail-closed, never resubmitted
    assert seen["attempts"] == 6


def test_sponsored_burn_widens_confirm_on_malformed_submit(monkeypatch):
    seen = _capture_confirm_attempts(monkeypatch)
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", _malformed_submit)
    signed = _Signed("BURNHASH")
    monkeypatch.setattr(xrpl_ops.Transaction, "from_blob", classmethod(lambda cls, blob: signed))
    monkeypatch.setattr(xrpl_ops.config, "SIGNING_ACCOUNT", "rSourceWallet")
    monkeypatch.setattr(xrpl_ops.config, "TOKEN_ISSUER_ADDRESS", "rIssuerWallet")
    sub = _run(
        xrpl_ops.submit_sponsored_burn("memo-1", signed_tx_blob="BLOB", signed_tx_hash="BURNHASH")
    )
    assert sub.state == "indeterminate"
    assert seen["attempts"] == 6


# --- get_tx shares the same read-retry (confirm-by-hash path, market pollers) ---


def test_get_tx_retries_malformed_then_returns(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def flaky_then_ok(self, req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("result")
        return _Resp({"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}})

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", flaky_then_ok)
    result = _run(xrpl_ops.get_tx("HASH"))
    assert result.get("validated")
    assert calls["n"] == 2


def test_get_tx_persistent_malformed_raises_after_budget(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def always_malformed(self, req):
        calls["n"] += 1
        raise KeyError("result")

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", always_malformed)
    with pytest.raises(KeyError):
        _run(xrpl_ops.get_tx("HASH"))
    assert calls["n"] == 1 + xrpl_ops._MALFORMED_POLL_RETRIES


def test_get_tx_other_errors_propagate_immediately(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def request_boom(self, req):
        calls["n"] += 1
        raise ConnectionError("unreachable")

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request_boom)
    with pytest.raises(ConnectionError):
        _run(xrpl_ops.get_tx("HASH"))
    assert calls["n"] == 1
