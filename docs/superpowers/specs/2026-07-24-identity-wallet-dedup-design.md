# Identity wallet-dedup (shared-wallet "same human" bucket) — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #206

## Problem

Today an identity is keyed `(platform, platform_user_id)` in the `identities`
table (`lfg_service/identity.py`). The same human on Discord and Telegram is two
independent rows with no notion that they are one person. The only thing linking
them is a shared `wallet` value — which happens when the user proves the *same*
XRPL address via Xaman on each surface (`identity.link(...)`, driven by
`POST /api/register` / `GET /api/signin/{uuid}` in `lfg_service/app.py`).

The unified-accounts work (#90, spec `2026-06-26-unified-accounts-design.md`,
already shipped) established **wallet-as-account** and added the inverse lookup
`identities_for_wallet(wallet)` + `handle_for_wallet(wallet)` +
`idx_identities_wallet` + `GET /api/account`. That gives us the *data* to group
identities by wallet, but there is still **no first-class "same human" primitive**
that code can call to answer:

- "Are these two identities the same human?" (`same_human(a, b)`)
- "What is this identity's canonical account bucket key?" (`account_bucket(...)`)
- "Who are all the identities in my bucket?" (already: `identities_for_wallet`, but
  keyed on a wallet the caller must resolve first)

**Premise correction (important for triage).** The issue body says the foundation
"already exists: the new append-only `wallet_links` table added by the free-mint
work," referencing `docs/superpowers/specs/2026-07-13-free-mint-newcomers-design.md`.
Neither the spec file, a `wallet_links` table, nor any free-mint code path exists
in the repo (`grep -rn wallet_links` and `find docs -iname '*free*'` both empty;
the only `free` hits are unrelated comments in `lfg_core/xrpl_ops.py`). That work
was never merged. So:

1. The dedup **foundation that does exist** is `identities.wallet` + the #90
   inverse lookup, not `wallet_links`.
