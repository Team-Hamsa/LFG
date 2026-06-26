# Unified User Accounts — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the **wallet** a first-class account: add an inverse lookup (wallet → identities), a per-identity `display_handle` kept fresh, a `GET /api/account` view, and an explicit Xaman-proof "link another surface" flow. Preserve cross-surface isolation exactly.

**Design:** [`docs/superpowers/specs/2026-06-26-unified-accounts-design.md`](../specs/2026-06-26-unified-accounts-design.md). Issue [#90](https://github.com/Team-Hamsa/LFG/issues/90).

**Architecture:** Extend `identities` in place (no new `accounts` table) — `display_handle` + `updated_at` columns, a `wallet` index, an `identities_for_wallet` helper. The link flow reuses the existing `/api/signin` machinery (proving the *same* wallet on a 2nd surface *is* the link); a thin account-aware wrapper + SDK methods + bot command surface it.

**Tech Stack:** Python 3, aiohttp (service), `xrpl-py` (`is_valid_classic_address`), `surfaces._client.LFGServiceClient`, repo-native **sync** tests (`asyncio.new_event_loop()` + direct call — NOT pytest-asyncio), `sqlite3`.

## Global Constraints

- **Backward compatibility:** `ensure_identities_table()` stays idempotent and self-migrating; existing rows keep PK/wallet, `display_handle` backfills from `platform_username`. The webapp default `platform="discord"` behavior is byte-identical. Pin with a regression test.
- **XRPL addresses are case-sensitive — never `.lower()` a wallet.** Store/compare verbatim; rely on `is_valid_classic_address` as the gate.
- **Cross-surface isolation preserved:** linking is wallet-proof-gated, never id-collision-based. Link payload ownership stays `(platform, user_id)`-keyed. Legacy `Users` write stays discord-only.
- **Inverse lookup is in-process only** — no HTTP endpoint maps an arbitrary wallet → identities (privacy). `GET /api/account` returns only the caller's own account.
- **Test style:** repo-native sync (`def test_...` + a `_run(coro)` loop helper, the `_Req` shim from `tests/test_service_signin_platform.py`). Seeds, if needed, use the throwaway `sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r`.
- **mypy:** `lfg_service.app` + `surfaces.*` are in the relaxed override; run the FULL `.venv/bin/mypy .` before claiming clean.
- **Resolve O1 before Task 5** (separate `/api/link/*` endpoints vs. a `link=true` flag). Plan assumes a `link=true` flag on the existing sign-in handlers with `link_*` SDK aliases; adjust if the user chooses separate endpoints.

---

### Task 1: Schema migration — `display_handle`, `updated_at`, wallet index

Unblocks: foundation for all downstream (#85, #89, #91).

**Files:**
- Modify: `lfg_service/identity.py` — `ensure_identities_table()`
- Test: `tests/test_identity.py` (extend)

**Interfaces:** `ensure_identities_table()` adds `display_handle TEXT`, `updated_at TIMESTAMP`, and `idx_identities_wallet` if absent; backfills `display_handle = platform_username` for existing rows. Idempotent and forward-only.

- [ ] **Step 1: Write failing tests.**
  - `test_ensure_adds_display_handle_column` — create an *old-shape* `identities` table (base DDL, no `display_handle`), insert a row, call `ensure_identities_table()`, assert `PRAGMA table_info` now lists `display_handle` and `updated_at`, and the existing row's `display_handle` equals its `platform_username`.
  - `test_ensure_creates_wallet_index` — after `ensure_identities_table()`, assert `idx_identities_wallet` appears in `SELECT name FROM sqlite_master WHERE type='index'`.
  - `test_ensure_is_idempotent_twice` — calling it twice does not raise and does not duplicate columns/index.
- [ ] **Step 2:** Implement the `PRAGMA table_info` introspection + guarded `ALTER TABLE` + backfill + `CREATE INDEX IF NOT EXISTS` (per spec §2). Run tests green.
- [ ] **Step 3:** Confirm existing `test_identity.py` tests still pass (link/resolve/migrate unaffected).

---

### Task 2: Inverse lookup — `identities_for_wallet`

Unblocks: **#85** (announcement handle), **#91** (firehose enrichment) — both consume this primitive.

**Files:**
- Modify: `lfg_service/identity.py` — add `identities_for_wallet(wallet) -> list[dict]`
- Test: `tests/test_identity.py` (extend)

**Interfaces:** `identities_for_wallet(wallet)` → list of `{platform, platform_user_id, display_handle, platform_username, created_at, updated_at}`, ordered by `created_at`; `[]` when none. Verbatim wallet match (no case folding).

- [ ] **Step 1: Write failing tests.**
  - `test_identities_for_wallet_returns_all_linked` — link `discord:1` and `telegram:2` to `rW`, plus `discord:3` to `rOTHER`; assert `identities_for_wallet("rW")` returns exactly the two `rW` rows with their platforms/handles.
  - `test_identities_for_wallet_empty` — unknown wallet → `[]`.
  - `test_identities_for_wallet_is_case_sensitive` — `rW` vs `rw` are distinct (proves no `.lower()`).
- [ ] **Step 2:** Implement the `SELECT ... WHERE wallet = ?` helper. Run green.

---

### Task 3: Display-handle capture & refresh

Unblocks: **#85**, **#91** (fresh handle to render).

**Files:**
- Modify: `lfg_service/identity.py` — `link(...)` gains `display_handle` + `updated_at`; add `touch_handle(platform, user_id, handle)`
- Modify: `lfg_service/app.py` — `handle_me` best-effort `touch_handle`
- Test: `tests/test_identity.py`, `tests/test_service_*` (extend / new)

**Interfaces:**
- `link(platform, puid, platform_username, wallet, *, display_handle=None)` — `display_handle` defaults to `platform_username`; upsert also sets `display_handle`, `updated_at=CURRENT_TIMESTAMP`.
- `touch_handle(platform, user_id, handle) -> None` — best-effort `UPDATE identities SET display_handle=?, updated_at=CURRENT_TIMESTAMP WHERE platform=? AND platform_user_id=?`; no-op if the row doesn't exist or handle unchanged.
- `handle_me` calls `touch_handle(_platform(user), user["id"], user["name"])` (best-effort, never blocks the response).

- [ ] **Step 1: Write failing tests.**
  - `test_link_sets_display_handle_default` — `link(...)` with no `display_handle` → row's `display_handle == platform_username` and `updated_at` is non-NULL.
  - `test_link_explicit_display_handle` — explicit `display_handle="Alice"` is stored.
  - `test_touch_handle_updates` — after `link`, `touch_handle` with a new handle changes `display_handle` and bumps `updated_at`; on a missing row it's a no-op (no raise).
  - `test_handle_me_refreshes_handle` (service) — using the `_Req`/`make_session_token` harness, call `handle_me` and assert `touch_handle` was invoked with the token's current name (monkeypatch `identity_store.touch_handle` to capture args). Response shape unchanged (`{id, username, wallet}`).
- [ ] **Step 2:** Implement. Keep `link`'s existing signature backward-compatible (keyword-only new arg). Run green.
- [ ] **Step 3:** Verify `register`/`signin` callers still pass (they call `link` positionally; the new arg is keyword-only/defaulted).

---

### Task 4: `GET /api/account` + SDK `account()`

Unblocks: **#89** (Mini App single account object).

**Files:**
- Modify: `lfg_service/app.py` — `handle_account` (`@require_wallet`), route registration
- Modify: `surfaces/_client/client.py` — `account(user_id, *, username="")`
- Test: `tests/test_service_*` (new), `tests/test_sdk_*` (extend)

**Interfaces:**
- `GET /api/account` (`@require_wallet`) → `{wallet, identities: [...]}` from `identities_for_wallet(request["wallet"])`; `400 {"error":"no wallet registered"}` when unregistered (inherited from `require_wallet`). Caller sees only their own account.
- SDK `account(user_id, *, username="")` → `await self._user_request("GET", "/api/account", user_id, username=username)`.

- [ ] **Step 1: Write failing tests.**
  - Service: `test_account_returns_caller_identities` — seed `identities` (monkeypatch `identity_store.identities_for_wallet`), drive `handle_account` with a token whose wallet resolves; assert `{wallet, identities}` shape and that it used the *resolved* wallet (not a client-supplied one).
  - Service: `test_account_no_wallet_400` — unregistered caller → 400.
  - SDK: `test_account_calls_endpoint` — using the SDK test harness (see `tests/test_sdk_*`), assert `account(...)` issues `GET /api/account` with the user session token.
- [ ] **Step 2:** Implement handler + route (`app.router.add_get("/api/account", require_wallet(handle_account))`) + SDK method. Run green.

---

### Task 5: Link flow — service (account-aware sign-in)

Unblocks: **#85/#91** (cross-surface handles only *exist* once users link).

> **Decision gate (O1):** this task assumes a `link=true` flag on the existing sign-in handlers. If the user chose separate `/api/link/*` endpoints, split accordingly — the test assertions are the same.

**Files:**
- Modify: `lfg_service/app.py` — `handle_signin_start` (record `link` intent in `signin_payloads[uuid]`), `handle_signin_status` (on `signed`, when link-intent, include `"account": {wallet, identities}` in the response)
- Test: `tests/test_service_link_flow.py` (new)

**Interfaces:**
- `POST /api/signin` accepts optional `{"link": true}`; stores it on the payload record. Response unchanged (`{uuid, signin_link}`).
- `GET /api/signin/{uuid}` on `signed`: links as today (`identity.link(platform, user_id, name, wallet)`); when the payload had link-intent, the response also carries `"account": {"wallet": wallet, "identities": identities_for_wallet(wallet)}`. Ownership check unchanged: `(platform, user_id)`-keyed.

- [ ] **Step 1: Write failing tests.**
  - `test_link_signed_attaches_and_returns_account` — payload `{platform:telegram, user_id:T, link:True}`; monkeypatch XUMM `get_payload_status` → signed with `account=rWALLET`, monkeypatch `identity.link` and `identity.identities_for_wallet` to return both `discord:D` and `telegram:T`. Assert response has `state="signed"`, `wallet=rWALLET`, and `account.identities` lists both surfaces.
  - `test_link_cross_platform_ownership_404` — a `discord` token cannot complete a `telegram` link payload (mirror `test_signin_status_cross_platform_404`).
  - `test_link_legacy_users_stays_discord_only` — a `telegram` link does **not** call `register_user` (assert legacy write not invoked); a `discord` link does.
  - `test_signin_without_link_flag_unchanged` — plain sign-in response has **no** `account` key (regression: byte-identical to today).
- [ ] **Step 2:** Implement the flag + account enrichment. Run green.
- [ ] **Step 3:** Run the full existing `tests/test_service_signin_platform.py` + `test_service_platform_register.py` to prove the non-link path is untouched.

---

### Task 6: Link flow — SDK

Unblocks: surface wiring (Task 7).

**Files:**
- Modify: `surfaces/_client/client.py` — `link_start`, `link_status`, `wait_for_link`
- Test: `tests/test_sdk_*` (extend, mirroring `test_sdk_signin_poll.py`)

**Interfaces:**
- `link_start(user_id, *, username="")` → `POST /api/signin` with `json={"link": True}`.
- `link_status(user_id, uuid)` → `GET /api/signin/{uuid}`.
- `wait_for_link(user_id, uuid, *, interval=2.0, timeout=180.0, sleep=asyncio.sleep)` → `_poll(lambda: self.link_status(...), SIGNIN_TERMINAL, ...)` (reuses the existing terminal set, `client.py:26`).

- [ ] **Step 1: Write failing tests.**
  - `test_link_start_sends_link_flag` — assert `link_start` POSTs to `/api/signin` with `{"link": True}` and the user session token.
  - `test_wait_for_link_polls_to_signed` — drive `wait_for_link` against a fake status sequence `pending → signed`, assert it returns the signed dict (incl. `account`).
- [ ] **Step 2:** Implement. Run green. Update `tests/test_sdk_exports.py` if it pins the public method set.

---

### Task 7: Surface wiring — `/link` (and `/account`) on both bots

Unblocks: end-to-end #90; downstream #85/#91 now have real linked handles to render.

**Files:**
- New: `surfaces/discord_bot/link_view.py`, `surfaces/telegram_bot/link_view.py` (modeled on the existing `register_view.py` in each)
- Modify: `surfaces/discord_bot/bot.py`, `surfaces/telegram_bot/bot.py` — register the `/link` command (+ optional `/account`)
- Modify: `surfaces/discord_bot/render.py`, `surfaces/telegram_bot/render.py` — an account/linked-confirmation renderer
- Test: `tests/test_discord_link.py`, `tests/test_telegram_link.py` (new), mirroring `test_discord_register.py` / `test_telegram_register.py`

**Interfaces:** `handle_link(svc, interaction|update, ...)` drives `svc.link_start` → show QR (`svc.qr_png`) → `svc.wait_for_link` → on `signed`, render *"Linked to your account"* listing the other surfaces from `final["account"]["identities"]` (excluding the current one). Telegram package must **never import `discord`**; share cross-surface text via `surfaces/_shared/*`.

- [ ] **Step 1: Write failing tests** (per bot, with fake interaction/update + fake `LFGServiceClient`, as in the register tests):
  - `test_link_shows_qr_then_confirms` — happy path: `link_start` → QR sent → `wait_for_link` returns `signed` with a two-identity `account` → confirmation lists the *other* surface's handle.
  - `test_link_signed_different_wallet_only_self` — `account` lists only the current identity (signed a fresh wallet) → message shows no other surfaces.
  - `test_link_service_error_reports_friendly` — `ServiceError` from `link_start` → friendly error (reuse `friendly_error`).
- [ ] **Step 2:** Implement `link_view.py` for each bot + render helper + command registration. Run per-bot tests green.
- [ ] **Step 3 (optional, same task):** add `/account` driving `svc.account(...)` to show the user their linked surfaces without re-signing. Test `test_account_lists_linked_surfaces`.

---

### Task 8: Downstream readiness — announcement handle (#85 spike) [optional, gated]

Unblocks: directly delivers **#85**; de-risks **#91**.

> Only if the user wants #90 to *land* the #85 win rather than just unblock it. Otherwise stop after Task 7 and let #85 consume the primitives.

**Files:**
- Modify: `surfaces/telegram_bot/events.py` — `make_announcement` uses `identity.identities_for_wallet(ev.wallet)` to render a real handle (prefer same-platform handle; fall back to any handle, then wallet)
- Test: `tests/test_telegram_events.py` (extend)

- [ ] **Step 1: Write failing test** — `make_announcement` for an event with a wallet that has a linked Telegram handle renders that handle (not "a user"); with only a Discord handle, renders the Discord handle; with none, falls back to the wallet.
- [ ] **Step 2:** Implement (in-process lookup, no HTTP). Run green.

---

## Done-When

- [ ] `.venv/bin/pytest tests/` green (new + existing).
- [ ] `.venv/bin/mypy .` clean.
- [ ] `.venv/bin/ruff format --check .` clean (pre-push runs ruff format).
- [ ] Existing sign-in / register / platform-isolation tests unchanged and passing — isolation guarantee provably intact.
- [ ] No `.lower()` applied to any wallet anywhere in the diff.
