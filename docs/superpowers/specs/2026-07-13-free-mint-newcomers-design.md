# Free Mint for New Users — Design

**Date:** 2026-07-13
**Status:** Approved (design)
**Scope:** Approach 1 of the mint-giveaway initiative — one free mint per
identity for newcomers who don't already own an LFG NFT. Mainnet.

## Problem

Minting is gated by payment today (`lfg_core/mint_flow.py::prepare_payment` —
LFGO holders burn LFGO, everyone else pays `MINT_PRICE_XRP`). We want to give
away a free mint to onboard newcomers, without opening a hole that lets one
person claim it repeatedly.

Wallet ownership is already cryptographically proven when a user connects/signs
in to link a wallet, so the free path does **not** need its own payment or
SignIn step for authentication.

## Decisions

- **Eligibility (A1): never-claimed AND owns-none.** An identity is eligible
  only if it has never claimed a free mint *and* none of its linked wallets
  currently holds a live LFG character. Targets genuine newcomers; the claim
  record — not the ownership check — is what prevents mint-transfer-repeat.
- **Cross-platform (B1): per-platform.** The free mint is keyed on
  `(platform, platform_user_id)`, matching the existing `identities` PK. A
  Discord identity and a Telegram identity each get one. Accepted low-stakes
  gap for a giveaway; tightening later is cheap because wallet history is
  retained (see follow-ups).
- **Network:** mainnet only. Schema is `network`-aware so a future
  testnet-staging deployment works unchanged.
- **Mechanism:** skip the XUMM payment payload entirely for the free path
  (auth already done at connect). Record the claim atomically at mint success.
- **Global cap:** at most `config.FREE_MINT_CAP` free mints per network
  (default **10** to start — tunable live via the `FREE_MINT_CAP` env var; `0`
  disables the giveaway). The cap
  counts active claims (`reserved` + `claimed`); a released reservation frees a
  slot. Enforced **atomically** inside `reserve_claim` (count + insert under a
  single `BEGIN IMMEDIATE` write lock) so a stampede of concurrent reservers at
  the boundary can never overshoot; `is_eligible` also checks it so a
  capped-out user sees the paid path up front.

## Architecture

Everything lives in the shared spine (`lfg_core` + `lfg_service`), so all three
surfaces (Discord bot, Discord Activity, Telegram) inherit the free path with
no per-surface tx logic. Surfaces change only their pay-screen copy.

### 1. Data model (same SQLite as `identities` / `Users`)

**`wallet_links`** — append-only wallet history. Today `identity.link()`
overwrites `identities.wallet` on re-link, losing prior wallets; that makes a
wallet switch look like a brand-new user. We keep `identities.wallet` as the
*active* pointer (no consumer changes) and additionally append here.

```sql
CREATE TABLE wallet_links (
    platform          TEXT NOT NULL,
    platform_user_id  TEXT NOT NULL,
    wallet            TEXT NOT NULL,
    linked_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (platform, platform_user_id, wallet)
);
CREATE INDEX idx_wallet_links_identity
    ON wallet_links(platform, platform_user_id);
```

`identity.link()` gains a second write: `INSERT OR IGNORE` into `wallet_links`
alongside the existing `identities` upsert, in the same transaction. Re-linking
an already-seen wallet is a no-op (PK conflict ignored); `linked_at` of the
first link is preserved.

**`free_mint_claims`** — one redemption per identity per network.

```sql
CREATE TABLE free_mint_claims (
    platform          TEXT NOT NULL,
    platform_user_id  TEXT NOT NULL,
    network           TEXT NOT NULL,
    wallet            TEXT NOT NULL,      -- wallet the free NFT was minted to
    nft_number        INTEGER,            -- filled on mint success
    status            TEXT NOT NULL,      -- 'reserved' | 'claimed' | 'released'
    claimed_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (platform, platform_user_id, network)
);
```

Both tables are created by a self-migrating `ensure_*` function invoked at boot
next to `ensure_identities_table()`, following the existing forward-only ADD
COLUMN / CREATE TABLE IF NOT EXISTS pattern.

Wallet-history backfill is unnecessary: existing users re-link (or their next
sign-in touches `link()`) and get a `wallet_links` row then; a user with no row
yet is simply treated as eligible on their known active wallet, which is
correct.

### 2. Eligibility helper (`lfg_core`)

New module `lfg_core/free_mint.py`:

```
is_eligible(platform, platform_user_id, network) -> bool
```

1. If a `free_mint_claims` row exists with status in `{reserved, claimed}` for
   this identity+network → **not eligible** (already claimed or a claim is
   in flight).
2. Collect the identity's wallets: `wallet_links` for the identity, unioned with
   the current `identities.wallet` (covers users predating `wallet_links`).
