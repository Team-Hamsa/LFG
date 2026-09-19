# tests/test_ws_failover_payment_watch.py
# 2026-09-19 incident: a paid 3x bulk mint (30 XRP) went unseen. The payment
# watcher subscribed on wss://xrplcluster.com, which was shedding load
# (tooBusy); its one history check ran before the payment landed and the
# subscription stream never delivered the tx, so the job sat "awaiting
# payment" until the user cancelled it. In the same minute the swap-fee AMM
# quote crashed on the busy node's error answer (KeyError 'amm').
#
# These tests pin the fixes: every XRPL websocket call walks config.WS_URLS
# (primary + same-chain fallbacks) past busy/unreachable servers, the payment
# watcher re-checks history while it listens and treats an error answer as an
# error (never as "no payment"), and a mint cancel is refused while a matching
# unclaimed payment is already on-ledger.
import asyncio
import json
import time
from decimal import Decimal

import pytest
from xrpl.models.requests import AccountTx
from xrpl.models.response import Response, ResponseStatus

import lfg_core.bulk_mint_flow as bulk_mint_flow
import lfg_core.mint_flow as mint_flow
import lfg_service.app as app
from lfg_core import config, payment_ledger, xrpl_ops
from lfg_service.app import make_session_token

DEST = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
SENDER = "rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ"
BRIX_HEX = "4252495800000000000000000000000000000000"
BRIX_ISSUER = "rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px"
RIPPLE_EPOCH = 946684800


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ok(result):
    return Response(status=ResponseStatus.SUCCESS, result=result)


def _busy():
    return Response(status=ResponseStatus.ERROR, result={"error": "tooBusy"})


def _payment_entry(tx_hash, drops="30000000", when=None):
    when = time.time() if when is None else when
    return {
        "hash": tx_hash,
        "validated": True,
        "tx_json": {
            "TransactionType": "Payment",
            "Account": SENDER,
            "Destination": DEST,
            "DeliverMax": drops,
            "date": int(when - RIPPLE_EPOCH),
        },
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": drops},
    }


class _FakeWS:
    """AsyncWebsocketClient stand-in. `answer(url, request)` returns the
    Response for a request; the subscription stream is silent forever (the
    incident's failure mode) unless `stream` messages are queued."""

    def __init__(self, url, answer, contacted, stream=None):
        self.url = url
        self._answer = answer
        self._contacted = contacted
        self._stream = list(stream or [])

    async def __aenter__(self):
        self._contacted.append(self.url)
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, _req):
        return None

    async def request(self, req):
        return self._answer(self.url, req)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._stream:
            return self._stream.pop(0)
        await asyncio.sleep(3600)  # silent stream
        raise StopAsyncIteration


def _patch_ws(monkeypatch, answer, urls=("ws://primary", "ws://fallback")):
    contacted: list[str] = []
    monkeypatch.setattr(config, "WS_URLS", tuple(urls), raising=False)
    monkeypatch.setattr(config, "WS_URL", urls[0])
    monkeypatch.setattr(
        xrpl_ops, "AsyncWebsocketClient", lambda url: _FakeWS(url, answer, contacted)
    )
    return contacted


