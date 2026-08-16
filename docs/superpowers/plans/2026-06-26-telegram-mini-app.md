# Telegram Mini App — Implementation Plan (TDD)

**Issue:** [Team-Hamsa/LFG #89](https://github.com/Team-Hamsa/LFG/issues/89)
**Spec:** `docs/superpowers/specs/2026-06-26-telegram-mini-app-design.md`
**Depends on:** #87 (launch buttons), #90 (account/auth mapping) — both merged.

Tasks are ordered and each names the test that proves it. **Part A is codeable
now** (validator, endpoint, client boot, launch button, config). **Part B is
ops/deployment** and needs the user (HTTPS exposure, BotFather). Do A first; A
is fully testable and mergeable behind the feature flag (no public URL → 503 /
button omitted), so it lands independently of B.

---

## Resolved Decisions (baked into the Part A PR)

These answer the §9 open questions for the implementation that shipped:

1. **Hosting — DEFERRED to ops.** Part A ships FEATURE-FLAGGED OFF. With
   `TELEGRAM_MINI_APP_URL` unset, no launch button is shown and no menu button
   is set; with the service-side bot token unset, `POST /api/telegram/auth`
   returns 503. The PR merges safely with the feature dormant. HTTPS/BotFather
   (Part B) remain the user's ops step.
2. **Launch surface — BOTH.** The BotFather chat menu button is set
   programmatically in the bot's `_post_init` (only when `TELEGRAM_MINI_APP_URL`
   is set) AND a "🎮 Open App" `WebAppInfo` inline button is added to the
   `/start` menu (only when the URL is set).
3. **Registration gate — load + prompt Xaman inline.** The Mini App loads for
   everyone; unregistered Telegram users get the existing inline Xaman sign-in
   prompt (mirrors the Discord Activity). `/api/telegram/auth` NEVER
   auto-creates or auto-looks-up a wallet.
4. **`telegram-web-app.js` — vendored same-origin** (`webapp/client/`), not
   hotlinked from the CDN.
5. **initData max-age — 3600s** (`TELEGRAM_INITDATA_MAX_AGE` default), the only
   replay guard.

---

## Part A — Codeable now

### A1. `initData` HMAC validator (pure, unit-tested first)

**File:** `lfg_service/telegram_auth.py`
**Function:**
`validate_init_data(init_data: str, bot_token: str, max_age: int, now: int | None = None) -> dict | None`
Returns the parsed fields dict (with `user` decoded to a dict) on success, else
`None`. Pure — no I/O, no globals; `now` injectable for deterministic staleness
tests.

**Algorithm** (per spec §3.1): parse query string → pull out `hash` (and drop
`signature`) → build sorted `key=value\n…` data-check-string → `secret_key =
HMAC_SHA256("WebAppData", bot_token)` → `calc = hex(HMAC_SHA256(secret_key,
dcs))` → `compare_digest(calc, hash)` → staleness check on `auth_date`.

**Tests — `tests/test_telegram_initdata.py` (write FIRST, red):**

Worked test-vector approach (self-generating, no secret needed from Telegram):
- A `_sign(fields: dict, bot_token: str) -> str` test helper builds a *valid*
  `initData` string the same way Telegram would (sorted dcs, the
  `"WebAppData"`-keyed HMAC). This lets every test construct known-good and
  known-bad vectors from a fixed dummy `bot_token` (e.g.
  `"123456:TEST-FAKE-TOKEN"`), so the validator and the signer must agree on
  the exact algorithm — that mutual agreement is the test's strength.

1. `test_valid_initdata_accepted` — sign `{auth_date: now, query_id, user:
   '{"id":55,"username":"alice"}'}` with the dummy token; validator returns a
   dict whose `user["id"] == 55`.
2. `test_tampered_hash_rejected` — flip one char of `hash` → `None`.
3. `test_tampered_field_rejected` — sign valid, then mutate `user` after signing
   → `None` (dcs no longer matches the hash).
4. `test_wrong_bot_token_rejected` — sign with token A, validate with token B →
   `None`.
