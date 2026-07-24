# Per-user OAuth2 PKCE for X — "Share from my account" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user connect their OWN X/Twitter account via OAuth2 Authorization
Code + PKCE and post a mint to it server-side ("Share from my account"), with
Fernet-encrypted per-user token storage, rotating-refresh handling, a spend
budget, and no secrets in the client — phase 3 of #41, per issue #252 and the
design at `docs/superpowers/specs/2026-07-24-x-per-user-oauth2-pkce-share-design.md`.

**Architecture:** Four independent seams — (1) encrypted token store
(`lfg_core/x_token_store.py`), (2) OAuth2 PKCE + posting client
(`lfg_core/x_oauth.py`), (3) service endpoints (`lfg_service/app.py`:
connect/callback/status/disconnect/share), (4) client UI
(`webapp/client/app.js`). Config (`lfg_core/config.py`) is the shared,
feature-off-when-unset seam. No mint/swap/economy code is touched.

**Tech Stack:** Python 3 / aiohttp / asyncio / sqlite3 / `cryptography` (Fernet) /
pytest; vanilla no-build JS client.

## Global Constraints

- **No XRPL transaction in this feature** — posting a tweet touches no ledger,
  so **SourceTag=2606160021 and provenance memos do NOT apply** and no tx is
  built. (Do not add a SourceTag/memo path.)
- **No secrets in the client:** OAuth2 client secret, PKCE `code_verifier`, and
  `X_TOKEN_ENC_KEY` are server-side only; the browser sees only the authorize
  URL and an opaque HMAC-signed `state`.
- **Tokens Fernet-encrypted at rest**; refresh rotation persisted **atomically
  before** the new access token is used.
- **Feature off when env unset** (`X_USER_SHARE_ENABLED` + all creds present),
  mirroring `config.py`'s existing `X_ENABLED` pattern.
- Pre-push gate — `ruff` / `ruff-format` / `mypy` / `gitleaks` / `pytest` /
  `validate-trait-config` — must pass; **never** `--no-verify`.
- Any `app.js` change **bumps the cache-buster** `app.js?v=32` in
  `webapp/client/index.html` in the same commit.
- New test files start with the env-guard preamble:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "test-zone")
  os.environ.setdefault("LAYER_SOURCE", "local")
  ```

---

### Task 1: Config knobs + encrypted token store

**Files:**
- Modify: `lfg_core/config.py` (new `X_OAUTH2_*`, `X_TOKEN_ENC_KEY`,
  `X_USER_SHARE_ENABLED`), `requirements.txt` (pin `cryptography`)
- Create: `lfg_core/x_token_store.py`
- Test: `tests/test_x_token_store.py`

**Interfaces:**
- Produces: `config.X_OAUTH2_CLIENT_ID/SECRET/REDIRECT_URI`,
  `config.X_TOKEN_ENC_KEY`, `config.X_USER_SHARE_ENABLED` (bool, true only when
  flag set AND all four creds present);
  `x_token_store.ensure_x_accounts_table()`,
  `upsert(wallet, x_user_id, x_handle, access, refresh, expires_at)`,
  `get(wallet) -> XAccount | None`,
  `update_tokens(wallet, access, refresh, expires_at)`, `delete(wallet)`.
- Consumes: `config.DB_PATH` (`lfg_core/user_db.DATABASE`), `config.X_TOKEN_ENC_KEY`.

- [ ] **Step 1: Write the failing test(s)** — `tests/test_x_token_store.py` with
  the env-guard preamble; set `X_TOKEN_ENC_KEY` to a generated Fernet key and
  point `config.DB_PATH` at a tmp DB. Assert: upsert→get round-trips
  `x_user_id`/`x_handle`/`access`/`refresh`/`expires_at`; the raw
  `access_token_enc`/`refresh_token_enc` bytes read straight from sqlite do NOT
  contain the plaintext token; `update_tokens` replaces both tokens + expiry in
  one row; `get` on an unknown wallet is `None`; `delete` removes the row;
  constructing the store with a wrong key makes `get` raise (no silent
  plaintext). Also assert `config.X_USER_SHARE_ENABLED` is `False` when the flag
  is set but a cred is missing.
  ```python
  def test_upsert_get_roundtrip_and_ciphertext(tmp_db):
      x_token_store.ensure_x_accounts_table()
      x_token_store.upsert("rWALLET", "42", "alice", "atk", "rtk", 1_800_000_000)
      acc = x_token_store.get("rWALLET")
      assert acc.access_token == "atk" and acc.refresh_token == "rtk"
      raw = _read_raw_blob("rWALLET")   # direct sqlite read
      assert b"atk" not in raw and b"rtk" not in raw
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_x_token_store.py -q` → fails (module/config attrs absent).
- [ ] **Step 3: Implement** — add the config vars (`X_USER_SHARE_ENABLED =
  env_flag("X_USER_SHARE_ENABLED", "0") and all((X_OAUTH2_CLIENT_ID,
  X_OAUTH2_CLIENT_SECRET, X_OAUTH2_REDIRECT_URI, X_TOKEN_ENC_KEY))`, matching the
  `X_ENABLED` idiom at `config.py:393`); pin `cryptography` in
  `requirements.txt`; write `x_token_store.py` (self-migrating
  `CREATE TABLE IF NOT EXISTS x_accounts`, `Fernet(config.X_TOKEN_ENC_KEY)`,
  sqlite3 like `identity.py`, `XAccount` dataclass with decrypted fields).
- [ ] **Step 4: Run to verify they pass** — same pytest command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/test_x_token_store.py tests/ -q -k "config or identity or x_"`.
- [ ] **Step 6: Commit** — `feat(x): encrypted per-user X token store + OAuth2 config (#252)`.

