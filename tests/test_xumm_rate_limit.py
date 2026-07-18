# XUMM 429 handling (2026-07-17 incident): detect rate limiting explicitly,
# cool off instead of retrying into it, serve payload status from cache, and
# cap SignIn payload creation per user at the service.

import asyncio
import os
import time

os.environ.setdefault("BUNNY_PULL_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")

import lfg_service.app as app
from lfg_core import xumm_ops
from lfg_service.app import make_session_token
from surfaces._client.client import LFGServiceClient
from surfaces._client.errors import ServiceError


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


_CREATE_OK = {
    "refs": {"qr_png": "q"},
    "next": {"always": "n"},
    "uuid": "11111111-2222-3333-4444-555555555555",
    "pushed": False,
}

_UUID = "11111111-2222-3333-4444-555555555555"


# --- create-side 429 handling ------------------------------------------------


def test_create_429_returns_none_without_tokenless_retry(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json)
        return _Resp(status=429, body={"error": {"code": 429}})

    monkeypatch.setattr(xumm_ops.requests, "post", fake_post)
    result = _run(xumm_ops._create_xumm_payload({"TransactionType": "SignIn"}, user_token="tok"))
    assert result is None
    # ONE call only: the 429 must not trigger the retry-without-token path.
    assert len(calls) == 1
    assert xumm_ops.rate_limited()


def test_create_skipped_during_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(
        xumm_ops.requests, "post", lambda *a, **k: calls.append(1) or _Resp(body=_CREATE_OK)
    )
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", time.monotonic() + 30)
    result = _run(xumm_ops._create_xumm_payload({"TransactionType": "SignIn"}))
    assert result is None
    assert calls == []  # no XUMM call spent while cooling off


def test_create_non_429_error_body_still_returns_none(monkeypatch):
    # The old failure mode: a rejected create has no "refs" and raised
    # KeyError('refs'). Now it must fail cleanly without arming the cooldown.
    monkeypatch.setattr(
        xumm_ops.requests, "post", lambda *a, **k: _Resp(status=400, body={"error": True})
    )
    result = _run(xumm_ops._create_xumm_payload({"TransactionType": "SignIn"}))
    assert result is None
    assert not xumm_ops.rate_limited()


# --- status-side caching + 429 handling --------------------------------------


def _status_body(signed=False, expired=False, opened=False):
    return {
        "meta": {"opened": opened, "signed": signed, "expired": expired},
        "response": {"account": "rXXX" if signed else None, "txid": None},
        "application": {},
    }


def test_status_terminal_state_cached(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return _Resp(body=_status_body(signed=True))

    monkeypatch.setattr(xumm_ops.requests, "get", fake_get)
    s1 = _run(xumm_ops.get_payload_status(_UUID))
    s2 = _run(xumm_ops.get_payload_status(_UUID))
    assert s1["signed"] and s2["signed"]
    assert len(calls) == 1  # second read served from cache


def test_status_429_serves_stale_cache_and_arms_cooldown(monkeypatch):
    responses = [_Resp(body=_status_body(opened=True)), _Resp(status=429)]
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return responses[len(calls) - 1]

    monkeypatch.setattr(xumm_ops.requests, "get", fake_get)
    s1 = _run(xumm_ops.get_payload_status(_UUID))
    assert s1["opened"] and not s1["signed"]
    s2 = _run(xumm_ops.get_payload_status(_UUID, force=True))  # hits the 429
    assert s2 == s1  # stale beats None
    assert xumm_ops.rate_limited()
    # Cooldown active: a further non-force call must not reach XUMM.
    s3 = _run(xumm_ops.get_payload_status(_UUID))
    assert s3 == s1
    assert len(calls) == 2


def test_status_watched_uuid_served_from_cache(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return _Resp(body=_status_body(opened=True))

    monkeypatch.setattr(xumm_ops.requests, "get", fake_get)
    _run(xumm_ops.get_payload_status(_UUID))
    xumm_ops._watched.add(_UUID)
    try:
        _run(xumm_ops.get_payload_status(_UUID))  # ws feed live: no REST call
    finally:
        xumm_ops._watched.discard(_UUID)
    assert len(calls) == 1


# --- service: sign-in creation guard ------------------------------------------


class _Req:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}
        self.match_info = {}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v

    async def json(self):
        raise ValueError("no body")


def _signin_env(monkeypatch, uuids):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)
    created = []

    async def fake_create(return_url=None):
        u = uuids[min(len(created), len(uuids) - 1)]
        created.append(u)
        return {"uuid": u, "xumm_url": f"https://xumm.app/sign/{u}"}

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    return created


