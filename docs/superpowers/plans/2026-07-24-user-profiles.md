# User Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a first-class `profiles` entity above per-platform
`identities`, populating the reserved `identities.account_id` hook so one profile
owns N identities and M wallets, carries a durable display name / avatar /
preferences, and is read/edited over `/api/profile`. Ships the degenerate
one-profile-per-wallet layer (per spec decision O1a); #206 later widens membership
across wallets.

**Architecture (independent seams):**
1. **DB + helpers** (`lfg_service/identity.py`): `profiles` table (self-migrating
   in `ensure_identities_table`), index on `account_id`, and profile helpers.
2. **Auto-attach** (`lfg_service/app.py` link sites): stamp `account_id` on every
   proven-wallet `link(...)`.
3. **Read/write API** (`lfg_service/app.py`): `GET`/`PATCH /api/profile` + routes;
   profile-first display-name resolution.
4. **SDK** (`surfaces/_client/client.py`): `profile` / `update_profile` methods.

**Tech Stack:** Python 3 / aiohttp / asyncio / sqlite3 / pytest; no client-JS
changes required (no `app.js` / cache-buster bump).

## Global Constraints

- **SourceTag=2606160021 + provenance memos** must be preserved on **any**
  transaction. This feature builds **no** on-ledger transaction (pure app-DB
  metadata), so it emits neither — but do not remove or bypass SourceTag/memo
  logic on any path you touch, and any future tx reading a profile still resolves
  through the identity's `wallet` with SourceTag + memos intact.
- **XRPL wallets are case-sensitive** — store/compare verbatim, NEVER `.lower()`;
  gate any wallet write on `is_valid_classic_address` (already done at link sites).
- **Self-migrating, forward-only DB** — extend `ensure_identities_table()` with
  `CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info`-guarded changes only; no
  down-migration. Runs every boot from `create_app()`.
- **Privacy seam** — a caller sees only their OWN profile; no arbitrary
  wallet → profile HTTP endpoint.
- **Backward compatibility** — `GET /api/account`, `identities_for_wallet`,
  `handle_for_wallet`, `resolve`, `link` contracts must not break; profile layer
  is additive and every wallet-scoped read works when a profile is absent.
- **Pre-push gate** (ruff --fix, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass; never `--no-verify`.
- **Test env-guard preamble** at the top of every new test module importing
  `lfg_core`:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "https://example.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")
  ```

---

### Task 1: `profiles` table + profile helpers in `identity.py`

**Files:**
- Modify: `lfg_service/identity.py`
- Test: `tests/test_identity.py` (extend; already builds an in-memory `identities`
  DDL incl. `account_id` at lines ~52 and ~180)

**Interfaces:**
- Produces: `ensure_profile_for_wallet(wallet: str) -> int`,
  `profile_for_identity(platform, platform_user_id) -> dict | None`,
  `profile_for_wallet(wallet) -> dict | None`,
  `link_identity_to_profile(platform, platform_user_id, profile_id: int) -> bool`,
  `update_profile(profile_id, *, display_name=None, avatar_url=None, preferences: dict | None = None) -> bool`,
  plus a `profiles` table + `idx_identities_account` created by the extended
  `ensure_identities_table()`.
- Consumes: `lfg_core.user_db.DATABASE`, existing `identities` rows.

- [ ] **Step 1: Write the failing test(s)** — in `tests/test_identity.py` (reuse
  its existing in-memory-DB harness / monkeypatched `DATABASE`), add:
  - `test_ensure_creates_profiles_table_idempotent` — call
    `ensure_identities_table()` twice; assert `profiles` and
    `idx_identities_account` exist, no error.
  - `test_ensure_profile_for_wallet_creates_and_is_idempotent` — link two
    identities on the same wallet; `ensure_profile_for_wallet(w)` twice returns
    the same id; both identities' `account_id` equal it; exactly one `profiles`
    row.
  - `test_same_wallet_joins_same_profile_diff_wallet_diff_profile` — proven-wallet
    convergence and divergence.
  - `test_wallet_case_preserved` — a verbatim mixed-case wallet is not folded.
  - `test_update_profile_partial_and_prefs_roundtrip` — patch only
    `display_name`, then `preferences={"announce_opt_out": True}`; assert other
    fields untouched, `preferences` returns as a dict, `updated_at` set.
  - `test_profile_for_identity_none_when_unprofiled`.
  Include the env-guard preamble if the module doesn't already have it.
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_identity.py -k profile -q`
  Expect `AttributeError`/`OperationalError` (helpers/table absent).
