# Per-user OAuth2 PKCE for X — "Share from my account" — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #252 (phase 3 of #41)

## Problem

Phase 1/2 of the X integration (#41) shipped two things: the **brand-account
auto-poster** (`surfaces/x_bot/`, OAuth 1.0a app creds, one shared account) and
zero-OAuth **Web-Intent share buttons** in the Activity
(`webapp/client/app.js` `buildShareControl` / `shareUrlFor`, driven by
`PUBLIC_SHARE_BASE_URL`). Neither lets a *user* post from *their own* X account:
the Web-Intent button just opens X's compose window in the user's browser
(client-side, no server post, no media attached beyond the OG card), and the
brand poster only ever tweets from the one brand handle.

Issue #41 §7 specced — and deliberately deferred — a **phase 3**: a per-user
OAuth2 Authorization Code + PKCE "connect your X account" flow plus a
server-side "Share this mint from my account" action that posts (with the NFT
image as native media) on the connected user's behalf. That deferred design
lives in `docs/superpowers/specs/2026-07-05-x-integration-design.md` §7 as a
five-bullet sketch; this doc is the full design that grounds it in the current
code and calls out the real decisions.

Nothing in the codebase implements any OAuth2 (PKCE) flow today: `grep -rn
"oauth2\|PKCE\|code_challenge\|code_verifier\|x_accounts"` finds nothing. The
existing `surfaces/x_bot/x_api.py` is an OAuth **1.0a** signer (brand account
only) and is the wrong shape for per-user posting.

## Constraints discovered

- **No XRPL transaction anywhere in this feature.** Posting a tweet touches no
  ledger, so **SourceTag (2606160021) and provenance memos do not apply** — same
  as the rest of #41 (parent spec §2: "No XRPL transactions … SourceTag rules
  don't apply"). Do not invent a tx path.
- **No-custody / no-secrets-in-client is the house invariant.** The OAuth2
  client secret, the PKCE `code_verifier`, and the token-encryption key MUST
  live server-side only. The browser only ever sees the authorize URL and, at
  callback, an opaque `state`.
- **Secret posture is `.env`-only** (CLAUDE.md; parent spec §2, §7). There is no
  secret store. Per-user access/refresh tokens are *personal-account posting
  capability*; the identity DB file (`config.DB_PATH`) is gitignored but sits on
  disk beside CSVs. Per §7 they MUST be **Fernet-encrypted at rest** with a key
  (`X_TOKEN_ENC_KEY`) from `.env`, so a leaked DB file alone cannot post as a
  user.
- **OAuth2 user tokens are short-lived (~2h) and refresh tokens rotate on use**
  (parent spec §3, row A4; scope `offline.access`). Refresh MUST persist the new
  refresh+access pair **atomically (single UPDATE) before the new access token
  is used**, or a crash mid-refresh strands the account (old refresh token
  already invalidated by X).
- **`state` must be bound to the caller's wallet session and be CSRF-safe.** The
  callback is a top-level browser redirect from X with **no `Authorization`
  header** available, so the wallet identity cannot come from `require_wallet`
  there — it must ride inside a tamper-proof `state`. The service already has an
  HMAC-signed-token primitive (`make_session_token` / `verify_session_token`,
  `_session_secret`) — reuse that construction for `state`.
- **Public HTTPS callback is a hard ops gate** (parent spec §7, §6.2; same
  dependency as Mini-App #89 Part B and `PUBLIC_SHARE_BASE_URL`). X requires the
  OAuth2 redirect URI to be a real registered public HTTPS URL. Feature stays
  **off when unset** (house convention, `lfg_core/config.py` X_* block).
- **Server-side posting spends real money.** X API is pay-per-use (~$0.015/post,
  $0.20/post-with-URL; parent spec §3 A2). Every user "share" is an app-account
  cost. `X_MONTHLY_POST_BUDGET` (default 100) already exists as the brand
  poster's cost knob (`surfaces/x_bot/state.py::month_count`); the per-user path
  needs a budget gate too (see Open questions).
- **Client is no-build vanilla JS with a cache-buster.** Any `app.js` change
  MUST bump `app.js?v=32` in `webapp/client/index.html` in the same commit.

## Design

Four seams, all additive; the mint/swap/economy flows are untouched.

### 1. Config (`lfg_core/config.py`, feature-off-when-unset)

New optional env vars (X provides a *separate* OAuth2 Client ID/Secret distinct
from the OAuth 1.0a consumer key/secret already in use):

```
X_OAUTH2_CLIENT_ID=<x-app-oauth2-client-id>
X_OAUTH2_CLIENT_SECRET=<x-app-oauth2-client-secret>   # confidential client
X_OAUTH2_REDIRECT_URI=<public-https>/api/x/callback   # must be registered in the X app
X_TOKEN_ENC_KEY=<fernet-key>                           # Fernet.generate_key()
X_USER_SHARE_ENABLED=0                                 # master flag (off by default)
```

`X_USER_SHARE_ENABLED` mirrors the `X_ENABLED = env_flag(...) and all(creds)`
pattern already at `config.py:393`: the flag is only *effectively* on when set
**and** all four of `X_OAUTH2_CLIENT_ID/SECRET/REDIRECT_URI/X_TOKEN_ENC_KEY` are
present. Unset ⇒ endpoints 404/403 and the client hides the button.

### 2. Token storage + encryption — `lfg_core/x_token_store.py` (new)

New table in the identity DB (`config.DB_PATH`, same DB as `identities`), created
by a self-migrating `ensure_x_accounts_table()` called from `app.on_startup`
next to `identity.ensure_identities_table()`:

```sql
CREATE TABLE IF NOT EXISTS x_accounts (
    wallet             TEXT PRIMARY KEY,   -- the canonical account key (matches identities.wallet)
    x_user_id          TEXT NOT NULL,
    x_handle           TEXT,
    access_token_enc   BLOB NOT NULL,      -- Fernet ciphertext
    refresh_token_enc  BLOB NOT NULL,      -- Fernet ciphertext
    expires_at         INTEGER NOT NULL,   -- epoch seconds
    connected_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP
)
```

Wallet PK (not `x_user_id`): the app's account identity is the XRPL wallet
(`identity.resolve` → wallet), and the share endpoint is `@require_wallet`, so a
wallet lookup is the natural key. Functions (all sqlite3, thread-offloaded like
`identity.py`): `upsert(wallet, x_user_id, x_handle, access, refresh, expires_at)`
(encrypts, atomic single-row upsert), `get(wallet) -> XAccount | None`
(decrypts), `update_tokens(wallet, access, refresh, expires_at)` (atomic single
UPDATE for refresh rotation), `delete(wallet) -> None`. Fernet key from
`config.X_TOKEN_ENC_KEY`; a missing/invalid key makes encrypt/decrypt raise so
the feature fails loudly rather than storing plaintext. **New dep:**
`cryptography` (Fernet) added to `requirements.txt` (present transitively today —
pin it explicitly since we now import it directly).

### 3. OAuth2 PKCE + posting client — `lfg_core/x_oauth.py` (new)

Pure, HTTP-boundary-mockable helpers (no OAuth 1.0a signing — OAuth2 user
context posts with a plain `Authorization: Bearer <access>` header):

- `new_pkce() -> (verifier, challenge_s256)` — 43–128-char URL-safe verifier,
  `base64url(sha256(verifier))` challenge (S256).
- `authorize_url(client_id, redirect_uri, state, challenge) -> str` — builds
  `https://x.com/i/oauth2/authorize?response_type=code&scope=tweet.read%20tweet.write%20users.read%20offline.access&code_challenge_method=S256&...`.
- `async exchange_code(session, code, verifier) -> TokenSet` — `POST
  https://api.x.com/2/oauth2/token`, `grant_type=authorization_code`, HTTP Basic
  client auth (`client_id:client_secret`), `code_verifier`. Returns
  `access_token`, `refresh_token`, `expires_in`.
- `async refresh(session, refresh_token) -> TokenSet` — `grant_type=refresh_token`
  (rotates the refresh token).
- `async revoke(session, token) -> None` — `POST .../2/oauth2/revoke`,
  best-effort (swallows failure).
- `async post_tweet(session, access_token, text, media_id=None) -> str` and
  `async upload_media(session, access_token, image_bytes, mime) -> str` — v2
  `POST /2/tweets` and `POST /2/media/upload` with the user's bearer token
  (reuse the endpoint constants/response-shape parsing already proven in
  `surfaces/x_bot/x_api.py`; media multipart, tweet JSON body).

### 4. Service endpoints (`lfg_service/app.py`)

State binding reuses the HMAC-token construction. `_x_oauth_state(wallet,
nonce)` = base64(json{wallet, nonce, exp}) + `hmac(_session_secret(), ...)`;
`_verify_x_oauth_state` mirrors `verify_session_token`. The PKCE `verifier` is
held in a short-TTL in-process map `_x_connect_pending[nonce] = (verifier,
exp)` (same posture as the existing session maps; a service restart between
connect and callback simply makes the user retry — acceptable, logged).

- **`GET /api/x/connect`** (`@require_wallet`, gated on `X_USER_SHARE_ENABLED`)
  → `new_pkce()`, random `nonce`, stash verifier, return
  `{"authorize_url": authorize_url(...)}`. Client opens it in a top-level tab.
- **`GET /api/x/callback?code=&state=`** (public — no Bearer; gated on flag) →
  `_verify_x_oauth_state` (reject expired/tampered → 400), pop verifier by
  `nonce` (missing → 400 "connect expired"), `exchange_code`, then `users/me`
  for `x_user_id`/`x_handle`, `x_token_store.upsert(wallet, ...)`. Responds with
  a tiny **HTML** page (not JSON — it renders in the browser tab) that says
  "connected as @handle — return to LFG" and, when in the Discord Activity,
  posts a `window.opener`/`postMessage` or just instructs the user to switch
  back. CSRF-safe because `state` is wallet-bound and HMAC-signed.
- **`GET /api/x/status`** (`@require_wallet`) → `{"connected": bool, "x_handle":
  ...}` from `x_token_store.get`. Drives the button's connected/disconnected
  rendering.
- **`DELETE /api/x/connect`** (`@require_wallet`) → best-effort
  `x_oauth.revoke` of the stored (decrypted) refresh token, then
  **unconditional** `x_token_store.delete(wallet)` (fail-closed: local delete
  always wins even if revoke 5xxs).
- **`POST /api/x/share`** (`@require_wallet`, gated on flag) → body
  `{"nft_number": N}` (optionally `nft_id`). Steps:
  1. `x_token_store.get(wallet)` → 409 `x_not_connected` if none.
  2. **Budget gate** (see Open questions) → 429 `x_budget_reached` if over cap.
  3. Token freshness: if `expires_at <= now + skew`, `x_oauth.refresh(...)` and
     `x_token_store.update_tokens(...)` **before** using the new access token
     (atomic single UPDATE). A refresh that fails with invalid_grant ⇒ delete
     the row + 409 `x_reauth_required` (user must reconnect).
  4. Resolve the mint's `image_url` + traits from the on-chain index / LFG table
     (same source the brand poster reads from the event `data`; here fetched by
     `nft_number`), download the image, `upload_media`, then `post_tweet(text +
     public card URL, media_id)`. Tweet text reuses the brand poster's copy
     helpers where practical.
  5. Record the post outcome for budget/idempotence and return `{"tweet_id",
     "url"}`. A same-(wallet,nft_number) re-share is deduped/rate-limited.

### 5. Client (`webapp/client/app.js`, `index.html` cache-buster bump)

On the mint-success (and swap-success) panels, `buildShareControl` gains a
sibling **"Share from my account"** control. On render it consults
`/api/x/status`: not connected ⇒ the control is a "Connect X" button that opens
`GET /api/x/connect`'s `authorize_url` in a new tab; connected ⇒ a "Post to
@handle" button that `POST`s `/api/x/share` and shows the resulting tweet link.
The existing zero-OAuth Web-Intent anchor **stays as the always-available
fallback** (parent spec §7 bullet: "falls back to Web Intent when not
connected", and when `X_USER_SHARE_ENABLED` is off). Bump `app.js?v=32` →
`v=33` in `index.html` in the same commit.

## Out of scope

- Auto-posting a user's mints to their account (this is an explicit,
  button-driven share only — server money is spent only on deliberate action).
- Posting swaps/assembles/achievements from the user account (allowlist
  extension, later — same posture as parent spec §9).
- Telegram/Discord-bot surfaces of the connect flow (the connect + share UI is
  Activity/web-client only for MVP; the endpoints are surface-agnostic and could
  be reused later).
- Any XRPL transaction, and therefore any SourceTag/memo work.
- Migrating the brand poster (`surfaces/x_bot/`) to OAuth2 — it stays OAuth 1.0a.

## Open questions / decisions for maintainer

1. **Budget accounting for per-user posts.** The brand poster tracks spend in
   `X_STATE_DB_PATH` via `surfaces/x_bot/state.py::month_count`, but that runs in
   the *separate* `lfg-x` pm2 process; the share endpoint runs *in-service*.
   Options: (a) the service writes to the same `x_state.db` (couples service to
   a surface module), (b) a new `x_share_log` table in the identity DB with its
   own `X_USER_SHARE_MONTHLY_BUDGET` + per-user daily cap, (c) both a global app
   cap and a per-user cap. Recommend **(b)** — a self-contained per-user budget
   table + a distinct env knob — since user posts and brand posts are different
   cost centers. Needs a number (per-user/day, global/month).
2. **Should per-user share be gated behind a paid X tier or the same
   pay-per-use budget as the brand poster?** (Ops/cost decision from #41's
   deferral gate #2.)
3. **Callback UX inside the Discord Activity.** The Activity runs in the
   `*.discordsays.com` sandbox iframe; the OAuth redirect lands on our public
   host in a *new top-level tab*, not the iframe. Confirm the return-to-app UX
   (manual switch-back vs `postMessage`) — Task 0-style verification like parent
   spec §10.3.
4. **`x_accounts` keyed by wallet** assumes one X account per wallet. Confirm a
   user re-connecting a different X handle should overwrite (upsert) rather than
   error.
5. **Relation to #273 (share attribution).** The `?ref=<wallet>` attribution the
   Web-Intent link already appends (`shareUrlFor`) — should a server-side
   user-posted tweet's card URL carry the same `ref`? (Probably yes, for
   consistent click attribution.)

## Testing

- **Unit (pure):** `x_oauth.new_pkce` produces a valid S256 verifier/challenge
  pair (challenge == base64url(sha256(verifier)), no padding); `authorize_url`
  contains all required params + the exact scope string; `_x_oauth_state` round-
  trips and rejects a tampered/expired state.
- **Unit (storage):** `x_token_store` upsert→get round-trips and the on-disk
  bytes are ciphertext (not the plaintext token); `update_tokens` replaces
  atomically; `get` on a missing wallet is `None`; a wrong `X_TOKEN_ENC_KEY`
  makes `get` raise (no silent plaintext).
- **Integration (aiohttp test client, X HTTP boundary mocked):** `GET
  /api/x/connect` returns an authorize URL and stashes a verifier; `GET
  /api/x/callback` with a valid signed state + mocked token/`users/me` responses
  writes an `x_accounts` row; a tampered `state` → 400; `POST /api/x/share`
  without a connected account → 409; with an expired access token, refresh is
  called and the rotated tokens are persisted *before* the post; over-budget →
  429; `DELETE /api/x/connect` deletes the row even when the mocked revoke 5xxs.
- **Manual smoke (ops, needs public HTTPS + real X OAuth2 app):** connect a
  personal X account end-to-end, share one testnet mint, confirm the tweet
  carries the NFT image + card URL, disconnect, confirm re-share is refused.
- **Gate:** full `pytest`, `ruff`/`ruff-format`/`mypy`/`gitleaks`,
  `validate-trait-config` all green (never `--no-verify`). New test files carry
  the `os.environ.setdefault` `BUNNY_PULL_ZONE`/`LAYER_SOURCE` env-guard preamble.