5. `test_stale_auth_date_rejected` — `auth_date = now - 7200`, `max_age=3600`,
   `now=now` → `None`.
6. `test_fresh_auth_date_accepted` — `auth_date = now - 60`, `max_age=3600` → ok.
7. `test_missing_hash_rejected` / `test_missing_user_handled` — malformed inputs
   → `None`, never raises.
8. `test_signature_field_ignored` — include a junk `signature=` field (Ed25519
   scheme) alongside a valid HMAC `hash`; still validates (signature dropped
   from dcs).
9. `test_constant_time_compare_used` — assert `hmac.compare_digest` path (smoke:
   a near-miss hash differing only in the last byte is rejected).

> Sourcing a *real* Telegram-signed vector is also valuable as a regression
> anchor: if the user can paste one real `initData` + the corresponding bot
> token into a gitignored fixture, add `test_real_vector` to prove the validator
> matches Telegram's production output, not just our own signer. Optional —
> the self-signed vectors already pin the algorithm.

### A2. `/api/telegram/auth` endpoint (session-token mint)

**File:** `lfg_service/app.py` — add `handle_telegram_auth`, route it in
`create_app` (`app.router.add_post("/api/telegram/auth", handle_telegram_auth)`).

Behavior (spec §3.2): read `init_data` from JSON body; if
`config.TELEGRAM_BOT_TOKEN` is empty → `503 {"code":"telegram_not_configured"}`;
validate via A1; on failure → `401 {"code":"bad_initdata"}`; on success mint
`make_session_token({"id": str(tg_id), "name": handle, "platform":
"telegram"})` and return `{session_token, user:{id, username}}`. **No wallet
creation/lookup.** Never log `init_data`.

**Tests — `tests/test_telegram_auth_endpoint.py`:**
1. `test_auth_returns_telegram_session_token` — monkeypatch a valid `init_data`
   (sign with a patched `config.TELEGRAM_BOT_TOKEN`); assert 200 and that
   `verify_session_token(resp token)["platform"] == "telegram"` and `id` matches.
2. `test_auth_rejects_bad_initdata` — garbage `init_data` → 401.
3. `test_auth_503_when_unconfigured` — `TELEGRAM_BOT_TOKEN=""` → 503.
4. `test_auth_does_not_register_wallet` — monkeypatch `register_user` /
   `identity_store.link` to fail the test if called; assert they are NOT called.
5. `test_telegram_token_cannot_read_discord_session` — mirror
   `test_signin_status_cross_platform_404`: mint via `/api/telegram/auth` for
   id `55`, then assert that token gets `404` on a `discord:55`-owned signin
   payload (cross-surface isolation preserved, spec §8).

### A3. Config plumbing

**File:** `lfg_core/config.py` — add (all optional, feature-off when unset):
- `TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")`
- `TELEGRAM_INITDATA_MAX_AGE = int(os.getenv("TELEGRAM_INITDATA_MAX_AGE", "3600"))`

**File:** `surfaces/telegram_bot/config.py` — add:
- `TELEGRAM_MINI_APP_URL = os.getenv("TELEGRAM_MINI_APP_URL", "")` (optional;
  launch button omitted when empty).

**File:** `CLAUDE.md` env block — document the three new vars.

**Test — `tests/test_config_telegram_miniapp.py`:** with the env vars set/unset,
assert the config values resolve and that empty defaults are the feature-off
sentinels.

### A4. Client dual-mode boot (`app.js`)

**File:** `webapp/client/app.js` + `webapp/client/index.html`.

- `index.html`: load the Telegram WebApp bridge **before** `app.js` (vendor
  same-origin per spec §4.1 recommendation: `vendor/telegram-web-app.js`).
  Neutralize the hardcoded "Connecting to Discord…" status text to "Connecting…".
- `app.js`:
  - add `const tg = window.Telegram?.WebApp; const insideTelegram = !!(tg &&
    tg.initData);`
  - add `async function setupTelegram()` (spec §4.2): `tg.ready()`,
    `tg.expand()`, POST `tg.initData` to `/api/telegram/auth`, store
    `sessionToken`, set `externalOpener = (url) => tg.openLink(url)`.
  - branch in `main()`: `insideTelegram → setupTelegram()` else `insideDiscord →
    setupDiscord()` else degraded-mode message; everything after
    `me = await api('/api/me')` unchanged.