@pytest.fixture(autouse=True)
def _fast_repoll(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_REPOLL_SECONDS", 0.05, raising=False)


# ---------------------------------------------------------------------------
# config.WS_URLS
# ---------------------------------------------------------------------------


def test_ws_urls_mainnet_defaults_follow_primary_deduped():
    urls = config.ws_urls("ws://10.30.0.176:6006", None, network="mainnet")
    assert urls[0] == "ws://10.30.0.176:6006"
    assert urls[1:] == config.MAINNET_WS_FALLBACK_URLS
    # the primary is never repeated when it is also a default
    assert (
        config.ws_urls("wss://xrplcluster.com/", None, network="mainnet").count(
            "wss://xrplcluster.com"
        )
        == 0
    )
    assert len(config.ws_urls("wss://xrplcluster.com", None, network="mainnet")) == len(
        config.MAINNET_WS_FALLBACK_URLS
    )


def test_ws_urls_explicit_fallbacks_replace_defaults():
    assert config.ws_urls("ws://a", "ws://b, ws://c", network="mainnet") == (
        "ws://a",
        "ws://b",
        "ws://c",
    )


def test_ws_urls_unknown_network_gets_no_fallbacks():
    assert config.ws_urls("ws://a", None, network="devnet") == ("ws://a",)


# ---------------------------------------------------------------------------
# AMM quote rides the JSON-RPC failover pool (like the trustline lookup, #567)
# ---------------------------------------------------------------------------


class _FakeRpc:
    def __init__(self, response):
        self._response = response
        self.requests = []

    async def request(self, req):
        self.requests.append(req)
        return self._response


def _patch_rpc(monkeypatch, response):
    client = _FakeRpc(response)
    monkeypatch.setattr(xrpl_ops, "async_rpc_client", lambda urls=None: client)

    def no_ws(url):
        raise AssertionError(f"AMM quote must not use a single websocket ({url})")

    monkeypatch.setattr(xrpl_ops, "AsyncWebsocketClient", no_ws)
    return client


def test_amm_quote_uses_the_failover_pool(monkeypatch):
    amm = {"amm": {"amount": "50000000", "amount2": {"value": "5000"}, "trading_fee": 0}}
    client = _patch_rpc(monkeypatch, _ok(amm))
    cost = _run(xrpl_ops.get_amm_xrp_cost(BRIX_HEX, BRIX_ISSUER, Decimal(100)))
    assert cost is not None and cost > 0
    assert len(client.requests) == 1


def test_amm_quote_error_answer_is_none(monkeypatch):
    _patch_rpc(monkeypatch, _busy())
    assert _run(xrpl_ops.get_amm_xrp_cost(BRIX_HEX, BRIX_ISSUER, Decimal(100))) is None


# ---------------------------------------------------------------------------
# wait_for_payment
# ---------------------------------------------------------------------------


def _wait(**kw):
    return xrpl_ops.wait_for_payment(
        destination=DEST,
        expected_sender=SENDER,
        expected_amount="30",
        currency="XRP",
        **kw,
    )


def test_payment_landing_after_subscribe_is_found_on_a_silent_stream(monkeypatch):
    """The incident: history is empty at subscribe time, the payment lands a
    moment later, and the stream never delivers it."""
    landed_at = time.time() + 0.2
    tx_hash = "A" * 64

    def answer(url, req):
        assert isinstance(req, AccountTx)
        if time.time() < landed_at:
            return _ok({"transactions": []})
        return _ok({"transactions": [_payment_entry(tx_hash)]})

    _patch_ws(monkeypatch, answer, urls=("ws://primary",))
    started = time.time()
    assert _run(_wait(timeout_seconds=5, claimant="bulk:test")) is True
    assert time.time() - started < 2
    assert payment_ledger.consumed_payment(tx_hash)["claimant"] == "bulk:test"


def test_busy_history_answer_moves_to_the_next_endpoint(monkeypatch):
    """A tooBusy account_tx answer must not read as 'no payment yet'."""
    tx_hash = "B" * 64

    def answer(url, req):
        if url == "ws://primary":
            return _busy()
        return _ok({"transactions": [_payment_entry(tx_hash)]})

    contacted = _patch_ws(monkeypatch, answer)
    monkeypatch.setattr(xrpl_ops.asyncio, "sleep", _instant_sleep_factory())
    assert _run(_wait(timeout_seconds=5)) is True
    assert "ws://fallback" in contacted


def _instant_sleep_factory():
    real_sleep = asyncio.sleep

    async def fast(delay, *a, **k):
        # keep the silent stream's long sleep real (it gets cancelled); make
        # the reconnect backoff instant
        return await real_sleep(0 if delay < 60 else delay)

    return fast


# ---------------------------------------------------------------------------
# find_unclaimed_payment (the cancel guard's ledger read)
# ---------------------------------------------------------------------------


def _find(not_before=None):
    return xrpl_ops.find_unclaimed_payment(
        destination=DEST,
        expected_sender=SENDER,
        expected_amount="30",
        not_before=time.time() - 60 if not_before is None else not_before,
        currency="XRP",
    )


def test_find_unclaimed_payment_true_when_landed_and_unclaimed(monkeypatch):
    _patch_ws(monkeypatch, lambda url, req: _ok({"transactions": [_payment_entry("C" * 64)]}))
    assert _run(_find()) is True
    # a read, never a claim
    assert payment_ledger.consumed_payment("C" * 64) is None


def test_find_unclaimed_payment_false_when_already_claimed(monkeypatch):
    payment_ledger.try_consume("D" * 64, SENDER, DEST, claimant="mint:other")
    _patch_ws(monkeypatch, lambda url, req: _ok({"transactions": [_payment_entry("D" * 64)]}))
    assert _run(_find()) is False


def test_find_unclaimed_payment_false_when_nothing_landed(monkeypatch):
    _patch_ws(monkeypatch, lambda url, req: _ok({"transactions": []}))
    assert _run(_find()) is False


def test_find_unclaimed_payment_none_when_every_endpoint_fails(monkeypatch):
    _patch_ws(monkeypatch, lambda url, req: _busy())
    assert _run(_find()) is None


# ---------------------------------------------------------------------------
# cancel guard (service handlers)
# ---------------------------------------------------------------------------


class _MockRequest:
    def __init__(self, session_id: str, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = {"session_id": session_id}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _token():
    return make_session_token({"id": "55", "name": "d", "platform": "discord"})


def _xrp_mint_session():
    s = mint_flow.MintSession("55", SENDER)
    s.pay_with = "XRP"
    s.pay_amount = "10"
    s.payment_uuid = "uuid-1"
    return s


def _stub_landed(monkeypatch, verdict):
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return verdict

    monkeypatch.setattr(xrpl_ops, "find_unclaimed_payment", fake)
    monkeypatch.setattr(app, "_spawn_payload_cancel", lambda uuid: None)
    return calls


@pytest.mark.parametrize(
    ("verdict", "status", "state"),
    [
        (True, 409, mint_flow.AWAITING_PAYMENT),
        (None, 503, mint_flow.AWAITING_PAYMENT),
        (False, 200, mint_flow.CANCELLED),
    ],
)
def test_mint_cancel_checks_the_ledger_first(monkeypatch, verdict, status, state):
    calls = _stub_landed(monkeypatch, verdict)
    s = _xrp_mint_session()
    app.mint_sessions[s.id] = s
    try:
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
    finally:
        app.mint_sessions.pop(s.id, None)
    assert resp.status == status
    assert s.state == state
    assert calls and calls[0]["expected_sender"] == SENDER
    assert calls[0]["not_before"] == s.created_at - 10
    if verdict is True:
        assert json.loads(resp.body)["error"] == "payment_received"


def test_mint_cancel_without_a_payment_request_skips_the_ledger(monkeypatch):
    """No payload was ever shown, so nothing can have been paid."""
    calls = _stub_landed(monkeypatch, True)
    s = mint_flow.MintSession("55", SENDER)
    app.mint_sessions[s.id] = s
    try:
        resp = _run(app.handle_mint_cancel(_MockRequest(s.id, _token())))
    finally:
        app.mint_sessions.pop(s.id, None)
    assert resp.status == 200
    assert s.state == mint_flow.CANCELLED
    assert calls == []


def _bulk_job():
    job = bulk_mint_flow.BulkMintJob("55", SENDER, 3)
    job.pay_with = "XRP"
    job.pay_amount = "30"
    job.payment_uuid = "uuid-2"
    return job


@pytest.mark.parametrize(
    ("verdict", "status", "state"),
    [
        (True, 409, bulk_mint_flow.AWAITING_PAYMENT),
        (None, 503, bulk_mint_flow.AWAITING_PAYMENT),
        (False, 200, bulk_mint_flow.CANCELLED),
    ],
)
def test_bulk_cancel_checks_the_ledger_first(monkeypatch, verdict, status, state):
    calls = _stub_landed(monkeypatch, verdict)
    job = _bulk_job()
    app.bulk_sessions[job.id] = job
    try:
        resp = _run(app.handle_bulk_mint_cancel(_MockRequest(job.id, _token())))
    finally:
        app.bulk_sessions.pop(job.id, None)
    assert resp.status == status
    assert job.state == state
    assert calls and calls[0]["expected_amount"] == "30"
    assert calls[0]["not_before"] == job.created_at - 10
