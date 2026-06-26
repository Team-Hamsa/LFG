# Unified User Accounts: Wallet-Keyed, Multi-Surface Identity Registry — Design

**Date:** 2026-06-26
**Status:** Draft — ready for review
**Issue:** [#90](https://github.com/Team-Hamsa/LFG/issues/90)
**Context:** The shared-services spine introduced an `identities` table keying `(platform, platform_user_id) → wallet` (`lfg_service/identity.py`). #90 inverts and enriches that: make the **wallet** a first-class account that links a user's identities across surfaces (Discord, Telegram, eventually X), stores a **display handle** per identity, and supports an explicit **"link another surface"** flow. This unblocks #85 (real announcement handles), #89 (Mini App auth mapping), and #91 (firehose identity enrichment).

---

## 1. Problem & Current State

### What `identities` does today

`lfg_service/identity.py` is a one-directional lookup: given a surface identity, find the wallet.

```python
# lfg_service/identity.py:17-25
CREATE TABLE IF NOT EXISTS identities (
    platform          TEXT NOT NULL,
    platform_user_id  TEXT NOT NULL,
    platform_username TEXT,
    wallet            TEXT NOT NULL,
    account_id        INTEGER,           -- reserved hook, nullable, unused
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (platform, platform_user_id)
)
```

- `link(platform, platform_user_id, platform_username, wallet)` — upsert keyed by `(platform, platform_user_id)` (`identity.py:33-52`).
- `resolve(platform, platform_user_id) -> wallet | None` — the forward lookup (`identity.py:55-68`).
- `migrate_users_to_identities()` — backfills legacy `Users` rows as `platform='discord'` (`identity.py:71-96`).

The file header already anticipates #90:

> *"The wallet is the canonical account; `account_id` is a reserved hook for future linked multi-surface profiles (nullable, unused now)."* (`identity.py:3-4`)

### How identities get written

Two service paths write identities, both via `identity_store.link(...)`:

1. **`POST /api/register`** (`app.py:291-319`) — `is_valid_classic_address` format check, then (discord only) `register_user(...)` to the legacy `Users` table, then `identity_store.link(...)`.
2. **`GET /api/signin/{uuid}`** on a signed Xaman approval (`app.py:520-558`) — the **proof-of-ownership** path: captures `s["account"]` from the signed payload and links it. This is the path both bots' `/register` now drive (`surfaces/discord_bot/register_view.py`, `surfaces/telegram_bot/register_view.py`).

`_resolve_wallet(platform, uid)` (`app.py:168-173`) does the read: `identities` first, with a discord-only fallback to the legacy `Users` table.

### Limits (what #90 must fix)

1. **No inverse lookup.** Nothing answers "given wallet `rXXX`, which surface identities belong to it?" The PRIMARY KEY is `(platform, platform_user_id)`; `wallet` is an unindexed value column. #85/#91 need wallet → handle(s).
2. **No first-class account.** A user on Discord and Telegram with the same wallet is two unrelated rows with no notion that they're the same person. There is no place to record "these two identities are one account," no canonical display handle, and `account_id` is always NULL.
3. **Handle is captured but stale and unused.** `platform_username` is written at register time (e.g. `str(interaction.user)` on Discord, `user.username` on Telegram) but never refreshed and never read back by announcements — #85 still says "a user".
4. **No explicit linking.** Today the *only* way two identities share a wallet is by **coincidence** — each independently proves the *same* address via its own Xaman sign-in. There is no "I'm already registered on Discord; attach my Telegram to the same account" flow, and (correctly) no auto-linking by colliding user-id.

### The isolation guarantee that must be preserved

The spine deliberately keeps surfaces isolated: `telegram:55` and `discord:55` are **distinct** identities and must never share a wallet by id-collision. This is enforced today by:

- `identities` PRIMARY KEY `(platform, platform_user_id)` — distinct rows (`tests/test_identity.py::test_same_user_id_different_platforms_are_distinct`).
- Sign-in payload ownership keyed by `(platform, user_id)` — a `discord:55` token gets `404` on a `telegram:55` payload (`app.py:524-531`, `tests/test_service_signin_platform.py::test_signin_status_cross_platform_404`).
- The legacy `Users` write gated to `platform == "discord"` so a colliding numeric id from another platform can't overwrite a Discord user's wallet (`app.py:299-305`, `app.py:537-543`).

**#90 must preserve all of this.** Linking two surfaces to one wallet-account must be **explicit and wallet-proof-gated**, never id-collision-based.

---

## 2. Data Model

### Decision: keep the wallet as the account key; add an inverse index + a `display_handle` column. Do NOT add a separate `accounts` table.

**Rationale.** The wallet *already is* the canonical account key (per the file header and the whole spine design). An `accounts(account_id PK, wallet UNIQUE)` table would add a layer of indirection (`identities.account_id → accounts.account_id → wallet`) that buys nothing today: every identity row already carries the wallet directly, and the wallet is the natural, stable, externally-meaningful key. We therefore:

1. **Add `display_handle` to `identities`** — the per-identity name, refreshed each time we see the user.
2. **Add an index on `identities(wallet)`** to make the inverse lookup (wallet → identities) cheap.
3. **Treat the set of `identities` rows sharing a `wallet` as "the account."** A wallet *is* an account; its identities are the rows where `wallet = ?`. No new table, no `account_id` population (the column stays the reserved nullable hook it is today — see Open Question O4 for whether to drop it).

This keeps the migration tiny (one `ADD COLUMN`, one `CREATE INDEX`), keeps the existing `link`/`resolve` API intact, and means existing rows need no rewrite — only a backfill of `display_handle` from the already-present `platform_username`.

### DDL (post-migration shape)

```sql
CREATE TABLE IF NOT EXISTS identities (
    platform          TEXT NOT NULL,
    platform_user_id  TEXT NOT NULL,
    platform_username TEXT,                       -- unchanged (raw handle at register)
    display_handle    TEXT,                       -- NEW: refreshed display name
    wallet            TEXT NOT NULL,
    account_id        INTEGER,                     -- still reserved/nullable (see O4)
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP,                   -- NEW: last handle/link refresh
    PRIMARY KEY (platform, platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_identities_wallet ON identities(wallet);
```

> `platform_username` is retained as the *raw* value captured at registration; `display_handle` is the value announcements render and that we keep fresh. Keeping both avoids a destructive rewrite of existing data and lets us evolve the display rule (e.g. strip discriminators) without losing the original.

### Migration approach (repo convention)

The repo uses **idempotent `CREATE TABLE IF NOT EXISTS` + additive `ALTER TABLE` guarded by an introspection check**, run at startup. `create_app()` already calls `ensure_identities_table()` then `migrate_users_to_identities()` (`app.py:741-742`). We extend `ensure_identities_table()` to be self-migrating, mirroring the introspection pattern already used in `migrate_users_to_identities` (`identity.py:75-76`, which reads `sqlite_master`):

```python
def ensure_identities_table() -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS identities ( ... )""")  # base shape
        cols = {r[1] for r in conn.execute("PRAGMA table_info(identities)")}
        if "display_handle" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN display_handle TEXT")
            # backfill from the value we already have
            conn.execute(
                "UPDATE identities SET display_handle = platform_username "
                "WHERE display_handle IS NULL"
            )
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN updated_at TIMESTAMP")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identities_wallet ON identities(wallet)"
        )
        conn.commit()
    finally:
        conn.close()
```

This is forward-only and safe to run on every boot — exactly like the existing helpers. No down-migration; SQLite `ADD COLUMN` is non-destructive.

### Data-model questions addressed

- **What happens to existing rows?** They keep their PK and wallet; `display_handle` is backfilled from `platform_username`; `updated_at` is left NULL until the next refresh. No row is rewritten or dropped.
- **Is wallet unique/stable as an account key?** A wallet is **stable** (XRPL classic address; a user's registered address doesn't change unless they re-register). It is **NOT unique per row** and intentionally so — *multiple* identities point at one wallet (that's the whole feature). It *is* unique *as an account*: "the account" = the set of rows with that wallet. Two different humans *could* register the same wallet on two surfaces; that is treated as one account (acceptable — see O3).
- **Case-normalization of XRPL addresses?** XRPL classic addresses (`r...`, base58check) are **case-sensitive** — the checksum makes a case-folded address invalid. We therefore **store and compare verbatim, never lower-case** (unlike, say, EVM hex addresses). `is_valid_classic_address` (already used at every write, `app.py:297,535`) rejects malformed/mis-cased input before it reaches the table, so the index and the inverse lookup compare exact strings. **Decision: no case normalization; rely on `is_valid_classic_address` as the gate.** (Documented explicitly so a future contributor doesn't "helpfully" `.lower()` a wallet.)

---

## 3. Inverse Lookup & Display-Handle Storage

### Inverse lookup: wallet → [identities]

New helper in `lfg_service/identity.py`:

```python
def identities_for_wallet(wallet: str) -> list[dict]:
    """All surface identities linked to a wallet-account. [] if none."""
    # SELECT platform, platform_user_id, display_handle, platform_username,
    #        created_at, updated_at FROM identities WHERE wallet = ?
    # ORDER BY created_at
```

Returns a list of `{platform, platform_user_id, display_handle, platform_username, created_at, updated_at}`. Backed by `idx_identities_wallet`. This is the primitive #85/#91 consume: given an event's `wallet`, fetch the identities and pick a handle.

### Display-handle storage & refresh

**Where the handle comes from.** It's already captured at register time from each surface's user object:

- Discord: `str(interaction.user)` (`register_view.py:19`) → passed as `username`.
- Telegram: `user.username or user.full_name or ""` (`register_view.py:21`).

**Storage.** `link(...)` already writes `platform_username`. We extend `link(...)` to also write `display_handle` (defaulting to the same value) and stamp `updated_at`:

```python
def link(platform, platform_user_id, platform_username, wallet, *, display_handle=None):
    # display_handle defaults to platform_username when not supplied.
    # INSERT ... ON CONFLICT(platform, platform_user_id) DO UPDATE SET
    #   platform_username = excluded.platform_username,
    #   display_handle    = excluded.display_handle,
    #   wallet            = excluded.wallet,
    #   updated_at        = CURRENT_TIMESTAMP
```

**Keeping it fresh.** Handles drift (users rename). Two complementary refresh points, no extra polling infrastructure:

1. **On every authenticated touch.** The session token carries `name` (`make_session_token`, `app.py:138-147`); `handle_me` and other authenticated handlers already know the current `username`. Add a lightweight, best-effort `touch_handle(platform, user_id, handle)` on `GET /api/me` so a handle refreshes naturally whenever the user interacts (no-op if unchanged). This requires the bots to pass the *current* username on those calls (they already pass `username=` on most `_user_request`s).
2. **On re-register / re-link.** Any `link(...)` (register or sign-in) overwrites the handle with the value the surface just gave us.

This is "good enough fresh" — a handle is at most as stale as the user's last interaction, which for an announcement-relevant user (someone who just minted) is *now*. We explicitly **do not** add a background job to crawl Discord/Telegram for renames (see O2).

---

## 4. The "Link Another Surface" Flow

### Goal

A user already registered on Discord (so `discord:D → rWALLET` exists) wants their Telegram identity attached to the **same** wallet-account, so that `telegram:T → rWALLET` and both appear under `identities_for_wallet(rWALLET)`.

### Decision: reuse the existing Xaman sign-in as proof. Same-wallet sign-in on the second platform *is* the link.

There is **no new linking primitive needed beyond what already happens.** When the user runs `/register` on the second surface and signs **with the same wallet** in Xaman, the existing `/api/signin` path links `(telegram, T) → rWALLET`. Because both rows now carry `rWALLET`, they are — by definition of our data model — the same account. **Linking is an emergent property of proving the same wallet on each surface.**

What #90 *adds* is making this **legible and intentional** rather than coincidental:

- A **`POST /api/link/start`** + **`GET /api/link/{uuid}`** pair (thin wrappers over the existing sign-in machinery) that, on a signed approval, (a) link the second identity and (b) return the **full account view** (`identities_for_wallet`) so the surface can confirm *"Linked to your account — also on Discord as @alice."*
- A bot `/link` (or `/account`) command that drives it and shows the confirmation.

Functionally `/link` ≈ `/register`, but its success message is account-aware. We can implement it as a flag on the sign-in path rather than a wholly separate endpoint (see O1).

### Sequence (ASCII)

```
User (already discord:D -> rWALLET)            2nd surface = Telegram
─────────────────────────────────────────────────────────────────────────
 user: /link  (on Telegram, as telegram:T)
        │
        ▼
 TG bot ──POST /api/link/start (session=telegram:T)──▶ lfg_service
                                                         │ create Xaman SignIn payload
                                                         │ store signin_payloads[uuid] =
                                                         │   {platform=telegram, user_id=T, ...}
        ◀────────────── {uuid, signin_link} ────────────┘
        │
   show QR  ◀── svc.qr_png(signin_link)
        │
 user scans in Xaman, signs WITH rWALLET  ──────────────▶ XUMM
        │  (poll)
 TG bot ──GET /api/link/{uuid} (session=telegram:T)──▶ lfg_service
                                                         │ XUMM: signed, account=rWALLET
                                                         │ is_valid_classic_address(rWALLET) ✓
                                                         │ identity.link(telegram, T, handle, rWALLET)
                                                         │   -> telegram:T now -> rWALLET
                                                         │ account = identities_for_wallet(rWALLET)
                                                         │   -> [discord:D @alice, telegram:T @alice_tg]
        ◀── {state:"signed", wallet:rWALLET, account:[...]} ┘
        │
   "✅ Linked to your account. Also on: Discord (@alice)."
```

If the user signs with a **different** wallet `rOTHER`, the result is simply `telegram:T → rOTHER` — a normal registration, *not* a link to the Discord account. That's correct: they proved a different wallet, so it's a different account. The confirmation message would show only the Telegram identity under `rOTHER`.

---

## 5. Cross-Platform Safety

The isolation guarantee (`telegram:X` must NOT auto-bind `discord:X`'s wallet) is **preserved by construction**:

1. **Linking is wallet-proof-gated, never id-based.** The only way `telegram:T` joins `discord:D`'s account is by **signing the same wallet in Xaman** on the Telegram surface. Possession of the wallet (proven by signature) is the authorization. A colliding numeric id grants nothing.
2. **Sign-in payload ownership stays `(platform, user_id)`-keyed.** `/api/link/{uuid}` inherits the exact ownership check from `/api/signin` (`app.py:524-531`): a `discord` token cannot read/complete a `telegram` payload. Re-tested for the link path.
3. **Legacy `Users` write stays discord-gated.** Unchanged (`app.py:299-305`, `app.py:537-543`). The link flow on a non-discord surface writes `identities` only.
4. **No "merge accounts by matching id" anywhere.** There is deliberately no code path that says "discord:55 and telegram:55 have the same id, so merge them." The data model has no id-collision join; the only join key is `wallet`, and a wallet only lands on a row via a proven sign-in or an explicit (discord-only, format-checked) register.

The result: two humans cannot collide into one account by id, and one human cannot hijack another's account without controlling their wallet. The threat that linking *introduces* — "could surface A see surface B's identity?" — is bounded to: yes, but only for identities that *share a proven wallet*, which is exactly the account the user assembled themselves.

---

## 6. New / Changed Service Endpoints

### `GET /api/account` — the account view (by the caller's own wallet)

Authenticated (`@require_wallet`). Returns the caller's account: their wallet plus every identity linked to it.

```
GET /api/account
Authorization: Bearer <session token>
→ 200
{
  "wallet": "rWALLET...",
  "identities": [
    {"platform": "discord",  "platform_user_id": "D", "display_handle": "alice",    "created_at": "...", "updated_at": "..."},
    {"platform": "telegram", "platform_user_id": "T", "display_handle": "alice_tg", "created_at": "...", "updated_at": null}
  ]
}
→ 400 {"error": "no wallet registered"}   # caller has no wallet yet
```

The caller only ever sees **their own** account (the one keyed by *their* resolved wallet) — there is no "look up an arbitrary wallet's identities" public endpoint (privacy; see O5). Internal consumers (announcements, firehose) call `identity.identities_for_wallet(...)` directly in-process, not over HTTP.

### `POST /api/link/start` + `GET /api/link/{uuid}` — link a surface

Authenticated (`@require_auth`). Thin wrappers over the sign-in machinery (or a `link=true` flag on `/api/signin` — O1). Request/response mirror sign-in, with the account view added on success:

```
POST /api/link/start
→ 200 {"uuid": "...", "signin_link": "https://xumm.app/sign/..."}

GET /api/link/{uuid}
→ 200 {"state": "pending" | "opened"}
→ 200 {"state": "signed", "wallet": "rWALLET...", "account": {"wallet": "...", "identities": [...]}}
→ 200 {"state": "expired"}
→ 404 {"error": "not found"}   # cross-platform ownership mismatch
```

### Unchanged

`POST /api/register`, `POST /api/signin`, `GET /api/signin/{uuid}`, `GET /api/me` keep their contracts (`/api/me` gains a best-effort handle-refresh side effect, response shape unchanged).

### SDK additions (`surfaces/_client/client.py`)

```python
async def account(self, user_id, *, username="") -> dict          # GET /api/account
async def link_start(self, user_id, *, username="") -> dict       # POST /api/link/start
async def link_status(self, user_id, uuid) -> dict                # GET /api/link/{uuid}
async def wait_for_link(self, user_id, uuid, ...) -> dict         # reuse _poll + SIGNIN_TERMINAL
```

`wait_for_link` reuses the existing `_poll(..., SIGNIN_TERMINAL, ...)` exactly as `wait_for_signin` does (`client.py:293-304`).

---

## 7. What This Unblocks (Minimal Surface Per Consumer)

- **#85 — real announcement handle.** `make_announcement` (`surfaces/telegram_bot/events.py:18`) currently says "a user". With the inverse lookup it does `identity.identities_for_wallet(ev.wallet)` and renders a handle. **Minimal need:** `identities_for_wallet` + `display_handle` (Tasks 1–3). A same-platform event can still prefer the matching-platform handle; cross-platform falls back to any handle or the wallet.
- **#89 — Mini App auth mapping.** Telegram `initData` → session token → `_resolve_wallet`. The account view (`GET /api/account`) lets the Mini App show "you're also linked on Discord" and gives #89 a single account object instead of per-surface lookups. **Minimal need:** `GET /api/account` (Task 4) + the existing session/identity plumbing.
- **#91 — firehose identity enrichment.** Every published `Event` carries `identity` + `wallet`; enrich each with a display handle via `identities_for_wallet` before fan-out to Discord/Telegram/X. **Minimal need:** `identities_for_wallet` (Task 2) — the same primitive as #85, reused.

All three lean on the **same two additions**: the inverse lookup and a fresh `display_handle`. The link flow (Tasks 5–7) is the user-facing feature that makes cross-surface handles *exist* to be shown.

---

## 8. Resolved Decisions

These were the open questions at spec time; all were decided before implementation and the PR (#90) was built to match. Recorded here so the merged spec reflects what shipped.

- **O1 — Link transport: `link=true` flag on `/api/signin` (NOT separate `/api/link/*` endpoints).** `POST /api/signin` accepts an optional `{"link": true}`, recorded on the payload record; `GET /api/signin/{uuid}` adds the `account` view to the signed response only when link-intent was set. Plain sign-in is byte-identical (no `account` key). The SDK exposes readable `link_start` / `link_status` / `wait_for_link` aliases over this same machinery.
- **O2 — Handle freshness: opportunistic only.** `touch_handle(platform, user_id, handle)` is called best-effort on `GET /api/me` (and every `link(...)` overwrites the handle). No background crawler. Revisit only if announcements show stale names.
- **O3 — Two humans, one wallet: collapse into one account.** Wallet possession (proven via Xaman) is the identity; a shared/custodial wallet is one account by design. No extra guard — inherent in wallet-as-key.
- **O4 — `account_id` column: kept, never populated.** No new `account_id` column is added and the existing NULL reserved hook stays as-is. The wallet IS the account key; there is no `accounts` table.
- **O5 — Account-view privacy: in-process inverse lookup only.** `GET /api/account` returns ONLY the caller's own resolved-wallet account. No HTTP endpoint maps an arbitrary wallet → identities; internal consumers (#85/#91 announcements/firehose) call `identity.identities_for_wallet(...)` in-process.
- **O6 — Unlinking: out of scope for #90.** No unlink flow ships in this PR; deferred as a possible follow-up.
