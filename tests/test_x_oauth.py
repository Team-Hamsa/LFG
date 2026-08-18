# tests/test_x_oauth.py
# Per-user X OAuth2 PKCE — "Share from my account" (#252, spec §7).
import asyncio
import base64
import hashlib
import json
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

from lfg_core import config
from lfg_service import app as server
from lfg_service import x_oauth

WALLET = "rTESTWALLETxxxxxxxxxxxxxxxxxxxxxxx"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRequest:
    def __init__(self, headers=None, query=None, body=None):
        self.headers = headers or {}
        self._store = {}
        self._body = body or {}

        class _Rel:
            pass

        self.rel_url = _Rel()
        self.rel_url.query = query or {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = str(tmp_path / "identity.db")
    monkeypatch.setattr(x_oauth, "DATABASE", db)
    monkeypatch.setattr(x_oauth, "pending_auths", {})
    yield db


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(config, "X_TOKEN_ENC_KEY", key)
    yield key


@pytest.fixture
def _feature_on(monkeypatch):
    monkeypatch.setattr(config, "X_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setattr(config, "X_OAUTH_CLIENT_SECRET", "")
    monkeypatch.setattr(config, "X_OAUTH_CALLBACK_URL", "https://example.test/api/x/callback")
    monkeypatch.setattr(config, "X_USER_SHARE_ENABLED", True)
    # Dev-mode auth shortcut: require_wallet injects mock_economy.DEV_OWNER.
    monkeypatch.setattr(config, "WEBAPP_DEV_MODE", True)


def _dev_wallet():
    from webapp import mock_economy

    return mock_economy.DEV_OWNER


# --- feature-off gating ------------------------------------------------------


@pytest.mark.parametrize(
    "handler_name",
    [
        "handle_x_connect",
        "handle_x_callback",
        "handle_x_account",
        "handle_x_disconnect",
        "handle_x_share",
    ],
)
def test_feature_off_is_404(monkeypatch, handler_name):
    monkeypatch.setattr(config, "X_USER_SHARE_ENABLED", False)
    resp = _run(getattr(server, handler_name)(_FakeRequest()))
    assert resp.status == 404


# --- PKCE flow shape ---------------------------------------------------------


def test_begin_authorization_s256_and_state_binding(monkeypatch):
    monkeypatch.setattr(config, "X_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "X_OAUTH_CALLBACK_URL", "https://example.test/cb")
    url = x_oauth.begin_authorization(WALLET)
    assert url.startswith(x_oauth.AUTHORIZE_URL + "?")
    assert "code_challenge_method=S256" in url
    assert "scope=tweet.read+tweet.write+users.read+offline.access" in url
    # exactly one pending state, bound to the wallet
    assert len(x_oauth.pending_auths) == 1
    state, pending = next(iter(x_oauth.pending_auths.items()))
    assert pending.wallet == WALLET
    assert f"state={state}" in url
    # the challenge in the URL is the S256 of the stored verifier
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(pending.code_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert f"code_challenge={expected}" in url


def test_claim_state_is_one_shot_and_expires(monkeypatch):
    monkeypatch.setattr(config, "X_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(config, "X_OAUTH_CALLBACK_URL", "https://example.test/cb")
    x_oauth.begin_authorization(WALLET)
    state = next(iter(x_oauth.pending_auths))
    assert x_oauth.claim_state(state).wallet == WALLET
    assert x_oauth.claim_state(state) is None  # no replay
    # expired states are rejected
    x_oauth.begin_authorization(WALLET)
    state2 = next(iter(x_oauth.pending_auths))
    x_oauth.pending_auths[state2].created_at -= x_oauth.STATE_TTL_SECONDS + 1
    assert x_oauth.claim_state(state2) is None


def test_callback_rejects_unknown_state(_feature_on):
    resp = _run(server.handle_x_callback(_FakeRequest(query={"state": "bogus", "code": "c"})))
    assert resp.status == 400


# --- encryption at rest ------------------------------------------------------


def test_token_encryption_round_trip_and_no_plaintext_at_rest(_isolated_db):
    access = "access-token-SECRET-A"
    refresh = "refresh-token-SECRET-R"
    x_oauth.upsert_account(WALLET, "42", "tester", access, refresh, time.time() + 7200)
    account = x_oauth.get_account(WALLET)
    assert x_oauth.decrypt_token(account["access_token_enc"]) == access
    assert x_oauth.decrypt_token(account["refresh_token_enc"]) == refresh
    # the raw DB file must not contain either plaintext token
    raw = open(_isolated_db, "rb").read()
    assert access.encode() not in raw
    assert refresh.encode() not in raw
    # and the stored columns aren't the plaintext either
    assert account["access_token_enc"] != access


def test_wrong_key_reads_as_disconnected(monkeypatch):
    x_oauth.upsert_account(WALLET, "42", "t", "a", "r", 0)  # already expired
    monkeypatch.setattr(config, "X_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    assert _run(x_oauth.get_usable_access_token(WALLET)) is None


# --- atomic refresh rotation -------------------------------------------------


def test_refresh_rotation_persists_before_use(monkeypatch):
    x_oauth.upsert_account(WALLET, "42", "t", "old-access", "old-refresh", time.time() - 10)

    calls = []

    async def fake_refresh(refresh_token):
        assert refresh_token == "old-refresh"
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": time.time() + 7200,
        }

    real_rotate = x_oauth.rotate_tokens

    def spy_rotate(*args, **kwargs):
        calls.append("rotate")
        return real_rotate(*args, **kwargs)

    monkeypatch.setattr(x_oauth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(x_oauth, "rotate_tokens", spy_rotate)

    token = _run(x_oauth.get_usable_access_token(WALLET))
    assert token == "new-access"
    assert calls == ["rotate"]  # persisted before the token was handed out
    # crash-after-persist simulation: the row already holds the rotated pair,
    # so a process death right here loses nothing.
    account = x_oauth.get_account(WALLET)
    assert x_oauth.decrypt_token(account["refresh_token_enc"]) == "new-refresh"
    assert x_oauth.decrypt_token(account["access_token_enc"]) == "new-access"


def test_rotation_crash_between_rotate_and_use_keeps_refresh_token(monkeypatch):
    """A crash after rotate_tokens but before the caller uses the access
    token must leave the NEW refresh token on disk (the old one is dead)."""
    x_oauth.upsert_account(WALLET, "42", "t", "old-access", "old-refresh", time.time() - 10)

    async def fake_refresh(refresh_token):
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": time.time() + 7200,
        }

    class Boom(Exception):
        pass

    real_rotate = x_oauth.rotate_tokens

    def crashing_rotate(*args, **kwargs):
        real_rotate(*args, **kwargs)  # persist lands...
        raise Boom()  # ...then the process "dies"

    monkeypatch.setattr(x_oauth, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(x_oauth, "rotate_tokens", crashing_rotate)

    with pytest.raises(Boom):
        _run(x_oauth.get_usable_access_token(WALLET))
    account = x_oauth.get_account(WALLET)
    assert x_oauth.decrypt_token(account["refresh_token_enc"]) == "new-refresh"


def test_rejected_refresh_deletes_row(monkeypatch):
    x_oauth.upsert_account(WALLET, "42", "t", "a", "dead-refresh", time.time() - 10)

    async def fake_refresh(refresh_token):
        raise x_oauth.XOAuthError("invalid_grant", status=400)

    monkeypatch.setattr(x_oauth, "refresh_access_token", fake_refresh)
    assert _run(x_oauth.get_usable_access_token(WALLET)) is None
    assert x_oauth.get_account(WALLET) is None


def test_unexpired_token_used_without_refresh(monkeypatch):
    x_oauth.upsert_account(WALLET, "42", "t", "live-access", "r", time.time() + 7200)

    async def explode(refresh_token):  # pragma: no cover - must not be called
        raise AssertionError("refresh must not run for a live token")

    monkeypatch.setattr(x_oauth, "refresh_access_token", explode)
    assert _run(x_oauth.get_usable_access_token(WALLET)) == "live-access"


# --- disconnect --------------------------------------------------------------


def test_disconnect_deletes_locally_even_when_revoke_fails(_feature_on, monkeypatch):
    wallet = _dev_wallet()
    x_oauth.upsert_account(wallet, "42", "t", "a", "r", time.time() + 7200)

    async def failing_revoke(token):
        raise x_oauth.XOAuthError("network down")

    monkeypatch.setattr(x_oauth, "revoke_token", failing_revoke)
    resp = _run(server.handle_x_disconnect(_FakeRequest()))
    assert resp.status == 200
    assert x_oauth.get_account(wallet) is None


# --- share -------------------------------------------------------------------


def test_share_falls_back_when_not_connected(_feature_on):
    resp = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 7})))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "not_connected"


def test_share_posts_server_built_link_free_text(_feature_on, monkeypatch):
    posted = {}

    async def fake_token(wallet, db_path=None):
        return "tok"

    async def fake_post(access_token, text):
        posted["text"] = text
        return "999"

    monkeypatch.setattr(x_oauth, "get_usable_access_token", fake_token)
    monkeypatch.setattr(x_oauth, "post_tweet", fake_post)
    resp = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 7})))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["posted"] is True and body["tweet_id"] == "999"
    assert posted["text"] == "I just minted LFG #7! 🧱 #XRPL"
    assert "http" not in posted["text"]  # link-free by directive


def test_share_rejects_unknown_kind(_feature_on):
    resp = _run(server.handle_x_share(_FakeRequest(body={"kind": "evil text injection"})))
    assert resp.status == 400


def test_x_accounts_table_schema(_isolated_db):
    x_oauth.ensure_x_accounts_table()
    cols = {r[1] for r in sqlite3.connect(_isolated_db).execute("PRAGMA table_info(x_accounts)")}
    assert cols == {
        "wallet",
        "x_user_id",
        "x_handle",
        "access_token_enc",
        "refresh_token_enc",
        "expires_at",
        "connected_at",
    }