def test_signin_reuses_pending_payload(monkeypatch):
    created = _signin_env(monkeypatch, ["p1", "p2"])
    token = make_session_token({"id": "901", "name": "u", "platform": "telegram"})
    r1 = _run(app.handle_signin_start(_Req(token)))
    r2 = _run(app.handle_signin_start(_Req(token)))
    assert r1.status == r2.status == 200
    assert created == ["p1"]  # second request re-served the pending payload
    assert r1.text == r2.text
    app.signin_payloads.pop("p1", None)


def test_signin_creation_rate_limited_per_user(monkeypatch):
    _signin_env(monkeypatch, ["q1", "q2", "q3", "q4"])
    token = make_session_token({"id": "902", "name": "u", "platform": "telegram"})
    statuses = []
    for _ in range(app.SIGNIN_CREATE_MAX + 1):
        resp = _run(app.handle_signin_start(_Req(token)))
        statuses.append(resp.status)
        app.signin_payloads.clear()  # defeat reuse so each call tries a create
    assert statuses[: app.SIGNIN_CREATE_MAX] == [200] * app.SIGNIN_CREATE_MAX
    assert statuses[-1] == 429


def test_signin_maps_xumm_rate_limit_to_503(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def fake_create(return_url=None):
        return None

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    monkeypatch.setattr(xumm_ops, "_rate_limited_until", time.monotonic() + 30)
    token = make_session_token({"id": "903", "name": "u", "platform": "telegram"})
    resp = _run(app.handle_signin_start(_Req(token)))
    assert resp.status == 503
    assert resp.headers["Retry-After"] == "30"


# --- shared client: no retry on deliberate back-pressure ----------------------


def test_signin_reuse_skips_signed_payload(monkeypatch):
    # A pending payload the ws watcher already saw signed must NOT be
    # re-served — that would fast-re-login the previous wallet.
    created = _signin_env(monkeypatch, ["r1", "r2"])
    token = make_session_token({"id": "904", "name": "u", "platform": "telegram"})
    r1 = _run(app.handle_signin_start(_Req(token)))
    assert r1.status == 200
    xumm_ops._STATUS_CACHE["r1"] = (
        time.monotonic(),
        {"opened": True, "signed": True, "expired": False},
    )
    r2 = _run(app.handle_signin_start(_Req(token)))
    assert r2.status == 200
    assert created == ["r1", "r2"]  # fresh payload, no reuse of the signed one
    app.signin_payloads.pop("r1", None)
    app.signin_payloads.pop("r2", None)


def test_signin_failed_create_refunds_quota(monkeypatch):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", False)

    async def fake_create(return_url=None):
        return None  # plain outage, not rate limited

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake_create)
    token = make_session_token({"id": "905", "name": "u", "platform": "telegram"})
    # Way past SIGNIN_CREATE_MAX failing attempts: each refunds its slot, so
    # the user is never locked out by an outage they didn't cause.
    for _ in range(app.SIGNIN_CREATE_MAX * 3):
        resp = _run(app.handle_signin_start(_Req(token)))
        assert resp.status == 502


def test_client_does_not_retry_rate_limiting():
    assert not LFGServiceClient._retryable(ServiceError("x", status=429))
    assert not LFGServiceClient._retryable(ServiceError("x", status=503, code="rate_limited"))
    # Plain transient 5xx (including 503 without the code) stays retryable.
    assert LFGServiceClient._retryable(ServiceError("x", status=502))
    assert LFGServiceClient._retryable(ServiceError("x", status=503))
