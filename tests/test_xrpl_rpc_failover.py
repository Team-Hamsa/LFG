# JSON-RPC endpoint failover (#493 follow-up). On 2026-09-13/14 the single
# public endpoint s1.ripple.com answered `tooBusy` during a burst of economy
# ops, _submit_and_confirm raised IndeterminateResultError (its confirm-by-hash
# read hit tooBusy too) and users lost assets. These tests pin the failover
# client's rules with a stubbed per-URL transport — no network.

import asyncio
import concurrent.futures
import logging

import httpx
import pytest
from xrpl.asyncio.clients import XRPLRequestFailureException
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Ledger, SubmitOnly, Tx
from xrpl.models.response import Response, ResponseStatus
from xrpl.models.transactions import Payment
from xrpl.transaction import sign, submit_and_wait
from xrpl.wallet import Wallet

from lfg_core import config, xrpl_ops, xrpl_rpc

A = "https://a.example/"
B = "https://b.example/"
C = "https://c.example/"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sync(fn, *args, **kwargs):
    """Call a SYNC xrpl-py entry point in a worker thread. SyncClient.request
    and the sync submit_and_wait use asyncio.run(), which on the main thread
    leaves the policy with no current loop and breaks later tests that call
    asyncio.get_event_loop() (the webapp suites)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args, **kwargs).result()


def _ok(result: dict) -> Response:
    return Response(status=ResponseStatus.SUCCESS, result=result)


def _err(code: str) -> Response:
    return Response(status=ResponseStatus.ERROR, result={"error": code})


class _Transport:
    """Per-URL scripted transport. Each URL maps to a list of outcomes consumed
    in order (the last one repeats); an outcome is a Response, an exception
    instance (raised), or a callable(payload) -> Response."""

    def __init__(self, script: dict) -> None:
        self.script = {url: list(outcomes) for url, outcomes in script.items()}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, payload: dict, timeout: float) -> Response:
        self.calls.append((url, payload))
        outcomes = self.script[url]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(payload)
        return outcome

    @property
    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


@pytest.fixture(autouse=True)
def _fresh_cooldowns(monkeypatch):
    xrpl_rpc.reset_cooldowns()
    clock = {"now": 1000.0}
    monkeypatch.setattr(xrpl_rpc, "_monotonic", lambda: clock["now"])
    yield clock
    xrpl_rpc.reset_cooldowns()


def _install(monkeypatch, script: dict) -> _Transport:
    transport = _Transport(script)
    monkeypatch.setattr(xrpl_rpc, "_post_json_rpc", transport)
    return transport


# --- soft (server-capacity / sync) errors fail over ---


@pytest.mark.parametrize("code", sorted(xrpl_rpc.SOFT_ERROR_CODES))
def test_soft_error_fails_over_to_next_url(monkeypatch, code):
    t = _install(monkeypatch, {A: [_err(code)], B: [_ok({"ledger_index": 7})]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    resp = _sync(client.request, Ledger(ledger_index="validated"))
    assert resp.is_successful() and resp.result == {"ledger_index": 7}
    assert t.urls == [A, B]


def test_soft_error_set_is_exactly_the_named_capacity_and_sync_codes():
    assert xrpl_rpc.SOFT_ERROR_CODES == frozenset(
        {
            "tooBusy",
            "slowDown",
            "noCurrent",
            "noClosed",
            "noNetwork",
            "notSynced",
            "notReady",
            "amendmentBlocked",
            "unlBlocked",
            "failedToForward",
            "connectionError",
            "requestError",
            "timeout",
            "invalidResponse",
        }
    )


# --- real answers never fail over ---


@pytest.mark.parametrize(
    "code", ["txnNotFound", "actNotFound", "invalidParams", "internal", "unknownCmd", "highFee"]
)
def test_non_soft_error_is_a_real_answer(monkeypatch, code):
    t = _install(monkeypatch, {A: [_err(code)], B: [_ok({})]})
    resp = _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, Tx(transaction="AB" * 32))
    assert not resp.is_successful() and resp.result["error"] == code
    assert t.urls == [A]


@pytest.mark.parametrize(
    "engine", ["tecNO_DST", "temMALFORMED", "tefPAST_SEQ", "telINSUF_FEE_P", "terQUEUED"]
)
def test_transaction_engine_result_never_fails_over(monkeypatch, engine):
    submit_result = {"engine_result": engine, "engine_result_message": "x"}
    t = _install(monkeypatch, {A: [_ok(submit_result)], B: [_ok({"engine_result": "tesSUCCESS"})]})
    resp = _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, SubmitOnly(tx_blob="DEADBEEF"))
    assert resp.result["engine_result"] == engine
    assert t.urls == [A]


def test_engine_result_carried_in_an_error_response_is_not_failed_over(monkeypatch):
    # rippled can report a tx-level rejection alongside an `error` field; only
    # the named server-state codes move on.
    rejected = Response(
        status=ResponseStatus.ERROR,
        result={"error": "invalidTransaction", "engine_result": "temBAD_FEE"},
    )
    t = _install(monkeypatch, {A: [rejected], B: [_ok({})]})
    resp = _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, SubmitOnly(tx_blob="DEADBEEF"))
    assert resp is rejected
    assert t.urls == [A]


# --- transport failures fail over ---


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        XRPLRequestFailureException({"error": 503, "error_message": "Server is overloaded"}),
        KeyError("result"),
    ],
)
def test_transport_error_fails_over(monkeypatch, exc):
    t = _install(monkeypatch, {A: [exc], B: [_ok({"ok": True})]})
    resp = _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, Ledger(ledger_index="validated"))
    assert resp.result == {"ok": True}
    assert t.urls == [A, B]


def test_programming_error_is_not_swallowed_as_transport(monkeypatch):
    t = _install(monkeypatch, {A: [ValueError("bug")], B: [_ok({})]})
    with pytest.raises(ValueError, match="bug"):
        _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, Ledger(ledger_index="validated"))
    assert t.urls == [A]


# --- all URLs fail: the LAST error surfaces unchanged ---


def test_all_fail_with_soft_errors_returns_last_response(monkeypatch):
    last = _err("noCurrent")
    t = _install(monkeypatch, {A: [_err("tooBusy")], B: [_err("slowDown")], C: [last]})
    resp = _sync(xrpl_rpc.FailoverJsonRpcClient([A, B, C]).request, Tx(transaction="AB" * 32))
    assert resp is last
    assert t.urls == [A, B, C]


def test_all_fail_last_exception_is_raised_as_is(monkeypatch):
    last = KeyError("result")
    _install(monkeypatch, {A: [_err("tooBusy")], B: [last]})
    with pytest.raises(KeyError) as info:
        _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, Tx(transaction="AB" * 32))
    assert info.value is last
    # the #385 malformed-body classifier still recognises what surfaces
    assert xrpl_ops._is_malformed_result_error(info.value)


def test_all_fail_last_soft_response_wins_over_earlier_exception(monkeypatch):
    last = _err("tooBusy")
    _install(monkeypatch, {A: [httpx.ConnectError("x")], B: [last]})
    assert _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, Tx(transaction="AB" * 32)) is last


# --- cooldown ---


def test_failed_url_cools_down_and_is_tried_last(monkeypatch, _fresh_cooldowns):
    t = _install(
        monkeypatch,
        {A: [_err("tooBusy"), _ok({"from": "A"})], B: [_ok({"from": "B"})]},
    )
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    _sync(client.request, Ledger(ledger_index="validated"))
    assert t.urls == [A, B]
    t.calls.clear()
    # still cooling: B first, A never touched
    assert _sync(client.request, Ledger(ledger_index="validated")).result == {"from": "B"}
    assert t.urls == [B]
    # the cooldown is module-level: a fresh client honours it too
    t.calls.clear()
    _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, Ledger(ledger_index="validated"))
    assert t.urls == [B]


def test_cooldown_expires(monkeypatch, _fresh_cooldowns):
    t = _install(monkeypatch, {A: [_err("tooBusy"), _ok({"from": "A"})], B: [_ok({"from": "B"})]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    _sync(client.request, Ledger(ledger_index="validated"))
    _fresh_cooldowns["now"] += xrpl_rpc.COOLDOWN_SECONDS + 0.1
    t.calls.clear()
    assert _sync(client.request, Ledger(ledger_index="validated")).result == {"from": "A"}
    assert t.urls == [A]


def test_all_cooling_still_tries_in_configured_order(monkeypatch, _fresh_cooldowns):
    t = _install(monkeypatch, {A: [_err("tooBusy"), _ok({"from": "A"})], B: [_err("tooBusy")]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    _sync(client.request, Ledger(ledger_index="validated"))  # both now cooling
    t.calls.clear()
    assert _sync(client.request, Ledger(ledger_index="validated")).result == {"from": "A"}
    assert t.urls == [A]


def test_cooling_urls_follow_available_ones_in_configured_order(monkeypatch, _fresh_cooldowns):
    t = _install(
        monkeypatch,
        {
            A: [_err("tooBusy"), _err("tooBusy")],
            B: [_err("tooBusy"), _err("tooBusy")],
            C: [_ok({})],
        },
    )
    client = xrpl_rpc.FailoverJsonRpcClient([A, B, C])
    _sync(client.request, Ledger(ledger_index="validated"))  # A, B cool down; C answers
    t.calls.clear()
    _sync(client.request, Ledger(ledger_index="validated"))
    assert t.urls == [C]


def test_success_clears_cooldown(monkeypatch, _fresh_cooldowns):
    t = _install(
        monkeypatch,
        {A: [_err("tooBusy"), _ok({"from": "A"})], B: [_err("tooBusy"), _ok({"from": "B"})]},
    )
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    _sync(client.request, Ledger(ledger_index="validated"))  # both cooling
    _sync(client.request, Ledger(ledger_index="validated"))  # A answers -> A cleared
    t.calls.clear()
    assert _sync(client.request, Ledger(ledger_index="validated")).result == {"from": "A"}
    assert t.urls == [A]


def test_real_answer_does_not_cool_down(monkeypatch):
    t = _install(monkeypatch, {A: [_err("txnNotFound"), _ok({"from": "A"})], B: [_ok({})]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    _sync(client.request, Tx(transaction="AB" * 32))
    t.calls.clear()
    _sync(client.request, Ledger(ledger_index="validated"))
    assert t.urls == [A]


# --- logging ---


def test_failover_logs_warning_with_method_and_url_but_no_blob(monkeypatch, caplog):
    blob = "1200002280000000" + "F" * 40
    _install(monkeypatch, {A: [_err("tooBusy")], B: [_ok({"engine_result": "tesSUCCESS"})]})
    with caplog.at_level(logging.WARNING, logger="lfg_core.xrpl_rpc"):
        _sync(xrpl_rpc.FailoverJsonRpcClient([A, B]).request, SubmitOnly(tx_blob=blob))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "submit" in warnings[0] and A in warnings[0] and "tooBusy" in warnings[0]
    assert blob not in caplog.text


# --- signed-blob resubmission safety, end to end through submit_and_wait ---


def test_submit_and_wait_resends_the_identical_signed_blob(monkeypatch):
    wallet = Wallet.create()
    signed = sign(
        Payment(
            account=wallet.classic_address,
            destination="rrrrrrrrrrrrrrrrrrrrrhoLvTp",
            amount="1",
            fee="12",
            sequence=5,
            last_ledger_sequence=100,
            source_tag=config.SOURCE_TAG,
        ),
        wallet,
    )
    tx_hash = signed.get_hash()

    def by_method(payload: dict) -> Response:
        method = payload["method"]
        if method == "submit":
            return _ok({"engine_result": "tesSUCCESS", "engine_result_message": "ok"})
        if method == "ledger":
            return _ok({"ledger_index": 50})
        if method == "tx":
            return _ok(
                {"hash": tx_hash, "validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}
            )
        raise AssertionError(f"unexpected method {method}")

    t = _install(monkeypatch, {A: [_err("tooBusy")], B: [by_method]})
    monkeypatch.setattr("xrpl.asyncio.transaction.reliable_submission._LEDGER_CLOSE_TIME", 0)
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    resp = _sync(submit_and_wait, signed, client, None, autofill=False)
    assert resp.result["hash"] == tx_hash

    submits = [(url, p) for url, p in t.calls if p["method"] == "submit"]
    assert [url for url, _ in submits] == [A, B]
    blobs = {p["params"][0]["tx_blob"] for _, p in submits}
    assert len(blobs) == 1  # never re-autofilled or re-signed
    # nothing but submit/ledger/tx was sent: no autofill reads (account_info/fee)
    assert {p["method"] for _, p in t.calls} <= {"submit", "ledger", "tx"}


def test_async_client_fails_over(monkeypatch):
    t = _install(monkeypatch, {A: [httpx.ConnectTimeout("x")], B: [_ok({"ledger_index": 9})]})
    client = xrpl_rpc.AsyncFailoverJsonRpcClient([A, B])
    resp = _run(client.request(Ledger(ledger_index="validated")))
    assert resp.result == {"ledger_index": 9}
    assert t.urls == [A, B]


# --- construction / factory ---


def test_client_is_a_json_rpc_client_with_primary_url():
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    assert isinstance(client, JsonRpcClient)
    assert client.url == A
    assert client.urls == (A, B)


def test_client_requires_at_least_one_url():
    with pytest.raises(ValueError):
        xrpl_rpc.FailoverJsonRpcClient([])


def test_factories_use_configured_url_list(monkeypatch):
    monkeypatch.setattr(config, "JSON_RPC_URLS", (B, C))
    sync_client = xrpl_ops.rpc_client()
    async_client = xrpl_ops.async_rpc_client()
    assert isinstance(sync_client, xrpl_rpc.FailoverJsonRpcClient)
    assert isinstance(async_client, xrpl_rpc.AsyncFailoverJsonRpcClient)
    assert sync_client.urls == (B, C) and async_client.urls == (B, C)


def test_restriction_filters_default_factory_urls_preserving_order(monkeypatch):
    monkeypatch.setattr(config, "JSON_RPC_URLS", (A, B, C))
    xrpl_rpc.restrict_to([C, A])
    assert xrpl_rpc.endpoint_restriction() == frozenset({A, C})
    assert xrpl_ops.rpc_client().urls == (A, C)
    assert xrpl_ops.async_rpc_client().urls == (A, C)
    # an explicit url list is never filtered
    assert xrpl_ops.rpc_client(urls=[B]).urls == (B,)
    assert xrpl_ops.async_rpc_client(urls=[B, C]).urls == (B, C)
    xrpl_rpc.clear_restriction()
    assert xrpl_rpc.endpoint_restriction() is None
    assert xrpl_ops.rpc_client().urls == (A, B, C)


def test_restriction_that_excludes_everything_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "JSON_RPC_URLS", (A, B))
    xrpl_rpc.restrict_to([C])
    with pytest.raises(ValueError):
        xrpl_ops.rpc_client()


def test_factories_accept_an_explicit_url_list(monkeypatch):
    monkeypatch.setattr(config, "JSON_RPC_URLS", (B, C))
    assert xrpl_ops.rpc_client(urls=[A]).urls == (A,)
    assert xrpl_ops.async_rpc_client(urls=[A, C]).urls == (A, C)


# --- config: ordered, de-duplicated URL list ---


def test_url_list_mainnet_defaults_primary_first_deduplicated():
    urls = config.json_rpc_urls("https://s1.ripple.com:51234/", None, network="mainnet")
    assert urls == (
        "https://s1.ripple.com:51234/",
        "https://xrplcluster.com/",
        "https://s2.ripple.com:51234/",
    )


def test_url_list_mainnet_pinned_primary():
    urls = config.json_rpc_urls("https://xrplcluster.com", "", network="mainnet")
    assert urls == (
        "https://xrplcluster.com",
        "https://s2.ripple.com:51234/",
        "https://s1.ripple.com:51234/",
    )


def test_url_list_testnet_defaults():
    urls = config.json_rpc_urls("https://s.altnet.rippletest.net:51234/", None, network="testnet")
    assert urls == ("https://s.altnet.rippletest.net:51234/", "https://testnet.xrpl-labs.com/")


def test_url_list_explicit_fallbacks_replace_defaults():
    urls = config.json_rpc_urls(
        "https://primary.example/",
        " https://b.example/ ,,https://PRIMARY.example ,https://c.example/,https://b.example",
        network="mainnet",
    )
    assert urls == ("https://primary.example/", "https://b.example/", "https://c.example/")


def test_url_list_unknown_network_gets_no_cross_chain_defaults():
    assert config.json_rpc_urls("https://devnet.example/", None, network="devnet") == (
        "https://devnet.example/",
    )


# --- the #493 incident shape, through xrpl_ops._submit_and_confirm ---


def test_busy_primary_no_longer_strands_a_submit_as_indeterminate(monkeypatch, tmp_path):
    """2026-09-14: the only endpoint said tooBusy to the submit AND to the
    confirm-by-hash read, so the op raised IndeterminateResultError. With a
    fallback, the one signed blob is resent and the hash confirms."""
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "0")
    monkeypatch.setenv("XRPL_SUBMISSION_LOCK_DIR", str(tmp_path))
    wallet = Wallet.create()
    tx = Payment(
        account=wallet.classic_address,
        destination="rrrrrrrrrrrrrrrrrrrrrhoLvTp",
        amount="1",
        source_tag=config.SOURCE_TAG,
    )
    signed_holder = {}
    sign_calls = []

    def fake_autofill_and_sign(unsigned, client, signer):
        sign_calls.append(1)
        signed_holder["tx"] = sign(
            Payment(
                account=unsigned.account,
                destination=unsigned.destination,
                amount=unsigned.amount,
                source_tag=unsigned.source_tag,
                fee="12",
                sequence=9,
                last_ledger_sequence=200,
            ),
            signer,
        )
        return signed_holder["tx"]

    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", fake_autofill_and_sign)

    def b_endpoint(payload: dict) -> Response:
        method = payload["method"]
        if method == "submit":
            raise httpx.ReadTimeout("fallback slow on submit")
        if method == "tx":
            return _ok(
                {
                    "hash": signed_holder["tx"].get_hash(),
                    "validated": True,
                    "meta": {"TransactionResult": "tesSUCCESS"},
                }
            )
        raise AssertionError(f"unexpected method {method}")

    t = _install(monkeypatch, {A: [_err("tooBusy")], B: [b_endpoint]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    result = _run(xrpl_ops._submit_and_confirm(tx, wallet, client, "incident replay"))

    assert result is not None and result["hash"] == signed_holder["tx"].get_hash()
    assert len(sign_calls) == 1
    submits = [p["params"][0]["tx_blob"] for _, p in t.calls if p["method"] == "submit"]
    assert len(submits) == 2 and len(set(submits)) == 1


# --- duplicate submit: the primary may have relayed before failing ---


def _submit_confirm_fixture(monkeypatch, tmp_path):
    """Real one-time signing + a Payment for _submit_and_confirm replays."""
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "0")
    monkeypatch.setenv("XRPL_SUBMISSION_LOCK_DIR", str(tmp_path))
    monkeypatch.setattr("xrpl.asyncio.transaction.reliable_submission._LEDGER_CLOSE_TIME", 0)
    wallet = Wallet.create()
    tx = Payment(
        account=wallet.classic_address,
        destination="rrrrrrrrrrrrrrrrrrrrrhoLvTp",
        amount="1",
        source_tag=config.SOURCE_TAG,
    )
    state: dict = {"signs": 0}

    def fake_autofill_and_sign(unsigned, client, signer):
        state["signs"] += 1
        state["tx"] = sign(
            Payment(
                account=unsigned.account,
                destination=unsigned.destination,
                amount=unsigned.amount,
                source_tag=unsigned.source_tag,
                fee="12",
                sequence=9,
                last_ledger_sequence=200,
            ),
            signer,
        )
        return state["tx"]

    monkeypatch.setattr(xrpl_ops, "autofill_and_sign", fake_autofill_and_sign)
    return wallet, tx, state


def _validated_tx(state: dict) -> Response:
    return _ok(
        {
            "hash": state["tx"].get_hash(),
            "validated": True,
            "meta": {"TransactionResult": "tesSUCCESS"},
        }
    )


@pytest.mark.parametrize("dup_result", ["tefPAST_SEQ", "tefALREADY"])
def test_duplicate_engine_result_from_fallback_resolves_the_original_hash(
    monkeypatch, tmp_path, dup_result
):
    """Primary times out AFTER relaying; the fallback sees the same signed blob
    as a duplicate (tefPAST_SEQ / tefALREADY). submit_and_wait must poll the
    ORIGINAL hash and resolve it: no second signature, no Indeterminate."""
    wallet, tx, state = _submit_confirm_fixture(monkeypatch, tmp_path)

    def fallback(payload: dict) -> Response:
        method = payload["method"]
        if method == "submit":
            return _ok({"engine_result": dup_result, "engine_result_message": "dup"})
        if method == "ledger":
            return _ok({"ledger_index": 150})
        if method == "tx":
            assert payload["params"][0]["transaction"] == state["tx"].get_hash()
            return _validated_tx(state)
        raise AssertionError(f"unexpected method {method}")

    t = _install(monkeypatch, {A: [httpx.ReadTimeout("relayed, then timed out")], B: [fallback]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    result = _run(xrpl_ops._submit_and_confirm(tx, wallet, client, "duplicate replay"))

    assert result is not None and result["hash"] == state["tx"].get_hash()
    assert state["signs"] == 1
    submits = [p["params"][0]["tx_blob"] for _, p in t.calls if p["method"] == "submit"]
    assert len(submits) == 2 and len(set(submits)) == 1
    polled = {p["params"][0]["transaction"] for _, p in t.calls if p["method"] == "tx"}
    assert polled == {state["tx"].get_hash()}


def test_duplicate_submit_whose_poll_fails_resolves_via_confirm_by_hash(monkeypatch, tmp_path):
    """Same duplicate, but submit_and_wait's own poll dies on every endpoint:
    _confirm_by_hash must then look up the ORIGINAL hash and resolve it."""
    wallet, tx, state = _submit_confirm_fixture(monkeypatch, tmp_path)
    seen = {"tx": 0}

    def fallback(payload: dict) -> Response:
        method = payload["method"]
        if method == "submit":
            return _ok({"engine_result": "tefPAST_SEQ", "engine_result_message": "dup"})
        if method == "ledger":
            return _ok({"ledger_index": 150})
        if method == "tx":
            assert payload["params"][0]["transaction"] == state["tx"].get_hash()
            seen["tx"] += 1
            if seen["tx"] == 1:  # submit_and_wait's own poll
                raise httpx.ReadTimeout("poll lost")
            return _validated_tx(state)
        raise AssertionError(f"unexpected method {method}")

    t = _install(monkeypatch, {A: [_err("tooBusy")], B: [fallback]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    result = _run(xrpl_ops._submit_and_confirm(tx, wallet, client, "duplicate confirm"))

    assert result is not None and result["hash"] == state["tx"].get_hash()
    assert state["signs"] == 1
    assert seen["tx"] >= 2  # the poll failed; confirm-by-hash resolved
    submits = [p["params"][0]["tx_blob"] for _, p in t.calls if p["method"] == "submit"]
    assert len(set(submits)) == 1


# --- HTTP status handling in the real per-URL exchange ---


def _http(monkeypatch, handlers: dict) -> list:
    """Route the real _post_json_rpc through httpx.MockTransport. `handlers`
    maps URL -> (status, body); a dict body is JSON, a str body is text."""
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        status, body = handlers[url]
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    monkeypatch.setattr(xrpl_rpc, "_http_transport", httpx.MockTransport(handler))
    return seen


_OK_BODY = {"result": {"status": "success", "ledger_index": 5}}


@pytest.mark.parametrize(
    "status,body",
    [
        (503, {"result": {"status": "error", "error": "internal"}}),
        (500, {"result": {"status": "error", "error": "internal"}}),
        (502, "<html>Bad Gateway</html>"),
        (504, "gateway timeout"),
        (429, "Too Many Requests"),
        (403, "Forbidden"),
        (404, "Not Found"),
        (408, "Request Timeout"),
        (401, "Unauthorized"),
        (301, "moved"),
    ],
)
def test_endpoint_level_http_status_fails_over(monkeypatch, status, body):
    seen = _http(monkeypatch, {A: (status, body), B: (200, _OK_BODY)})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    out = _sync(client.request, Ledger(ledger_index="validated"))
    assert out.is_successful() and out.result["ledger_index"] == 5
    assert seen == [A, B]


def test_http_400_text_is_a_real_bad_request_not_failed_over(monkeypatch):
    seen = _http(monkeypatch, {A: (400, "params unparseable"), B: (200, _OK_BODY)})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    with pytest.raises(XRPLRequestFailureException) as info:
        _sync(client.request, Ledger(ledger_index="validated"))
    assert info.value.error == 400
    assert seen == [A]


def test_http_400_json_is_returned_as_the_answer(monkeypatch):
    body = {"result": {"status": "error", "error": "invalidParams"}}
    seen = _http(monkeypatch, {A: (400, body), B: (200, _OK_BODY)})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    out = _sync(client.request, Ledger(ledger_index="validated"))
    assert not out.is_successful() and out.result["error"] == "invalidParams"
    assert seen == [A]


@pytest.mark.parametrize(
    "body",
    [[], {"result": None}, {"result": "oops"}, [{"result": {"status": "success"}}]],
    ids=["array", "result-null", "result-string", "array-of-envelopes"],
)
def test_http_2xx_wrong_json_shape_fails_over(monkeypatch, body):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url == A:
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=_OK_BODY)

    monkeypatch.setattr(xrpl_rpc, "_http_transport", httpx.MockTransport(handler))
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    assert _sync(client.request, Ledger(ledger_index="validated")).is_successful()
    assert seen == [A, B]


def test_http_2xx_wrong_json_shape_on_last_endpoint_raises_request_failure(monkeypatch):
    monkeypatch.setattr(
        xrpl_rpc,
        "_http_transport",
        httpx.MockTransport(lambda request: httpx.Response(200, json={"result": None})),
    )
    client = xrpl_rpc.FailoverJsonRpcClient([A])
    with pytest.raises(XRPLRequestFailureException) as info:
        _sync(client.request, Ledger(ledger_index="validated"))
    assert info.value.error == 200


def test_indeterminate_submit_through_failover_still_carries_signed_hash(monkeypatch, tmp_path):
    """#497: when every endpoint is busy for the submit AND the confirm, the
    IndeterminateResultError still names the one signed hash."""
    wallet, tx, state = _submit_confirm_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(xrpl_ops.asyncio, "sleep", _no_sleep)
    _install(monkeypatch, {A: [_err("tooBusy")], B: [httpx.ConnectError("down")]})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    with pytest.raises(xrpl_ops.IndeterminateResultError) as info:
        _run(xrpl_ops._submit_and_confirm(tx, wallet, client, "all busy"))
    assert info.value.tx_hash == state["tx"].get_hash()
    assert state["signs"] == 1


async def _no_sleep(_seconds):
    return None


def test_http_2xx_non_json_still_fails_over(monkeypatch):
    seen = _http(monkeypatch, {A: (200, "<html>maintenance</html>"), B: (200, _OK_BODY)})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    assert _sync(client.request, Ledger(ledger_index="validated")).is_successful()
    assert seen == [A, B]


def test_all_fail_last_5xx_json_body_surfaces_as_the_response(monkeypatch):
    # Clio answers a plain-HTTP tooBusy as 503 + JSON; with nowhere left to go
    # the caller gets that Response, exactly as a single-endpoint client did.
    body = {"result": {"status": "error", "error": "tooBusy"}}
    _http(monkeypatch, {A: (502, "bad gateway"), B: (503, body)})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    out = _sync(client.request, Ledger(ledger_index="validated"))
    assert not out.is_successful() and out.result["error"] == "tooBusy"


def test_all_fail_last_5xx_text_raises_request_failure(monkeypatch):
    busy = {"result": {"status": "error", "error": "tooBusy"}}
    _http(monkeypatch, {A: (503, busy), B: (502, "bad gateway")})
    client = xrpl_rpc.FailoverJsonRpcClient([A, B])
    with pytest.raises(XRPLRequestFailureException) as info:
        _sync(client.request, Ledger(ledger_index="validated"))
    assert info.value.error == 502


# --- busy retry rounds (2026-09-19: every endpoint shed load at once) ---


@pytest.fixture
def _busy_rounds(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(xrpl_rpc, "BUSY_RETRY_BACKOFF_SECONDS", (1.0, 2.5))
    monkeypatch.setattr(xrpl_rpc, "_sleep", fake_sleep)
    return sleeps


def test_all_busy_pass_is_retried_after_backoff(monkeypatch, _busy_rounds):
    t = _install(
        monkeypatch,
        {A: [_err("tooBusy"), _ok({"ledger_index": 9})], B: [_err("tooBusy")]},
    )
    resp = _run(xrpl_rpc.failover_request([A, B], Ledger(ledger_index="validated")))
    assert resp.is_successful() and resp.result == {"ledger_index": 9}
    assert _busy_rounds == [1.0]
    assert t.urls == [A, B, A]


def test_http_503_counts_as_busy(monkeypatch, _busy_rounds):
    busy = xrpl_rpc.HTTPStatusFailure(503, "Server is overloaded", None)
    _install(monkeypatch, {A: [busy, _ok({"ledger_index": 3})]})
    resp = _run(xrpl_rpc.failover_request([A], Ledger(ledger_index="validated")))
    assert resp.result == {"ledger_index": 3}
    assert _busy_rounds == [1.0]


def test_busy_rounds_are_bounded_and_surface_the_last_answer(monkeypatch, _busy_rounds):
    t = _install(monkeypatch, {A: [_err("tooBusy")], B: [_err("slowDown")]})
    resp = _run(xrpl_rpc.failover_request([A, B], Ledger(ledger_index="validated")))
    assert not resp.is_successful() and resp.result["error"] == "slowDown"
    assert _busy_rounds == [1.0, 2.5]
    assert len(t.calls) == 6


def test_transport_failure_in_a_pass_is_not_retried(monkeypatch, _busy_rounds):
    t = _install(monkeypatch, {A: [_err("tooBusy")], B: [httpx.ConnectTimeout("slow")]})
    with pytest.raises(httpx.ConnectTimeout):
        _run(xrpl_rpc.failover_request([A, B], Ledger(ledger_index="validated")))
    assert _busy_rounds == []
    assert t.urls == [A, B]


def test_real_answer_is_never_retried(monkeypatch, _busy_rounds):
    t = _install(monkeypatch, {A: [_err("txnNotFound")]})
    resp = _run(xrpl_rpc.failover_request([A], Tx(transaction="AB" * 32)))
    assert resp.result["error"] == "txnNotFound"
    assert _busy_rounds == [] and len(t.calls) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (1.0, 2.5)),
        ("", ()),
        ("0.5, 2", (0.5, 2.0)),
        ("abc", (1.0, 2.5)),
        ("-1", (1.0, 2.5)),
    ],
)
def test_parse_busy_backoff(raw, expected):
    assert xrpl_rpc.parse_busy_backoff(raw) == expected