---

### Task 2: OAuth2 PKCE + posting client (`lfg_core/x_oauth.py`)

**Files:**
- Create: `lfg_core/x_oauth.py`
- Test: `tests/test_x_oauth.py`

**Interfaces:**
- Produces: `new_pkce() -> tuple[str, str]`;
  `authorize_url(client_id, redirect_uri, state, challenge) -> str`;
  `async exchange_code(session, code, verifier) -> TokenSet`;
  `async refresh(session, refresh_token) -> TokenSet`;
  `async revoke(session, token) -> None`;
  `async post_tweet(session, access_token, text, media_id=None) -> str`;
  `async upload_media(session, access_token, image_bytes, mime) -> str`.
  `TokenSet` = `(access_token, refresh_token, expires_in)`.
- Consumes: `config.X_OAUTH2_CLIENT_ID/SECRET/REDIRECT_URI`; `aiohttp.ClientSession`.

- [ ] **Step 1: Write the failing test(s)** — `tests/test_x_oauth.py`
  (env-guard preamble). Pure assertions: `new_pkce()` returns a 43–128-char
  URL-safe verifier and a challenge equal to
  `base64.urlsafe_b64encode(sha256(verifier)).rstrip("=")`; `authorize_url`
  contains `response_type=code`, `code_challenge_method=S256`, the exact scope
  `tweet.read tweet.write users.read offline.access` (url-encoded), the
  `client_id`, `redirect_uri`, `state`, and `code_challenge`. HTTP assertions
  with a stubbed `aiohttp` session (fake response returning canned JSON):
  `exchange_code` sends `grant_type=authorization_code` + Basic client auth +
  `code_verifier` and parses `access_token`/`refresh_token`/`expires_in`;
  `refresh` sends `grant_type=refresh_token`; `post_tweet` sends the bearer
  header and returns `data.id`.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_x_oauth.py -q` → import/attr failure.
- [ ] **Step 3: Implement** — `x_oauth.py`; reuse the endpoint constants and the
  `data.<key>` response-parsing shape proven in `surfaces/x_bot/x_api.py`
  (`TWEET_CREATE_URL`, `USERS_ME_URL`, `MEDIA_UPLOAD_URL`), but with an OAuth2
  `Authorization: Bearer` header instead of OAuth 1.0a signing. Token endpoint
  `https://api.x.com/2/oauth2/token`, authorize `https://x.com/i/oauth2/authorize`,
  revoke `https://api.x.com/2/oauth2/revoke`.
