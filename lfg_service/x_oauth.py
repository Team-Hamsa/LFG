# lfg_service/x_oauth.py
# Per-user X (Twitter) OAuth2 PKCE — "Share from my account" (#252, phase 3
# of #41; spec docs/superpowers/specs/2026-07-05-x-integration-design.md §7).
#
# Responsibilities:
#   * PKCE authorization (S256) — build the authorize URL, hold the
#     state -> (wallet, code_verifier) binding in memory until the callback.
#   * Token exchange / refresh / best-effort revoke against api.x.com.
#   * The `x_accounts` table (identity DB): one row per wallet with the
#     user's x_user_id/handle and Fernet-encrypted access+refresh tokens.
#     Tokens are NEVER stored in plaintext — a leaked DB file must not leak
#     posting capability for users' personal X accounts (spec §7).
#   * Refresh rotation (spec A4): X rotates the refresh token on every use
#     and the old one is invalidated, so the rotated pair is persisted
#     atomically (single UPDATE) BEFORE the new access token is used — a
#     crash after persist loses nothing; a crash before use loses nothing.
#
# No XRPL transactions here — no SourceTag/memos surface (spec §9).
# Feature-off posture: config.X_USER_SHARE_ENABLED is False unless
# X_OAUTH_CLIENT_ID + X_OAUTH_CALLBACK_URL + X_TOKEN_ENC_KEY are all set;
# the service gates every /api/x/* route on it (404) so an undeployed
# callback URL exposes nothing.

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
from cryptography.fernet import Fernet, InvalidToken

from lfg_core import config
from lfg_core.user_db import DATABASE  # identity DB path (same file as identities)

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
USERS_ME_URL = "https://api.x.com/2/users/me"
TWEET_CREATE_URL = "https://api.x.com/2/tweets"

SCOPES = "tweet.read tweet.write users.read offline.access"

# How long an issued state/code_verifier pair stays redeemable. The user has
# to click through X's consent screen; 10 minutes is generous without letting
# stale states pile up forever.
STATE_TTL_SECONDS = 600

# Refresh slightly before the ~2h expiry X reports, so a token that is about
# to lapse mid-request is rotated up front instead of failing the post.
_EXPIRY_SKEW_SECONDS = 60


