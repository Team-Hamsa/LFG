# User Profiles — first-class profile entity above per-platform identities — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #207

## Problem

Today the account model bottoms out at the wallet. `lfg_service/identity.py` keys
every row `(platform, platform_user_id) → wallet`, and #90 (unified accounts,
`docs/superpowers/specs/2026-06-26-unified-accounts-design.md`) deliberately
decided **not** to add an accounts table: *"the wallet IS the account key; there
is no `accounts` table"* (decision O4). Under that model **the account = the set
of `identities` rows sharing one `wallet`**. `GET /api/account`
(`app.py:handle_account`) literally returns `{wallet, identities:
identities_for_wallet(wallet)}`.

That model has a ceiling the issue calls out: a single human with **more than one
wallet** is more than one account. There is nowhere to record:

- a durable **display name / avatar / preferences** that survives a wallet change
  or spans two wallets (today `display_handle` is per-identity and per-surface,
  refreshed opportunistically in `touch_handle` / `handle_for_wallet`);
- that Discord identity `D`, Telegram identity `T`, **and** wallets `rA` + `rB`
  are all **one person** — needed for per-human accounting (free mints, credits,
  per-human leaderboards) rather than per-`(platform, user_id)` or per-wallet;
- the `identities.account_id INTEGER` column already carved out for exactly this
  (`identity.py:22`, header comment lines 3-4: *"a reserved hook for future
  linked multi-surface profiles (nullable, unused now)"*) — grep confirms it is
  written **nowhere** (`grep -rn account_id` → only the DDL and two test DDLs).

The issue names two seeds — `wallet_links` (from the free-mint work) and
`identities.account_id`. **Finding during triage:** the free-mint spec
(`docs/superpowers/specs/2026-07-13-free-mint-newcomers-design.md`) and the
`wallet_links` table it was to introduce **do not exist in the repo** (`ls` →
missing; `grep -rn wallet_links` → zero hits). So one of the two named seeds is
not yet real; the other (`account_id`) is real but unpopulated. This creates a
concrete ordering dependency (see Open questions).

## Constraints discovered

- **No-custody, wallet-proof-gated linking (from #90 §5).** The *only* way two
  identities may join one account is by **proving the same wallet in Xaman** —
  never by id-collision (`discord:55` must never auto-bind `telegram:55`). Sign-in
  ownership stays `(platform, user_id)`-keyed (`handle_signin_status`,
  `app.py:~4420`). A profile must inherit this: two identities may share a
  profile **only** through a proven-wallet edge (or an explicit, Xaman-gated link
  action), never through a matching platform id. Profiles introduce **no new
  trust primitive** — they are a durable *view* over already-proven wallet edges.
- **XRPL addresses are case-sensitive.** `identities_for_wallet` warns callers to
  never `.lower()` a wallet (base58check checksum). Any profile↔wallet table must
  store/compare wallets verbatim and gate writes on `is_valid_classic_address`.
- **Self-migrating, forward-only DB convention.** `ensure_identities_table()`
  does `CREATE TABLE IF NOT EXISTS` + introspected `ALTER TABLE ADD COLUMN`
  guarded by `PRAGMA table_info`, run every boot from `create_app()`
  (`app.py:5419-5420`). New tables/columns must follow this exact pattern — no
  Alembic, no down-migration. DB path is `lfg_core.user_db.DATABASE` (the app DB,
  network-aware via `db_path`).
- **The account DB is NOT the on-ledger DB.** Profiles are pure application
  metadata; they hold **no** on-chain state and mint/burn/offer nothing. No
  transaction is built by this feature, so **SourceTag=2606160021 and provenance
  memos are not emitted here** — but any *future* tx that reads a profile still
  resolves through the identity's `wallet` and keeps SourceTag + memos unchanged.
- **Privacy seam (#90 O5).** `GET /api/account` returns ONLY the caller's own
  account; there is no public "arbitrary wallet → identities" endpoint. Internal
  consumers (announcements, firehose `enrich_minter_identity`, `app.py:120`) call
  `identity_store.identities_for_wallet(...)` in-process. The profile endpoint
  must keep this: a caller sees only their own profile.
- **Backward compatibility.** `GET /api/account`, `identities_for_wallet`,
  `handle_for_wallet`, `resolve`, `link` all have live callers across three
  surfaces (`app.py`, both bots, web). Their contracts must not break; the
  profile layer is **additive** and every existing wallet-scoped read must keep
  working when a profile is absent (profile is optional metadata, not a gate).

## Design

### Data model — one new table + populate the reserved hook

Two additions in `lfg_service/identity.py`, both self-migrating in
`ensure_identities_table()` (extended in place, same pattern as the
`display_handle` / `updated_at` / `user_token` blocks already there):

```sql
-- NEW: the first-class profile entity (one row per human)
CREATE TABLE IF NOT EXISTS profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name  TEXT,                       -- profile-level, overrides per-identity handles
    avatar_url    TEXT,                       -- optional; CDN/remote URL, no upload pipeline in v1
    preferences   TEXT,                       -- JSON blob, opaque to the DB (announce opt-out, etc.)
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP
);

-- POPULATE the existing reserved hook: identities.account_id -> profiles.id
-- (column already exists; no ADD COLUMN needed, just start writing it)
CREATE INDEX IF NOT EXISTS idx_identities_account ON identities(account_id);
```

**What a profile owns.** The profile row holds the durable, cross-surface
metadata (`display_name`, `avatar_url`, `preferences`). Its **members** are
derived, not duplicated:

- **Linked identities** = `SELECT ... FROM identities WHERE account_id = ?`.
- **Wallets** = `SELECT DISTINCT wallet FROM identities WHERE account_id = ?`
  (a wallet belongs to a profile transitively, through the identities that proved
  it). A profile therefore owns *N* identities and *M* distinct wallets with **no
  extra join table** — the wallet already lives on every identity row, exactly as
  #90 argued for keeping the wallet on `identities` rather than in an `accounts`
  table. We reuse that: `account_id` is the new grouping key; `wallet` stays where
  it is.

> **Why `account_id` on `identities`, not a `profile_wallets` table.** The issue
> floats "a profiles/accounts table keyed above identities." We add exactly that
> table for the *metadata*, but keep membership as a **foreign key on identities**
> rather than a separate wallet-join table, because (a) `account_id` is already
> reserved for this, (b) every wallet edge is already on an identity row (an
> orphan wallet with no identity cannot exist — a wallet only lands on a row via a
> proven sign-in or a discord-gated register), so a `profile_wallets` table would
> be derivable-and-droppable duplicate state. If a future need arises for a
> profile to own a wallet with **no** identity (e.g. a treasury address), promote
> to a `profile_wallets` table then — recorded as Open question O4.

### New helpers in `lfg_service/identity.py`

```python
def ensure_profile_for_wallet(wallet: str) -> int:
    """Return the profile id every identity on `wallet` belongs to, creating a
    profile and stamping account_id on those identities if none exists yet.
    Idempotent. The first identity's display_handle seeds display_name."""

def profile_for_identity(platform: str, platform_user_id: str) -> dict | None:
    """The profile row + its members for one identity, or None if unprofiled.
    Returns {id, display_name, avatar_url, preferences(dict),
             identities:[...], wallets:[...]}."""

def profile_for_wallet(wallet: str) -> dict | None:
    """Same shape, resolved from any identity on `wallet`."""

def link_identity_to_profile(platform, platform_user_id, profile_id: int) -> bool:
    """Set identities.account_id = profile_id for one identity (the merge
    primitive #206's dedup graph drives). Verbatim wallet, best-effort, logged."""

def update_profile(profile_id: int, *, display_name=None, avatar_url=None,
                   preferences: dict | None = None) -> bool:
    """Patch profile metadata; stamps updated_at. Partial (only-provided fields)."""
```

`preferences` is (de)serialized as JSON at the helper boundary so callers see a
dict; the column stays opaque TEXT. All helpers follow the file's house style:
own `sqlite3.connect(DATABASE)`, try/except-log-return-falsy, `finally: close()`,
never `.lower()` a wallet.

### How membership is assigned (composition with #206)

A profile's membership is **seeded from the shared-wallet edge**, which is
precisely what #206 (identity dedup) computes:

1. **Auto-attach on wallet proof.** Every `identity_store.link(...)` site
   (register `app.py:~3293`, sign-in `app.py:~4426`, web `app.py:~4537`) gains a
   best-effort follow-up `ensure_profile_for_wallet(wallet)` +
   `link_identity_to_profile(...)`. When a *second* identity proves a wallet that
   already has a profile, it joins that profile — the cross-surface dedup #206
   describes, realized as `account_id` convergence. When it proves a *new*
   wallet, a fresh profile is created. (A single human with wallet `rA` on Discord
   and `rB` on Telegram gets **two** profiles until an explicit merge — see O1.)
2. **#206 is the merge engine; #207 is the durable target.** #206 builds the
   "same human" graph (its `wallet_links` append-log + connected components).
   #207 gives that graph a **home**: resolving a #206 bucket = "these identities
   share a profile," implemented by pointing their `account_id` at one
   `profiles.id`. Concretely, #206's reconciliation pass calls
   `link_identity_to_profile(...)` to collapse a connected component onto the
   oldest member's profile. **Delineation:** #206 owns *which identities are the
   same human* (the graph + collapse policy); #207 owns *the entity they collapse
   into* (the row, its metadata, and the read API). #207 can ship a **degenerate
   one-wallet-per-profile** version with no #206 (each proven wallet = one
   profile); #206 later upgrades membership to span wallets. Neither hard-requires
   the other to compile, but per-human accounting across *multiple wallets* needs
   both.
3. **Explicit link stays Xaman-gated.** The #90 "link another surface" flow
   (`link=true` on `/api/signin`) already merges identities by proving the same
   wallet; that path now also converges `account_id`. A future "these are two
   different wallets, both mine" merge (O1) MUST require proving **both** wallets.

### How surfaces read it

- **New endpoint `GET /api/profile`** (`@require_wallet`, alongside
  `handle_account` at `app.py:639`): returns the caller's profile
  (`profile_for_wallet(request["wallet"])`) or, if unprofiled, a synthesized
  profile-shaped view over `identities_for_wallet` so callers never 404 during
  rollout. Caller sees only their own profile (privacy seam preserved).
- **New endpoint `PATCH /api/profile`** (`@require_wallet`): updates
  `display_name` / `avatar_url` / `preferences` for the caller's own profile
  (resolve profile from `request["wallet"]`, then `update_profile`). Rejects
  edits to a profile the caller's wallet is not a member of.
- **`GET /api/account` unchanged** (backward compat) but MAY gain an optional
  `profile_id` field in its JSON.
- **Display-name resolution upgrade.** `_lb_display_name` (`app.py:684`) and
  `enrich_minter_identity` (`app.py:120`) currently call
  `handle_for_wallet(wallet)`. Add a profile-first lookup:
  `profile_for_wallet(wallet).display_name or handle_for_wallet(wallet) or
  truncated-wallet`. This makes leaderboards/announcements honor a user's chosen
  profile name — the concrete per-human payoff.
- **SDK** (`surfaces/_client/client.py`): add `async def profile(self, user_id,
  *, username="")` (GET) and `async def update_profile(self, user_id, **fields)`
  (PATCH), mirroring the existing `account(...)` client method.

### Data-model changes summary

| Change | File | Kind |
|---|---|---|
| `profiles` table | `identity.py` `ensure_identities_table()` | new (CREATE IF NOT EXISTS) |
| `idx_identities_account` | same | new index |
| populate `identities.account_id` | link sites in `app.py` | write reserved column |
| profile helpers | `identity.py` | new functions |
| `GET`/`PATCH /api/profile` | `app.py` | new endpoints + routes |
| profile-first display name | `app.py` `_lb_display_name`, `enrich_minter_identity` | additive |

No on-ledger transaction is built, so no SourceTag/memo surface changes.

## Out of scope

- **Avatar upload pipeline.** v1 stores an `avatar_url` string only; no image
  ingest/CDN-upload flow (a future issue can add one, reusing the BunnyCDN path).
- **Cross-wallet merge UX** (proving two *different* wallets are one human) —
  belongs with #206's explicit-merge policy; v1 auto-attaches only by shared
  wallet.
- **Unlinking / splitting a profile** (deferred, mirrors #90 O6).
- **The #206 dedup graph itself** (`wallet_links`, connected-component
  reconciliation) — #206 owns it; #207 only consumes the collapse via
  `link_identity_to_profile`.
- **Migrating per-identity accounting** (free-mint claim gating, credits) from
  per-identity to per-profile — that is the *consumer* work #207 unblocks, filed
  under free-mint / #206, not this spec.

## Open questions / decisions for maintainer

- **O1 — Ordering vs #206 and free-mint.** The issue names `wallet_links` (a
  free-mint deliverable) as a seed, but **neither the free-mint spec nor
  `wallet_links` exists in the repo today.** Options: (a) ship #207's degenerate
  one-profile-per-wallet layer now (no #206 needed) and let #206 later widen
  membership; (b) block #207 until #206 lands so the first membership write is
  already multi-wallet-aware. Recommendation: **(a)** — the `account_id`
  convergence primitive is identical either way, and shipping the table + reads
  now lets leaderboards/announcements honor profile names immediately. Confirm.
- **O2 — Auto-create policy.** Should a profile be created eagerly for **every**
  proven wallet (proposed), or lazily only when a user sets a display name /
  second identity links? Eager keeps `account_id` densely populated (simpler
  reads) at the cost of many single-member profiles.
- **O3 — Two humans, one custodial wallet (inherits #90 O3).** #90 decided a
  shared wallet is *one account*. That means they'd share *one profile* (and one
  display name). Acceptable, or does a profile need a stronger "distinct human"
  signal? Recommend inheriting #90 O3 (one wallet-edge = one profile).
- **O4 — Membership as FK vs join table.** Proposed: `identities.account_id` FK
  (no wallet-join table) since no wallet exists without an identity. Confirm we
  never need a profile to own a wallet with zero identities (treasury/vanity); if
  we might, add `profile_wallets` up front instead.
- **O5 — Profile-name precedence.** When a profile has a `display_name`, does it
  **always** win over per-surface `display_handle` in announcements, or only on
  cross-platform events? Proposed: profile name wins whenever set.
- **O6 — `preferences` schema.** Left an opaque JSON blob in v1. First real key is
  likely `announce_opt_out`. Decide whether to formalize a typed schema now or
  keep free-form until a second key appears.

## Testing

**Unit (`tests/test_identity.py` — extend; it already builds an in-memory
`identities` DDL incl. `account_id`):**
- `ensure_identities_table` creates `profiles` + `idx_identities_account`
  idempotently (run twice, no error, one table).
- `ensure_profile_for_wallet` creates one profile, stamps `account_id` on every
  identity of that wallet, and is idempotent (second call returns same id, no
  duplicate profile).
- A second identity proving the **same** wallet joins the **same** profile
  (`account_id` converges); a second identity proving a **different** wallet gets
  a **different** profile.
- Case-sensitivity: a mixed-case wallet is treated verbatim (never `.lower()`d) —
  mirrors the existing `identities_for_wallet` guarantee.
- `profile_for_identity` / `profile_for_wallet` return the full member view;
  `None` for an unprofiled identity.
- `update_profile` patches only provided fields, stamps `updated_at`, round-trips
  `preferences` as a dict.
- **Isolation invariant preserved:** `discord:55` and `telegram:55` with
  *different* wallets land in *different* profiles (extend
  `test_same_user_id_different_platforms_are_distinct`).

**Integration (`tests/` service-level, e.g. a `test_service_profile.py`):**
- `GET /api/profile` returns the caller's profile; a caller with no profile gets
  the synthesized fallback (never 404).
- `PATCH /api/profile` updates the caller's `display_name`; a subsequent
  `GET /api/leaderboard` renders that name via the profile-first `_lb_display_name`.
- Privacy: `GET /api/profile` never exposes another wallet's profile.

**Manual smoke:**
- Register on Discord (wallet `rA`) → `GET /api/profile` shows one-member
  profile; set a display name via PATCH; confirm it appears in a leaderboard row
  and a mint announcement.
- Link the same wallet on the web surface (`platform="web"`) → the web identity
  joins the same profile (`account_id` converges), profile now lists two
  identities under one display name.

Every test file importing `lfg_core` at module top must carry the env-guard
preamble (`os.environ.setdefault("BUNNY_PULL_ZONE", ...)`,
`setdefault("LAYER_SOURCE", "local")`) per the repo's test convention.
