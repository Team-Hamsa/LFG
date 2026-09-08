# Log out / disconnect wallet (P0, 2026-09-07).
# - POST /api/logout revokes the caller's session token server-side (tokens
#   are stateless HMAC, so revocation is a process-local denylist until exp).
# - POST /api/wallet/disconnect unlinks the wallet from a Discord/Telegram
#   identity (and the legacy Users row for Discord); for platform="web" the
#   wallet IS the identity, so it degrades to a logout.
import asyncio
import json
import sqlite3

import pytest

import lfg_core.user_db as user_db
import lfg_service.identity as identity
from lfg_service import app as server

WALLET = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self._store: dict = {}

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _fresh_dbs(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(identity, "DATABASE", db)
    monkeypatch.setattr(user_db, "DATABASE", db)
    identity.ensure_identities_table()
    identity.ensure_revoked_sessions_table()
    user_db.create_users_table()
    return db


def setup_function(_fn):
    server._revoked_sessions.clear()


def _body(resp):
    return json.loads(resp.text)


# --- identity / user_db primitives ---


def test_identity_unlink_removes_current_wallet_but_keeps_history(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", WALLET)
    assert identity.unlink("discord", "1") is True
    assert identity.resolve("discord", "1") is None
    # append-only wallet_links history survives (bucket resolver depends on it)
    assert identity.bucket_for_wallet(WALLET) is not None
    # unlinking an unknown identity is a no-op, not an error
    assert identity.unlink("discord", "does-not-exist") is True


def test_user_db_delete_user(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    user_db.register_user("1", "bob", WALLET)
    assert user_db.get_user("1") is not None
    assert user_db.delete_user("1") is True
    assert user_db.get_user("1") is None


# --- token revocation ---


def test_revoked_token_fails_verification(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    tok = server.make_session_token({"id": "1", "name": "bob"})
    other = server.make_session_token({"id": "2", "name": "eve"})
    assert server.verify_session_token(tok)
    server.revoke_session_token(tok)
    assert server.verify_session_token(tok) is None
    assert server.verify_session_token(other)  # unaffected


def test_revoke_garbage_token_is_noop(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    assert server.revoke_session_token("not-a-token") is True
    assert server.revoke_session_token("") is True
    assert server._revoked_sessions == {}
    assert identity.load_revoked_sessions(0) == {}


def test_revocation_survives_restart(tmp_path, monkeypatch):
    # Greptile P1 on #458: a deployer restart must not resurrect a logged-out
    # token. Simulate the restart by wiping the in-process cache and reloading.
    _fresh_dbs(tmp_path, monkeypatch)
    tok = server.make_session_token({"id": "1", "name": "bob"})
    server.revoke_session_token(tok)
    server._revoked_sessions.clear()
    assert server.verify_session_token(tok)  # cache gone → would be accepted
    server.load_revoked_sessions()
    assert server.verify_session_token(tok) is None
    # an expired revocation is dropped on reload, so the table stays bounded
    assert identity.load_revoked_sessions(now=10**12) == {}


def test_load_revocations_fails_closed(tmp_path, monkeypatch):
    # Greptile P1 (round 2): an unreadable revocation table must not become an
    # empty denylist. The loader raises, and the cache is left untouched.
    _fresh_dbs(tmp_path, monkeypatch)
    server._revoked_sessions["keep"] = 10**12
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "missing" / "x.db"))
    with pytest.raises(sqlite3.OperationalError):
        server.load_revoked_sessions()
    assert server._revoked_sessions == {"keep": 10**12}


def test_logout_reports_persist_failure(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    monkeypatch.setattr(identity, "add_revoked_session", lambda *_a: False)
    tok = server.make_session_token({"id": "1", "name": "bob"})
    resp = _run(server.handle_logout(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 500
    assert server.verify_session_token(tok) is None  # still denied in-process


def test_logout_endpoint_revokes_bearer_token(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    tok = server.make_session_token({"id": "1", "name": "bob"})
    resp = _run(server.handle_logout(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 200 and _body(resp) == {"ok": True}
    # same token is now rejected by require_auth
    resp = _run(server.handle_me(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 401


# --- wallet disconnect ---


def test_disconnect_discord_unlinks_identity_and_legacy_users_row(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", WALLET)
    user_db.register_user("1", "bob", WALLET)
    tok = server.make_session_token({"id": "1", "name": "bob", "platform": "discord"})
    req = _Req({"Authorization": f"Bearer {tok}"})
    resp = _run(server.handle_wallet_disconnect(req))
    assert resp.status == 200
    assert _body(resp) == {"ok": True, "wallet": None}
    assert _run(server._resolve_wallet("discord", "1")) is None
    # the session itself stays valid — the Activity re-auths via Discord anyway
    assert server.verify_session_token(tok)


def test_disconnect_telegram_unlinks_identity(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    identity.link("telegram", "9", "alice", WALLET)
    tok = server.make_session_token({"id": "9", "name": "alice", "platform": "telegram"})
    resp = _run(server.handle_wallet_disconnect(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 200
    assert identity.resolve("telegram", "9") is None


def test_disconnect_web_revokes_session_keeps_identity(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    identity.link("web", WALLET, WALLET, WALLET)
    tok = server.make_session_token({"id": WALLET, "name": WALLET, "platform": "web"})
    resp = _run(server.handle_wallet_disconnect(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 200
    assert server.verify_session_token(tok) is None
    # wallet==identity on web; the row (and its push token) is not destroyed
    assert identity.resolve("web", WALLET) == WALLET


def test_disconnect_reports_unlink_failure(tmp_path, monkeypatch):
    _fresh_dbs(tmp_path, monkeypatch)
    identity.link("discord", "1", "bob", WALLET)
    monkeypatch.setattr(identity, "unlink", lambda *_a: False)
    tok = server.make_session_token({"id": "1", "name": "bob", "platform": "discord"})
    resp = _run(server.handle_wallet_disconnect(_Req({"Authorization": f"Bearer {tok}"})))
    assert resp.status == 500
    assert identity.resolve("discord", "1") == WALLET


def test_routes_registered():
    routes = {(r.method, r.resource.canonical) for r in server.create_app().router.routes()}
    assert ("POST", "/api/logout") in routes
    assert ("POST", "/api/wallet/disconnect") in routes