class XOAuthError(Exception):
    """Any failure talking to X's OAuth2/token/tweet endpoints."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --- Fernet at-rest encryption ---------------------------------------------


def _fernet() -> Fernet:
    key = config.X_TOKEN_ENC_KEY
    if not key:
        raise XOAuthError("X_TOKEN_ENC_KEY is not configured")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken as exc:  # wrong/rotated key: treat as disconnected
        raise XOAuthError("stored X token cannot be decrypted") from exc


# --- x_accounts store -------------------------------------------------------


def ensure_x_accounts_table(db_path: str | None = None) -> None:
    conn = sqlite3.connect(db_path or DATABASE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS x_accounts (
                wallet            TEXT PRIMARY KEY,
                x_user_id         TEXT NOT NULL,
                x_handle          TEXT,
                access_token_enc  TEXT NOT NULL,
                refresh_token_enc TEXT,
                expires_at        REAL NOT NULL,
                connected_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def upsert_account(
    wallet: str,
    x_user_id: str,
    x_handle: str | None,
    access_token: str,
    refresh_token: str | None,
    expires_at: float,
    db_path: str | None = None,
) -> None:
    """Store (or replace) a wallet's connected X account. Tokens are
    encrypted here — plaintext never touches the DB layer."""
    ensure_x_accounts_table(db_path)
    conn = sqlite3.connect(db_path or DATABASE)
    try:
        conn.execute(
            """
            INSERT INTO x_accounts
                (wallet, x_user_id, x_handle, access_token_enc,
                 refresh_token_enc, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet) DO UPDATE SET
                x_user_id = excluded.x_user_id,
                x_handle = excluded.x_handle,
                access_token_enc = excluded.access_token_enc,
                refresh_token_enc = excluded.refresh_token_enc,
                expires_at = excluded.expires_at,
                connected_at = CURRENT_TIMESTAMP
            """,
            (
                wallet,
                x_user_id,
                x_handle,
                encrypt_token(access_token),
                encrypt_token(refresh_token) if refresh_token else None,
                expires_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def rotate_tokens(
    wallet: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: float,
    db_path: str | None = None,
) -> None:
    """Persist a rotated access+refresh pair in one UPDATE (spec A4: the
    rotation MUST land on disk before the new access token is used — the old
    refresh token is already dead server-side, so losing the new one here
    permanently disconnects the user)."""
    conn = sqlite3.connect(db_path or DATABASE)
    try:
        conn.execute(
            """
            UPDATE x_accounts
               SET access_token_enc = ?, refresh_token_enc = ?, expires_at = ?
             WHERE wallet = ?
            """,
            (
                encrypt_token(access_token),
                encrypt_token(refresh_token) if refresh_token else None,
                expires_at,
                wallet,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_account(wallet: str, db_path: str | None = None) -> dict[str, Any] | None:
    ensure_x_accounts_table(db_path)
    conn = sqlite3.connect(db_path or DATABASE)
    try:
        row = conn.execute(
            "SELECT wallet, x_user_id, x_handle, access_token_enc, "
            "refresh_token_enc, expires_at, connected_at "
            "FROM x_accounts WHERE wallet = ?",
            (wallet,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "wallet": row[0],
        "x_user_id": row[1],
        "x_handle": row[2],
        "access_token_enc": row[3],
        "refresh_token_enc": row[4],
        "expires_at": row[5],
        "connected_at": row[6],
    }


def delete_account(wallet: str, db_path: str | None = None) -> bool:
    ensure_x_accounts_table(db_path)
    conn = sqlite3.connect(db_path or DATABASE)
    try:
        cur = conn.execute("DELETE FROM x_accounts WHERE wallet = ?", (wallet,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- PKCE authorization -----------------------------------------------------


@dataclass
class PendingAuth:
    wallet: str
    code_verifier: str
    created_at: float

    def expired(self, now: float | None = None) -> bool:
        return ((now if now is not None else time.time()) - self.created_at) > STATE_TTL_SECONDS


# state -> PendingAuth. In-memory on purpose: a restart mid-consent just
# means the user clicks "connect" again (same posture as the in-memory
# mint/swap sessions in lfg_service/app.py).
pending_auths: dict[str, PendingAuth] = {}


def _s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def begin_authorization(wallet: str) -> str:
    """Create a state+verifier pair bound to `wallet` and return the
    authorize URL to open. The state IS the session binding: only the
    wallet recorded here can be credited by the callback."""
    # Sweep expired states so abandoned connects don't accumulate.
    now = time.time()
    for key in [k for k, v in pending_auths.items() if v.expired(now)]:
        pending_auths.pop(key, None)

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)  # 43-128 chars per RFC 7636
    pending_auths[state] = PendingAuth(wallet=wallet, code_verifier=code_verifier, created_at=now)
    params = {
        "response_type": "code",
        "client_id": config.X_OAUTH_CLIENT_ID,
        "redirect_uri": config.X_OAUTH_CALLBACK_URL,
        "scope": SCOPES,
        "state": state,
        "code_challenge": _s256_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def claim_state(state: str) -> PendingAuth | None:
    """One-shot redemption of a callback state (pop — a state can never be
    replayed). Returns None for unknown or expired states."""
    pending = pending_auths.pop(state, None)
    if pending is None or pending.expired():
        return None
    return pending


# --- HTTP to X --------------------------------------------------------------


def _token_request_auth() -> tuple[dict[str, str], dict[str, str]]:
    """(extra headers, extra form fields) for the token/revoke endpoints:
    confidential clients authenticate with Basic; public clients send
    client_id in the body."""
    if config.X_OAUTH_CLIENT_SECRET:
        creds = f"{config.X_OAUTH_CLIENT_ID}:{config.X_OAUTH_CLIENT_SECRET}"
        basic = base64.b64encode(creds.encode()).decode()
        return {"Authorization": f"Basic {basic}"}, {}
    return {}, {"client_id": config.X_OAUTH_CLIENT_ID}


async def _post_form(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    headers, extra = _token_request_auth()
    async with aiohttp.ClientSession() as http:
        async with http.post(url, data={**form, **extra}, headers=headers) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {}
            return resp.status, body if isinstance(body, dict) else {}


def _parse_token_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    if status != 200 or "access_token" not in body:
        raise XOAuthError(
            f"X token endpoint returned {status}: {body.get('error', 'unknown error')}",
            status=status,
        )
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_at": time.time() + float(body.get("expires_in", 7200)),
    }


async def exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    status, body = await _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.X_OAUTH_CALLBACK_URL,
            "code_verifier": code_verifier,
        },
    )
    return _parse_token_response(status, body)


async def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    status, body = await _post_form(
        TOKEN_URL,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    return _parse_token_response(status, body)


async def revoke_token(token: str) -> bool:
    """Best-effort revoke; failures are logged, never raised (spec §7: the
    local row delete always wins)."""
    try:
        status, _ = await _post_form(REVOKE_URL, {"token": token})
        return status == 200
    except Exception as exc:  # network down, DNS, anything — still disconnect locally
        logger.warning("X token revoke failed (continuing with local delete): %s", exc)
        return False


async def fetch_me(access_token: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as http:
        async with http.get(
            USERS_ME_URL, headers={"Authorization": f"Bearer {access_token}"}
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or "data" not in body:
                raise XOAuthError(f"X users/me returned {resp.status}", status=resp.status)
            data: dict[str, Any] = body["data"]  # {id, name, username}
            return data


async def post_tweet(access_token: str, text: str) -> str:
    async with aiohttp.ClientSession() as http:
        async with http.post(
            TWEET_CREATE_URL,
            json={"text": text},
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status not in (200, 201) or "data" not in body:
                raise XOAuthError(f"X tweet create returned {resp.status}", status=resp.status)
            return str(body["data"].get("id", ""))


# --- token lifecycle --------------------------------------------------------


async def get_usable_access_token(wallet: str, db_path: str | None = None) -> str | None:
    """Return a live access token for `wallet`, refreshing (with atomic
    rotation persistence) if the stored one is expired. None = not
    connected (no row, no refresh token when needed, or refresh rejected
    — the caller should treat all of these as "fall back to Web Intent")."""
    account = get_account(wallet, db_path)
    if not account:
        return None
    try:
        if account["expires_at"] > time.time() + _EXPIRY_SKEW_SECONDS:
            return decrypt_token(account["access_token_enc"])
        if not account["refresh_token_enc"]:
            return None
        refresh_token = decrypt_token(account["refresh_token_enc"])
    except XOAuthError:
        # Undecryptable tokens (rotated enc key) — the connection is dead.
        return None

    try:
        rotated = await refresh_access_token(refresh_token)
    except XOAuthError as exc:
        if exc.status in (400, 401):
            # Refresh token revoked/expired server-side: connection is dead.
            # Drop the row so the UI reports "not connected" honestly.
            delete_account(wallet, db_path)
            return None
        raise

    # A4: persist the rotated pair BEFORE the new access token is used —
    # the old refresh token died the moment X rotated it.
    rotate_tokens(
        wallet,
        rotated["access_token"],
        rotated["refresh_token"],
        rotated["expires_at"],
        db_path,
    )
    return str(rotated["access_token"])