2. The issue's secondary ask — "migrate free-mint claim gating from per-identity
   to per-bucket" — is **moot until a free-mint feature ships**, and is deferred
   here (see Open questions; it overlaps #207).

What remains genuinely actionable and #206-specific: formalize a thin
**account-bucket lookup layer** over the existing wallet-keyed model, expose a
"same human" predicate, add a **backfill/audit** tool that reports which
identities collapse into shared-wallet buckets, and document the resolve seam so
any *future* per-human gate has one primitive to call.

## Constraints discovered (real invariants that shape the design)

- **Wallet is the account key; no `accounts` table** (unified-accounts O3/O4). The
  set of `identities` rows sharing a `wallet` *is* the account. `account_id`
  stays a NULL reserved hook. This design must not introduce a competing account
  entity (that is #207's job — first-class profiles).
- **Cross-platform isolation is by construction** (unified-accounts §5). Two
  identities join one bucket **only** by both proving the same wallet in Xaman —
  never by id-collision. `discord:55` and `telegram:55` are distinct
  (`tests/test_identity.py::test_same_user_id_different_platforms_are_distinct`).
  The bucket layer must derive membership **only** from the shared `wallet`, and
  must not add any id-based join.
- **XRPL classic addresses are case-sensitive** (base58check checksum). Wallets
  are matched **verbatim, never lower-cased** (`is_valid_classic_address` is the
  gate at every write). The bucket key is the wallet string as stored.
- **A bucket can legitimately be one human across many identities OR two humans
  sharing/custodial one wallet** (O3: collapse into one account by design). The
  "same human" predicate is therefore precisely "same proven wallet" — no
  stronger claim.
- **Leaky-by-design** (issue): only catches users who link the *same* wallet on
  both platforms. Different-wallet-per-surface users stay separate. The design
  states this explicitly; it is not a bug to fix here.
- **Already wallet-keyed downstream.** `lfg_core/leaderboard.py` boards
  (`users_nfts`, `users_swaps`, `users_builds`, …) key on **wallet**, and
  `history_store` events reference wallets — so stats/leaderboards/history are
  *already* deduped per wallet. This bucket layer changes **no leaderboard
  math**; it serves per-*identity*-gated flows (future free mint / credits) and
  account-aware UX, not the already-wallet-keyed boards.
- **No on-ledger transaction** is built by this feature — it is a pure read/DB
  layer. SourceTag `2606160021` + provenance memos are therefore N/A here
  (nothing is signed or submitted).
- **Network-aware DB.** `identities` lives in the single app DB (`user_db.DATABASE`),
  not the per-network `onchain_<net>.db`. Identity/bucket data is network-agnostic
  (a wallet is a wallet on either chain). No `ECONOMY_NETWORK` seam applies.

## Design

Three seams: (A) a bucket lookup layer in `lfg_service/identity.py`, (B) a service
endpoint surfacing the caller's bucket, (C) a read-only audit/backfill script.

### A. Bucket lookup layer — `lfg_service/identity.py`

All functions are thin, side-effect-free reads over the existing table + index.
No schema change (the #90 `idx_identities_wallet` already backs them).

```python
def account_bucket(platform: str, platform_user_id: str) -> str | None:
    """Canonical bucket key for an identity = its resolved wallet, or None if
    the identity has no wallet on file. The bucket key IS the wallet (there is
    no separate account id). Thin wrapper over resolve()."""
    return resolve(platform, platform_user_id)

def bucket_members(platform: str, platform_user_id: str) -> list[dict[str, object]]:
    """Every identity in this identity's account bucket (the same-human set),
    ordered by created_at. [] if the identity has no wallet. Equivalent to
    identities_for_wallet(resolve(platform, uid)); returns the identity's own
    row too (a lone identity is a bucket of one)."""
    wallet = resolve(platform, platform_user_id)
    return identities_for_wallet(wallet) if wallet else []

def same_human(
    platform_a: str, uid_a: str, platform_b: str, uid_b: str
) -> bool:
    """True iff both identities resolve to the SAME proven wallet. False if
    either is unregistered (None wallet never matches None — an unregistered
    identity is nobody's bucket-mate). Wallet compared verbatim (case-sensitive)."""
    wa = resolve(platform_a, uid_a)
    wb = resolve(platform_b, uid_b)
    return wa is not None and wa == wb
```

Design notes:
- **`same_human` fail-closed on None.** Two unregistered identities are *not*
  the same human (both `None` → `False`), so a per-human gate can never treat
  "nobody" as "everybody." This is the single most important invariant to test.
- **No caching.** These are single-row / single-index reads on a small local
  SQLite table; the existing `resolve`/`identities_for_wallet` are already used
  on the hot path uncached. Consistent with the file's style.
- **Reuses, does not duplicate.** `bucket_members` is defined in terms of the
  existing `resolve` + `identities_for_wallet` so there is one query for "who
  shares this wallet," not two divergent ones.

### B. Service endpoint — extend the account view

`GET /api/account` (`lfg_service/app.py::handle_account`, `@require_wallet`)
already returns `{"wallet", "identities": [...]}` — which is exactly the caller's
bucket. **No new endpoint is required for the read path;** the bucket *is* the
account view. We add one derived, cheap field so surfaces can render "you're one
account across N surfaces" without client-side logic:

```json
{
  "wallet": "rWALLET...",
  "identities": [ {"platform":"discord",...}, {"platform":"telegram",...} ],
  "bucket_size": 2,
  "platforms": ["discord", "telegram"]
}
```

`bucket_size = len(identities)`, `platforms = sorted({i["platform"] ...})`.
Backward compatible (additive keys). The privacy posture from O5 is preserved:
a caller only ever sees **their own** resolved-wallet bucket; there is no
arbitrary wallet → identities HTTP lookup. Internal consumers keep calling
`identity_store.bucket_members(...)` / `identities_for_wallet(...)` in-process.

### C. Backfill / audit tool — `scripts/audit_identity_buckets.py`

A read-only ops script (like every other `scripts/*.py` audit) that reports the
current bucket structure so a maintainer can see the dedup graph and confirm a
future per-human gate would behave. It writes nothing to the DB.

```
.venv/bin/python scripts/audit_identity_buckets.py [--json]
```

Output: total identities, total buckets (distinct wallets), and every
**multi-identity bucket** (wallet → its identity rows, incl. cross-platform ones)
— i.e. the humans #206 can actually dedup. `--json` emits machine-readable rows
for reports. Exit 0 always (informational; it is not a gate). This is the
"migration/backfill" the issue asks for: there is **no data migration needed**
(buckets are derived live from `identities.wallet`), so "backfill" here means
*surfacing* the already-derivable graph, not rewriting rows.

### What this deliberately does NOT change

- No `wallet_links` table (doesn't exist; not needed — `identities.wallet` is the
  edge set).
- No leaderboard/history math (already wallet-keyed).
- No first-class profile entity, avatar, prefs, or multi-wallet account (that is
  #207).
- No free-mint gating change (free-mint isn't in the repo).

## Out of scope

- **Free-mint per-bucket claim gating** — deferred until a free-mint feature
  exists; when it does, it calls `same_human` / `account_bucket` at its gate.
- **First-class profiles / multi-wallet accounts / display prefs** — #207.
- **Unlinking** an identity from a bucket (O6 in #90, still deferred).
- **Auto-linking / merging by id-collision** — explicitly forbidden by the
  isolation invariant.
- **Any on-ledger transaction.**

## Open questions / decisions for maintainer

1. **Fold into #207?** #206 (shared-wallet dedup graph) and #207 (first-class
   profiles) heavily overlap; #207's body calls this graph "one input to profile
   membership." A live brainstorm would ask: ship this thin bucket layer now as
   the seam #207 later builds on, or defer entirely into #207? Recommendation:
   ship the thin layer (A + C) now — it is small, self-contained, and unblocks a
   per-human gate without committing to the larger profile model.
2. **Is the bucket layer worth building before its first consumer exists?** With
   free-mint unshipped and leaderboards already wallet-keyed, `same_human` /
   `account_bucket` have **no in-tree caller today** except the audit script and
   the `/api/account` enrichment. Maintainer decides: land the primitive + audit
   as the documented seam, or close #206 as "foundation already covered by #90,
   revisit when a per-human gate is actually needed."
3. **Custodial/shared-wallet collision** (O3): two different humans on one wallet
   collapse to one bucket. Accepted for #90; confirm it's still acceptable for a
   *gating* use (e.g. two people sharing a wallet get one free mint between them).
4. **Should `/api/account` gain `bucket_size`/`platforms`, or should surfaces
   derive it?** Additive and cheap; included here for zero client logic, but it's
   a judgment call.

## Testing

**Unit (`tests/test_identity.py`, no lfg_core import → no env-guard preamble needed):**
- `account_bucket` returns the resolved wallet; `None` for an unregistered
  identity.
- `bucket_members` returns all rows sharing the wallet (incl. cross-platform),
  ordered by `created_at`; `[]` for unregistered; a lone identity → bucket of one.
- `same_human` True for two identities on the same wallet across platforms;
  False when wallets differ; **False when either is unregistered** (both-None
  must not match).
- Isolation regression: `discord:55` linked to `rA` and `telegram:55` linked to
  `rB` are **not** `same_human` (id-collision never buckets).

**Integration (`tests/test_service_*`, aiohttp test client):**
- `GET /api/account` includes `bucket_size` and sorted `platforms`; a caller
  with one identity gets `bucket_size == 1`; two same-wallet identities → 2.
- Privacy: the endpoint never returns identities outside the caller's own
  resolved-wallet bucket.

**Manual smoke:**
- Register the same wallet on Discord and Telegram (two `/register` flows,
  same Xaman address); run `scripts/audit_identity_buckets.py` and confirm the
  two identities appear in one multi-identity bucket; hit `GET /api/account` from
  either surface and confirm `bucket_size == 2`.
