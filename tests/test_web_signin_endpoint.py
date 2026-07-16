# tests/test_web_signin_endpoint.py
# Standalone web surface (spec 2026-07-16): client-callable wallet signin.
# POST /api/web/signin creates a XUMM SignIn payload (rate-limited per IP);
# GET /api/web/signin/{uuid} polls it and, on signed, bootstraps a
# platform="web" session where the wallet IS the platform_user_id.
import asyncio
import json
import os

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

import lfg_service.app as app
from lfg_service.app import verify_session_token

WALLET = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, body=None, headers=None, match=None, remote="1.2.3.4"):
        self._body = body or {}
        self.headers = headers or {}
        self.match_info = match or {}
        self.remote = remote
        self._store: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def setup_function(_fn):
    app.web_signin_payloads.clear()
    app._web_signin_hits.clear()


def _fake_create(captured=None):
    async def fake(return_url=None):
        if captured is not None:
            captured.append(return_url)
        return {"uuid": "u-1", "xumm_url": "https://xumm.app/sign/u-1"}

    return fake


def test_start_creates_payload(monkeypatch):
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    resp = _run(app.handle_web_signin_start(_Req()))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"uuid": "u-1", "signin_link": "https://xumm.app/sign/u-1"}
    assert "u-1" in app.web_signin_payloads


def test_start_passes_allowlisted_origin_as_return_url(monkeypatch):
    captured: list = []
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create(captured))
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", ("https://build.letseffinggo.com",))
    _run(app.handle_web_signin_start(_Req(headers={"Origin": "https://build.letseffinggo.com"})))
    assert captured == [
        {"app": "https://build.letseffinggo.com", "web": "https://build.letseffinggo.com"}
    ]


def test_start_foreign_origin_gets_no_return_url(monkeypatch):
    captured: list = []
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create(captured))
    monkeypatch.setattr(app.config, "WEB_ALLOWED_ORIGINS", ("https://build.letseffinggo.com",))
    _run(app.handle_web_signin_start(_Req(headers={"Origin": "https://evil.example"})))
    assert captured == [None]


def test_start_rate_limited_per_ip(monkeypatch):
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    for _ in range(app.WEB_SIGNIN_RATE_MAX):
        assert _run(app.handle_web_signin_start(_Req())).status == 200
    resp = _run(app.handle_web_signin_start(_Req()))
    assert resp.status == 429
    assert json.loads(resp.body)["code"] == "rate_limited"
    # a different IP is unaffected
    other = _Req(remote="5.6.7.8")
    assert _run(app.handle_web_signin_start(other)).status == 200


def test_start_rate_limit_keys_on_rightmost_forwarded_for(monkeypatch):
    # Behind the funnel every peer addr is localhost and tailscaled APPENDS the
    # real client to X-Forwarded-For — so only the RIGHTMOST entry is trusted.
    # A caller-supplied leftmost value must not shard the bucket: rotating
    # spoofed prefixes with the same trusted tail still trips the limit.
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    for i in range(app.WEB_SIGNIN_RATE_MAX):
        req = _Req(headers={"X-Forwarded-For": f"spoof-{i}, 9.9.9.9"}, remote="127.0.0.1")
        assert _run(app.handle_web_signin_start(req)).status == 200
    req = _Req(headers={"X-Forwarded-For": "spoof-x, 9.9.9.9"}, remote="127.0.0.1")
    assert _run(app.handle_web_signin_start(req)).status == 429


def test_stale_rate_limit_ips_are_pruned(monkeypatch):
    # The hits dict must not grow forever across distinct client IPs: a fresh
    # request's prune pass drops IPs whose window has fully expired.
    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", _fake_create())
    app._web_signin_hits["203.0.113.7"] = [0.0]  # far in the past
    _run(app.handle_web_signin_start(_Req()))
    assert "203.0.113.7" not in app._web_signin_hits


def test_start_xumm_unreachable_502(monkeypatch):
    async def fake(return_url=None):
        return None

    monkeypatch.setattr(app.xumm_ops, "create_signin_payload", fake)
    resp = _run(app.handle_web_signin_start(_Req()))
    assert resp.status == 502


def test_status_signed_issues_web_session(monkeypatch):
    app.web_signin_payloads["u-2"] = {"created_at": 0}
    linked: dict = {}
    tokens: dict = {}

    async def fake_status(uuid):
        return {
            "signed": True,
            "account": WALLET,
            "expired": False,
            "opened": True,
            "user_token": "push-tok",
        }

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app.identity_store, "handle_for_wallet", lambda w: None)
    monkeypatch.setattr(
        app.identity_store,
        "link",
        lambda p, uid, name, wallet: linked.update(p=p, uid=uid, name=name, w=wallet) or True,
    )
    monkeypatch.setattr(
        app.identity_store,
        "set_user_token",
        lambda p, uid, tok: tokens.update({(p, uid): tok}),
    )
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-2"})))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["state"] == "signed"
    assert body["wallet"] == WALLET
    decoded = verify_session_token(body["session_token"])
    assert decoded is not None
    assert decoded["platform"] == "web"
    assert decoded["id"] == WALLET
    assert linked == {"p": "web", "uid": WALLET, "name": body["user"]["username"], "w": WALLET}
    assert tokens[("web", WALLET)] == "push-tok"
    assert "u-2" not in app.web_signin_payloads


def test_status_signed_prefers_existing_handle(monkeypatch):
    app.web_signin_payloads["u-h"] = {"created_at": 0}

    async def fake_status(uuid):
        return {"signed": True, "account": WALLET, "expired": False, "opened": True}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    monkeypatch.setattr(app.identity_store, "handle_for_wallet", lambda w: "alice")
    monkeypatch.setattr(app.identity_store, "link", lambda p, uid, name, wallet: True)
    monkeypatch.setattr(app.identity_store, "set_user_token", lambda p, uid, tok: None)
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-h"})))
    assert json.loads(resp.body)["user"]["username"] == "alice"


def test_status_unknown_uuid_404():
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "nope"})))
    assert resp.status == 404


def test_status_pending_and_opened(monkeypatch):
    app.web_signin_payloads["u-4"] = {"created_at": 0}

    async def fake_status(uuid):
        return {"signed": False, "account": None, "expired": False, "opened": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-4"})))
    assert json.loads(resp.body)["state"] == "pending"

    async def fake_opened(uuid):
        return {"signed": False, "account": None, "expired": False, "opened": True}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_opened)
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-4"})))
    assert json.loads(resp.body)["state"] == "opened"


def test_status_expired_deletes_record(monkeypatch):
    app.web_signin_payloads["u-3"] = {"created_at": 0}

    async def fake_status(uuid):
        return {"signed": False, "account": None, "expired": True, "opened": False}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-3"})))
    assert json.loads(resp.body)["state"] == "expired"
    assert "u-3" not in app.web_signin_payloads


def test_status_invalid_account_not_signed(monkeypatch):
    # A malformed signer address must never mint a session.
    app.web_signin_payloads["u-5"] = {"created_at": 0}

    async def fake_status(uuid):
        return {"signed": True, "account": "not-an-address", "expired": False, "opened": True}

    monkeypatch.setattr(app.xumm_ops, "get_payload_status", fake_status)
    resp = _run(app.handle_web_signin_status(_Req(match={"payload_uuid": "u-5"})))
    body = json.loads(resp.body)
    assert body.get("state") != "signed"
    assert "session_token" not in body