- [ ] **Step 3: Implement** — in `identity.py`:
  - extend `ensure_identities_table()` with a `CREATE TABLE IF NOT EXISTS
    profiles (...)` block and `CREATE INDEX IF NOT EXISTS idx_identities_account
    ON identities(account_id)` (mirror the existing `display_handle`/`user_token`
    blocks; commit inside the same `try`).
  - add the five helpers, each owning its `sqlite3.connect(DATABASE)`,
    try/except-log-return-falsy, `finally: close()`. `preferences` JSON-encoded on
    write, `json.loads` on read (default `{}`). `ensure_profile_for_wallet` seeds
    `display_name` from the wallet's best `display_handle`
    (`handle_for_wallet`). NEVER `.lower()` a wallet.
- [ ] **Step 4: Run to verify they pass** — same pytest `-k profile` command.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/test_identity.py -q` (confirm existing
  identity/isolation tests still pass, incl.
  `test_same_user_id_different_platforms_are_distinct`).
- [ ] **Step 6: Commit** — `feat(identity): profiles table + account_id helpers (#207)`

---

### Task 2: Auto-attach identities to a profile at every link site

**Files:**
- Modify: `lfg_service/app.py` (the three `identity_store.link(...)` sites:
  register `~3293`, sign-in `~4426`, web signin `~4537`)
- Test: `tests/test_identity.py` or a small `tests/test_service_profile.py`

**Interfaces:**
- Consumes: `ensure_profile_for_wallet`, `link_identity_to_profile` (Task 1).
- Produces: `identities.account_id` populated after any successful link.

- [ ] **Step 1: Write the failing test(s)** — assert that after a `link(...)` for
  a wallet, the identity's `account_id` is non-NULL and a second `link` on the
  same wallet from a different platform converges to the same `account_id`. If
  testing through the service, drive `handle_register` / signin handlers with the
  in-memory DB harness; otherwise unit-test a small `attach_profile(wallet,
  platform, uid)` helper you extract.
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_service_profile.py -q` (or the identity
  test) — expect `account_id` still NULL.
- [ ] **Step 3: Implement** — after each successful
  `await asyncio.to_thread(identity_store.link, ...)`, add a best-effort
  `pid = await asyncio.to_thread(identity_store.ensure_profile_for_wallet,
  wallet)` then `await asyncio.to_thread(identity_store.link_identity_to_profile,
  platform, uid, pid)`. Wrap in the same defensive logging the surrounding link
  code uses (a profile-attach failure must NEVER fail a register/sign-in — log
  and continue, exactly like the existing "identity.link failed …" handling).
- [ ] **Step 4: Run to verify they pass** — same pytest command.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/ -k "identity or signin or register or profile" -q`.
- [ ] **Step 6: Commit** — `feat(service): auto-attach identities to a profile on wallet proof (#207)`

---

### Task 3: `GET`/`PATCH /api/profile` + profile-first display name

**Files:**
- Modify: `lfg_service/app.py` (add `handle_profile_get` / `handle_profile_patch`
  near `handle_account` at `~639`; register routes near `~5434`; upgrade
  `_lb_display_name` `~684` and `enrich_minter_identity` `~120`)
- Test: `tests/test_service_profile.py`

**Interfaces:**
- Produces: `GET /api/profile` (`@require_wallet`) →
  `{id, display_name, avatar_url, preferences, identities:[...], wallets:[...]}`
  (synthesized fallback when unprofiled); `PATCH /api/profile` (`@require_wallet`)
  patches the caller's own profile.
- Consumes: `profile_for_wallet`, `update_profile`, `ensure_profile_for_wallet`.

- [ ] **Step 1: Write the failing test(s)** — with the service test harness:
  - `GET /api/profile` returns the caller's profile; an unprofiled caller gets the
    synthesized profile-shaped view (never 404).
  - `PATCH /api/profile {"display_name": "alice"}` updates it; re-`GET` reflects it.
  - After PATCH, `GET /api/leaderboard` renders `alice` for that wallet's row
    (exercises the profile-first `_lb_display_name`).
  - Privacy: a caller cannot read/patch another wallet's profile.
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_service_profile.py -q` — expect 404 /
  missing route.
- [ ] **Step 3: Implement** —
  - `handle_profile_get`: resolve `request["wallet"]` → `profile_for_wallet`; if
    `None`, synthesize `{id: None, display_name: handle_for_wallet(w),
    avatar_url: None, preferences: {}, identities: identities_for_wallet(w),
    wallets: [w]}`.
  - `handle_profile_patch`: `ensure_profile_for_wallet(request["wallet"])` →
    `update_profile(pid, ...)` from the JSON body (whitelist `display_name`,
    `avatar_url`, `preferences`); return the refreshed profile.
  - register both routes in `create_app()` next to `add_get("/api/account", ...)`.
  - `_lb_display_name`: try `profile_for_wallet(wallet)` `display_name` first, then
    fall back to the current `handle_for_wallet` → truncated-wallet chain.
  - `enrich_minter_identity`: prefer a profile `display_name` when present.
- [ ] **Step 4: Run to verify they pass** — same pytest command.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/ -k "profile or account or leaderboard or me" -q`.
- [ ] **Step 6: Commit** — `feat(service): GET/PATCH /api/profile + profile-first display name (#207)`

---

### Task 4: SDK `profile` / `update_profile` client methods

**Files:**
- Modify: `surfaces/_client/client.py`
- Test: extend the client's existing test module (mirror how `account(...)` is
  tested), or `tests/test_service_profile.py` end-to-end via the SDK.