- [ ] **Step 4: Run to verify they pass** — same command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/test_x_oauth.py -q` plus a quick `-k "x_"` sweep.
- [ ] **Step 6: Commit** — `feat(x): OAuth2 PKCE + user-context posting client (#252)`.

---

### Task 3: Connect / callback / status / disconnect endpoints

**Files:**
- Modify: `lfg_service/app.py` (route registration near `app.router.add_get`
  block ~line 5421; `on_startup` to call `x_token_store.ensure_x_accounts_table`;
  `_x_oauth_state`/`_verify_x_oauth_state` helpers modeled on
  `make_session_token`/`verify_session_token`; `_x_connect_pending` map)
- Test: `tests/test_x_connect_endpoints.py`

**Interfaces:**
- Produces: `GET /api/x/connect` (`@require_wallet`) → `{authorize_url}`;
  `GET /api/x/callback?code=&state=` (public) → HTML page + row write;
  `GET /api/x/status` (`@require_wallet`) → `{connected, x_handle}`;
  `DELETE /api/x/connect` (`@require_wallet`) → row delete.
- Consumes: `x_oauth.*`, `x_token_store.*`, `config.X_USER_SHARE_ENABLED`,
  `_session_secret()`, `require_wallet`.

- [ ] **Step 1: Write the failing test(s)** — aiohttp test client
  (`AioHTTPTestCase`-style, matching existing `tests/` app-endpoint tests), X
  HTTP boundary mocked (monkeypatch `x_oauth.exchange_code`/`users_me`). With
  `X_USER_SHARE_ENABLED` on: `GET /api/x/connect` returns an authorize URL and a
  verifier is stashed; `GET /api/x/callback` with the signed `state` from that
  connect + mocked token exchange writes an `x_accounts` row and returns 200
  HTML; a tampered/expired `state` → 400 with no row; `GET /api/x/status`
  reflects connected/`x_handle`; `DELETE /api/x/connect` deletes the row even
  when the mocked `revoke` raises. With the flag off, `/api/x/connect` → 403/404.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_x_connect_endpoints.py -q`.
- [ ] **Step 3: Implement** — the four handlers + `state` HMAC helpers + the
  short-TTL `_x_connect_pending` map; register routes; wire
  `ensure_x_accounts_table` into `on_startup`. Callback returns a minimal HTML
  "connected as @handle — return to LFG" page.
- [ ] **Step 4: Run to verify they pass** — same command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/ -q -k "app or x_ or session"` to catch route/startup regressions.
- [ ] **Step 6: Commit** — `feat(x): connect/callback/status/disconnect endpoints (#252)`.

---

### Task 4: `POST /api/x/share` — post on the user's behalf + refresh + budget

**Files:**
- Modify: `lfg_service/app.py` (handler + route)
- Create: `lfg_core/x_share_budget.py` (per-user + global spend cap; see spec
  Open Q1 — implement the recommended self-contained `x_share_log` table +
  `X_USER_SHARE_MONTHLY_BUDGET` knob)
- Test: `tests/test_x_share.py`

**Interfaces:**
- Produces: `POST /api/x/share` (`@require_wallet`) `{nft_number}` →
  `{tweet_id, url}`; `x_share_budget.check_and_reserve(wallet) -> bool`,
  `record(wallet, tweet_id)`.
- Consumes: `x_token_store.get/update_tokens/delete`, `x_oauth.refresh/
  upload_media/post_tweet`, the NFT `image_url`/traits lookup by `nft_number`
  (on-chain index / LFG table helpers already used by the poster path).

- [ ] **Step 1: Write the failing test(s)** — `tests/test_x_share.py`, X + image
  HTTP mocked. Assert: no connected account → 409 `x_not_connected`; an expired
  `expires_at` triggers `x_oauth.refresh` AND `x_token_store.update_tokens` is
  called (rotated tokens persisted) **before** `post_tweet`; a refresh
  `invalid_grant` deletes the row and returns 409 `x_reauth_required`;
  over-budget → 429 `x_budget_reached` with no post; happy path uploads media +
  posts and returns `{tweet_id, url}`; a duplicate same-(wallet,nft_number)
  share is deduped/limited. Order assertion (refresh-before-post) via a call
  recorder.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest
  tests/test_x_share.py -q`.
- [ ] **Step 3: Implement** — the handler (steps 1–5 of spec §4 bullet
  `POST /api/x/share`), the budget module, and the by-`nft_number`
  image_url/traits resolution. Refresh path does the atomic
  `update_tokens` before using the new access token.
- [ ] **Step 4: Run to verify they pass** — same command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest
  tests/ -q -k "x_ or share or app"`.
- [ ] **Step 6: Commit** — `feat(x): share-from-my-account post endpoint w/ refresh + budget (#252)`.

---

### Task 5: Client "Share from my account" control + Web-Intent fallback

**Files:**
- Modify: `webapp/client/app.js` (extend `buildShareControl`; add
  `xStatus()`/`connectX()`/`shareFromMyX()`), `webapp/client/index.html`
  (bump `app.js?v=32` → `v=33`)
- Test: `webapp/tests/` smoke (extend existing client smoke test if present;
  otherwise a minimal DOM/behavior assertion)

**Interfaces:**
- Produces: a "Connect X" / "Post to @handle" control beside the existing
  Web-Intent anchor on the mint- and swap-success panels, gated on
  `/api/x/status`.
- Consumes: `GET /api/x/status`, `GET /api/x/connect`, `POST /api/x/share`.

- [ ] **Step 1: Write the failing test(s)** — extend the webapp smoke to assert
  the share panel still renders the Web-Intent anchor when share is unavailable
  (fallback preserved), and that a connected `/api/x/status` renders the "Post
  to @handle" control. (If no JS test harness exists for this panel, assert via
  the existing `webapp/tests` smoke that `/api/x/status` is wired and the
  `app.js` cache-buster bumped — keep it light.)
