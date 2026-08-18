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


@pytest.fixture(autouse=True)
def _reset_share_guards():
    server._x_share_last.clear()
    server._x_share_posted.clear()
    yield
    server._x_share_last.clear()
    server._x_share_posted.clear()


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
    assert posted["text"] == "I just minted LFG #7! 🧱 @letseffinggo #XRPL"
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


# --- review-round hardening (PR #398 bot findings) ---------------------------


def test_concurrent_refresh_single_rotation_no_delete(monkeypatch):
    """Two overlapping requests after expiry must serialize: exactly one
    refresh call, both callers converge on the rotated token, the row
    survives (Greptile 3803522292 / CodeRabbit 3803544737)."""
    x_oauth.upsert_account(WALLET, "42", "t", "old-access", "old-refresh", time.time() - 10)
    refresh_calls = []

    async def fake_refresh(refresh_token):
        refresh_calls.append(refresh_token)
        if len(refresh_calls) > 1:
            # X would reject a second use of the same (now-invalidated) token.
            raise x_oauth.XOAuthError("invalid_grant", status=400)
        await asyncio.sleep(0.01)  # widen the race window
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": time.time() + 7200,
        }

    monkeypatch.setattr(x_oauth, "refresh_access_token", fake_refresh)

    async def both():
        return await asyncio.gather(
            x_oauth.get_usable_access_token(WALLET),
            x_oauth.get_usable_access_token(WALLET),
        )

    results = _run(both())
    assert results == ["new-access", "new-access"]
    assert refresh_calls == ["old-refresh"]  # one rotation, not two
    account = x_oauth.get_account(WALLET)
    assert account is not None  # never deleted
    assert x_oauth.decrypt_token(account["refresh_token_enc"]) == "new-refresh"


def test_rejected_refresh_spares_row_rotated_by_another_writer(monkeypatch):
    """A 400 on OUR refresh token must not delete a row another writer has
    already rotated — re-read and use the fresh tokens instead."""
    x_oauth.upsert_account(WALLET, "42", "t", "old-access", "old-refresh", time.time() - 10)

    async def fake_refresh(refresh_token):
        # Simulate a second process winning the rotation mid-flight: the row
        # now carries fresh tokens, and X rejects our stale one.
        x_oauth.rotate_tokens(WALLET, "winner-access", "winner-refresh", time.time() + 7200)
        raise x_oauth.XOAuthError("invalid_grant", status=400)

    monkeypatch.setattr(x_oauth, "refresh_access_token", fake_refresh)
    assert _run(x_oauth.get_usable_access_token(WALLET)) == "winner-access"
    assert x_oauth.get_account(WALLET) is not None


def test_transport_timeout_becomes_xoautherror(monkeypatch):
    """A stalled/broken connection to X must surface as XOAuthError, not a
    raw aiohttp/asyncio exception (Greptile 3803522298 / CR 3803544721)."""

    class _BoomSession:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("timeout") is x_oauth._HTTP_TIMEOUT  # bounded
            raise TimeoutError()

    monkeypatch.setattr(x_oauth.aiohttp, "ClientSession", _BoomSession)
    with pytest.raises(x_oauth.XOAuthError):
        _run(x_oauth._post_form(x_oauth.TOKEN_URL, {}))
    with pytest.raises(x_oauth.XOAuthError):
        _run(x_oauth.fetch_me("tok"))
    with pytest.raises(x_oauth.XOAuthError):
        _run(x_oauth.post_tweet("tok", "hi"))


def test_non_json_response_and_bad_expires_in_stay_in_contract():
    """HTML error pages and junk expires_in must not escape XOAuthError /
    raise raw ValueError (CR 3803544729)."""

    class _HtmlResp:
        async def json(self, content_type=None):
            raise ValueError("not JSON")

    assert _run(x_oauth._read_json(_HtmlResp())) == {}
    parsed = x_oauth._parse_token_response(200, {"access_token": "a", "expires_in": "soon(tm)"})
    assert parsed["access_token"] == "a"
    assert parsed["expires_at"] > time.time()  # fell back to the ~2h default


def test_decrypt_garbage_blob_is_xoautherror():
    with pytest.raises(x_oauth.XOAuthError):
        x_oauth.decrypt_token("not-even-base64!!")