**Interfaces:**
- Produces: `async def profile(self, user_id, *, username="") -> dict` (GET),
  `async def update_profile(self, user_id, **fields) -> dict` (PATCH).
- Consumes: the existing `_user_request` plumbing used by `account(...)`.

- [ ] **Step 1: Write the failing test(s)** — assert the SDK issues `GET
  /api/profile` and `PATCH /api/profile` with the auth/session headers the
  existing `account(...)` method sends, and returns the parsed JSON.
- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/ -k "client and profile" -q`.
- [ ] **Step 3: Implement** — add both methods next to `account(...)`, reusing
  `_user_request` (or the existing request helper) with the correct verb/path.
- [ ] **Step 4: Run to verify they pass** — same pytest command.
- [ ] **Step 5: Wider suite / regression run** —
  `.venv/bin/python -m pytest tests/ -k "client" -q`.
- [ ] **Step 6: Commit** — `feat(sdk): profile / update_profile client methods (#207)`

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q`.
- [ ] Run lint/type gate: `.venv/bin/ruff check . && .venv/bin/ruff format --check .
  && .venv/bin/mypy lfg_service lfg_core` (or invoke the pre-commit pre-push hook
  directly). Fix everything; never `--no-verify`.
- [ ] No `app.js` / client asset changed → no cache-buster bump needed (confirm
  the diff touches no `webapp/client/**`).
- [ ] Push the branch and open a **non-draft** PR against `Team-Hamsa/LFG`:
  - No AI attribution anywhere (no `Co-Authored-By`, no generated-with footer).
  - PR body: link the spec + plan, summarize the four seams, and explicitly note
    the **ordering relationship to #206** (this ships the one-profile-per-wallet
    layer; #206 later widens membership across wallets via
    `link_identity_to_profile`) and that the free-mint `wallet_links` seed named
    in #207 does not yet exist.
  - Wait for **Greptile** + **CodeRabbit**; resolve every actionable finding
    (fix in code AND reply on its thread naming the fixing commit) before merge.