**Tests:** the client is no-build vanilla JS with no existing JS test harness.
Prove via:
1. `tests/test_app_js_boot.py` (lightweight, string/AST-grep style): assert
   `app.js` contains the `insideTelegram` branch, calls `/api/telegram/auth`,
   and that the Discord path is still present (regression guard).
2. Manual smoke through `WEBAPP_DEV_MODE=1` mock harness for the shared UI
   (already covered by the dev mock) — no Telegram client needed to exercise
   panels.
3. Defer true end-to-end (real Telegram client opening the Mini App) to Part B
   verification once the public URL exists.

### A5. Telegram launch button (`/app` + menu button)

**Files:** `surfaces/telegram_bot/commands.py`, `surfaces/telegram_bot/bot.py`.

- `commands.py`: add `async def app_cmd(update, context)` that, when
  `config.TELEGRAM_MINI_APP_URL` is set, replies with an inline
  `InlineKeyboardButton("🎮 Open LFG App", web_app=WebAppInfo(url=...))`; when
  unset, replies with a "Mini App not configured yet" message. Add the same
  button to the `/start` menu keyboard (guarded by the URL being set).
- `bot.py`: register `CommandHandler("app", cmds.app_cmd)`; optionally set the
  persistent chat menu button in `_post_init` via
  `application.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(...))` when
  the URL is configured.

**Tests — `tests/test_telegram_app_button.py`** (mirror existing
`commands.py` test style — fake `update`/`context`):
1. `test_app_cmd_sends_webapp_button_when_url_set` — with
   `TELEGRAM_MINI_APP_URL` set, the reply's keyboard contains a button whose
   `web_app.url` equals the configured URL.
2. `test_app_cmd_graceful_when_url_unset` — no URL → a plain "not configured"
   message, no `web_app` button.
3. `test_start_menu_includes_app_button_when_configured` — `/start` keyboard
   gains the app button only when the URL is set.

---

## Part B — Ops / deployment (needs the user)

These cannot be unit-tested in-repo; they are verified by the user against live
Telegram. Do NOT block Part A on these — Part A merges with the feature off.

### B1. HTTPS exposure (THE decision — spec §7)
- User chooses: **Cloudflare Tunnel (recommended)** / reverse proxy + Let's
  Encrypt / reuse existing fronting.
- Provision a stable public HTTPS hostname fronting the whole `lfg_service`
  app on `:8176` (same-origin: static + API together — no CORS).
- **Verify:** `curl https://<host>/api/config` returns 200 over valid TLS from
  off-host.

### B2. Set `TELEGRAM_MINI_APP_URL`
- Set the env var to the B1 hostname in the service `.env` and the Telegram
  bot's environment; restart `lfg-telegram` and `lfg-activity` (pm2).
- **Verify:** `/app` in DM now shows the launch button; the menu button appears.

### B3. BotFather configuration
- `@BotFather → /setmenubutton` (or rely on the programmatic
  `set_chat_menu_button` from A5) pointing at the B1 URL.
- Confirm BotFather accepts the URL (valid HTTPS, port 443).
- **Verify (end-to-end):** open the Mini App from Telegram → it loads, calls
  `/api/telegram/auth`, `/api/me` returns the Telegram identity; an unregistered
  user is prompted to Xaman sign-in inline; registered users land on the mint
  home; mint / swap / dressup all work — full parity with the Discord Activity.

---

## Sequencing & merge strategy

1. A1 → A2 → A3 (validator, endpoint, config) — pure backend, fully unit-tested,
   one PR.
2. A4 → A5 (client boot, launch button) — second PR; safe behind the unset
   `TELEGRAM_MINI_APP_URL` (button omitted) and the 503 (endpoint off).
3. Both A PRs route through CodeRabbit (touch application code — not trivial).
4. Part B is an ops checklist the user runs once the A PRs are merged; B3's
   end-to-end is the acceptance gate for closing #89.