- [ ] **Step 2: Run to verify they fail** — run the webapp smoke test command
  used elsewhere in `webapp/tests/`.
- [ ] **Step 3: Implement** — the client control + fetches; the always-present
  Web-Intent fallback stays. Bump the cache-buster in the SAME commit.
- [ ] **Step 4: Run to verify they pass** — rerun the smoke.
- [ ] **Step 5: Wider suite / regression run** — full `webapp/tests` smoke +
  `.venv/bin/python -m pytest -q`.
- [ ] **Step 6: Commit** — `feat(x): Share-from-my-account client control + fallback (#252)` (includes `index.html` cache-buster bump).

---

### Final Task: Full gate + PR

- [ ] Run the full pre-push gate locally: `.venv/bin/python -m pytest -q`,
  `ruff check .`, `ruff format --check .`, `mypy` (from project `.venv`),
  `gitleaks`, `validate-trait-config` — all green, never `--no-verify`.
- [ ] Confirm the feature is fully OFF with the new env unset (no route errors,
  client hides the button, existing Web-Intent share unchanged).
- [ ] Push the branch; `gh pr create` **non-draft**, no AI attribution in the PR
  body or commits (per repo rules). Body: summary, the #252 link, the ops gates
  (public HTTPS callback registered in the X OAuth2 app, `X_OAUTH2_*` +
  `X_TOKEN_ENC_KEY` set, budget decision from spec Open Q1/Q2), and the note
  that no XRPL tx / SourceTag is involved.
- [ ] Wait for **Greptile + CodeRabbit**; resolve every actionable finding (fix
  in code AND reply on its thread naming the fixing commit) before merge.