3. If any of those wallets owns a live character
   (`onchain_nfts` where `owner IN (...)` and `is_burned=0`, network-scoped via
   `db_path`) → **not eligible**.
4. Otherwise → **eligible**.

The ownership query reads the local listener-fresh index — no on-chain RPC on
the hot path.

### 3. Claim lifecycle (race-safe)

Reserve-then-confirm, so concurrent sessions from one identity can't both pass
and a failed mint doesn't burn the freebie:

- **Reserve** (`reserve_claim`): `INSERT` a row with `status='reserved'`. The
  PK `(platform, platform_user_id, network)` makes this atomic — a second
  concurrent reserve fails the insert and that session falls back to the paid
  path. Called at the start of the free branch in `prepare_payment`, guarded by
  a re-check of `is_eligible`.
- **Confirm** (`confirm_claim`): on mint success, set `status='claimed'`,
  `nft_number`, `wallet`.
- **Release** (`release_claim`): on mint failure/cancel/timeout, set
  `status='released'` (or delete the reserved row) so the user can retry.
  Released rows do not block a future `is_eligible`.

### 4. Mint flow (`lfg_core/mint_flow.py`)

`MintSession` learns whether this mint is free:

- `prepare_payment()`: call `free_mint.is_eligible(...)`. If eligible and the
  reserve succeeds, set `self.free = True`, leave `pay_with/pay_amount`/payment
  link unset, and let the flow advance straight to `GENERATING` without building
  a XUMM payment payload. If not eligible (or reserve lost the race), fall
  through to the existing LFGO/XRP payment logic unchanged.
- The background pipeline is otherwise identical. On reaching the success
  terminal state, call `confirm_claim`; on any failure/cancel path, call
  `release_claim`.
- `to_dict()` / status payload exposes `free: bool` so surfaces render the
  right screen.

The mint tx itself is unchanged: same `NFTokenMint`, same `SourceTag`, same
provenance memo — with `campaign="free-mint"` added to the memo so on-chain
analytics can distinguish giveaway mints from paid ones.

### 5. Service + surfaces

- `lfg_service`: the mint-start endpoint already carries the caller's
  identity `(platform, platform_user_id)` and resolved wallet; it passes
  `network = config.XRPL_NETWORK` into the session. No new endpoint — the
  free/paid decision is internal to the session. The status poll surfaces
  `free`.
- Surfaces: when `free` is true, show a "Free mint 🎉" confirmation instead of
  the pay screen; otherwise unchanged. No new tx code on any surface.

### 6. Admin / ops

`scripts/free_mint_admin.py` (loopback CLI, like other `scripts/*.py`):

- `list` — all claims for a network (status, wallet, nft_number, when).
- `revoke <platform> <user_id>` — release a claim so the identity can claim
  again (support / correcting a bad mint).
- `grant <platform> <user_id> <wallet>` — pre-authorize a claim, bypassing the
  eligibility scan (e.g. a promo recipient who already owns an NFT).

This CLI is intentionally the seed of the Approach-2 credit tooling.

## Error handling

- Reserve loses the PK race → treated as "not eligible now" → paid path. No
  error surfaced; the user just sees the normal pay screen.
- Ownership index unavailable (missing `onchain_<net>.db`) → fail **closed**:
  treat as *not* eligible (charge normally) rather than hand out a free mint we
  can't justify. Logged.
- Mint fails after reserve → `release_claim`; the reserved row must never strand
  eligibility. The release is called from the same failure paths that already
  set `FAILED`/`CANCELLED`/`PAYMENT_TIMEOUT`.

## Testing

- `wallet_links`: re-link appends without clobbering; re-linking a seen wallet
  is a no-op; active pointer in `identities` still updates.
- `is_eligible`: never-claimed + owns-none → True; owns-one (any linked wallet)
  → False; reserved/claimed row → False; released row → True.
- Multi-wallet: ownership found under a *historical* wallet_links wallet → not
  eligible.
- Claim race: two concurrent reserves → exactly one wins; loser takes paid path.
- Failed mint → `release_claim` → identity eligible again.
- Fail-closed: missing on-chain index → not eligible.
- `mint_flow`: eligible session skips payment payload and reaches offer; claim
  confirmed with `nft_number` on success.

## Out of scope / follow-ups (filed as issues)

- **Cross-platform identity linking:** link identities that share a wallet into
  one bucket (would upgrade B1→B2). Foundation (`wallet_links`) is laid here.
- **User profiles:** a first-class profile entity above per-platform identities.
- **Testnet-staging vs mainnet-prod split:** separate deployments so testnet
  testing can't touch prod, and vice versa. Currently only the DB is
  network-split; the app runs one configured `XRPL_NETWORK`.
- **Approach 2 — general credit system:** grantable/redeemable credits beyond
  the single newcomer freebie. `free_mint_admin.py` + `free_mint_claims` are
  the seed.