def test_callback_escapes_handle(_feature_on, monkeypatch):
    """The X handle is remote data interpolated into text/html — it must be
    escaped (CR 3803544702)."""
    x_oauth.begin_authorization(WALLET)
    state = next(iter(x_oauth.pending_auths))

    async def fake_exchange(code, verifier):
        return {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 7200}

    async def fake_me(access_token):
        return {"id": "42", "username": "<img src=x onerror=alert(1)>"}

    monkeypatch.setattr(x_oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(x_oauth, "fetch_me", fake_me)
    resp = _run(server.handle_x_callback(_FakeRequest(query={"state": state, "code": "c"})))
    assert resp.status == 200
    assert "<img" not in resp.text
    assert "&lt;img" in resp.text


def test_share_cooldown_and_dedup(_feature_on, monkeypatch):
    """Spend guards (CR 3803544709): same (kind, nft) re-share returns the
    cached tweet with no second paid post; a different share inside the
    cooldown window is 429."""
    posts = []

    async def fake_token(wallet, db_path=None):
        return "tok"

    async def fake_post(access_token, text):
        posts.append(text)
        return str(len(posts))

    monkeypatch.setattr(x_oauth, "get_usable_access_token", fake_token)
    monkeypatch.setattr(x_oauth, "post_tweet", fake_post)

    r1 = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 1})))
    assert r1.status == 200 and len(posts) == 1

    # Same event again: deduped, still one paid post.
    r2 = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 1})))
    assert r2.status == 200
    body2 = json.loads(r2.body)
    assert body2["deduped"] is True and body2["tweet_id"] == "1"
    assert len(posts) == 1

    # A different event inside the cooldown: 429, no post.
    r3 = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 2})))
    assert r3.status == 429
    assert json.loads(r3.body)["code"] == "cooldown"
    assert len(posts) == 1

    # Cooldown elapsed: the different event posts.
    server._x_share_last[_dev_wallet()] -= config.X_USER_SHARE_COOLDOWN_SECONDS + 1
    r4 = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 2})))
    assert r4.status == 200 and len(posts) == 2


def test_share_403_keeps_connection(_feature_on, monkeypatch):
    """A 403 (duplicate content / policy) is NOT a dead token: the account
    row must survive and the client gets a distinct code (CR 3803544711)."""
    wallet = _dev_wallet()
    x_oauth.upsert_account(wallet, "42", "t", "live-access", "r", time.time() + 7200)

    async def refused(access_token, text):
        raise x_oauth.XOAuthError("duplicate content", status=403)

    monkeypatch.setattr(x_oauth, "post_tweet", refused)
    resp = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 3})))
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "post_refused"
    assert x_oauth.get_account(wallet) is not None  # connection intact


def test_share_401_deletes_connection(_feature_on, monkeypatch):
    wallet = _dev_wallet()
    x_oauth.upsert_account(wallet, "42", "t", "live-access", "r", time.time() + 7200)

    async def rejected(access_token, text):
        raise x_oauth.XOAuthError("token revoked", status=401)

    monkeypatch.setattr(x_oauth, "post_tweet", rejected)
    resp = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": 3})))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "not_connected"
    assert x_oauth.get_account(wallet) is None


def test_share_rejects_boolean_nft_number(_feature_on):
    """bool is a subclass of int — `true` must not tweet '#True' at real
    cost (CR 3803544685)."""
    resp = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": True})))
    assert resp.status == 400
    resp = _run(server.handle_x_share(_FakeRequest(body={"kind": "mint", "nft_number": "7"})))
    assert resp.status == 400


def test_refresh_never_clobbers_reconnected_account(monkeypatch):
    """Greptile 3803679817: a wallet completes a NEW OAuth callback (account
    B) while a refresh of the OLD account (A) is in flight. The rotation
    result for A must be discarded (CAS on the consumed refresh-token
    ciphertext), the row must keep B's identity/tokens, and the caller must
    be served B's access token — never A's rotated one."""
    x_oauth.upsert_account(WALLET, "user-A", "a", "a-access", "a-refresh", time.time() - 10)

    async def fake_refresh(refresh_token):
        assert refresh_token == "a-refresh"
        # Mid-refresh, the reconnect callback lands account B on the row
        # (bypassing the per-wallet lock — this exercises the CAS backstop,
        # e.g. a second process).
        x_oauth.upsert_account(WALLET, "user-B", "b", "b-access", "b-refresh", time.time() + 7200)
        return {
            "access_token": "a-rotated-access",
            "refresh_token": "a-rotated-refresh",
            "expires_at": time.time() + 7200,
        }

    monkeypatch.setattr(x_oauth, "refresh_access_token", fake_refresh)
    token = _run(x_oauth.get_usable_access_token(WALLET))
    assert token == "b-access"  # served from the newer connection
    account = x_oauth.get_account(WALLET)
    assert account["x_user_id"] == "user-B"
    assert x_oauth.decrypt_token(account["access_token_enc"]) == "b-access"
    assert x_oauth.decrypt_token(account["refresh_token_enc"]) == "b-refresh"


def test_stale_refresh_rejection_never_deletes_reconnected_account(monkeypatch):
    """Same race, rejection flavor: X 400s the OLD account's refresh after
    the wallet reconnected as account B — the CAS delete must not fire."""
    x_oauth.upsert_account(WALLET, "user-A", "a", "a-access", "a-refresh", time.time() - 10)

    async def fake_refresh(refresh_token):
        x_oauth.upsert_account(WALLET, "user-B", "b", "b-access", "b-refresh", time.time() + 7200)
        raise x_oauth.XOAuthError("invalid_grant", status=400)

    monkeypatch.setattr(x_oauth, "refresh_access_token", fake_refresh)
    assert _run(x_oauth.get_usable_access_token(WALLET)) == "b-access"
    account = x_oauth.get_account(WALLET)
    assert account is not None and account["x_user_id"] == "user-B"


def test_client_x_user_share_flag_can_turn_off():
    """The client must adopt x_user_share only when the server sent the
    field — a truthy-OR latch could never disarm (CR 3803544743)."""
    import os as _os

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, "webapp", "client", "app.js")).read()
    assert "'x_user_share' in cfg" in src
    assert "!!cfg.x_user_share) || xUserShare" not in src
