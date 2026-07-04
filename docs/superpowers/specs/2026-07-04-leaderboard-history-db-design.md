# Leaderboard + Ledger History Database — Design

**Date:** 2026-07-04
**Status:** Approved (brainstorm session)
**Related issues:** #48 (BRIX daily claim — consumer of this data), #42 (Web UI)

## Goal

Two deliverables, shipped before the mainnet cutover:

1. **Ledger history database** — a per-network, regenerable SQLite archive of
   *every* XRPL transaction touching the LFG NFT collection and the BRIX
   token, from collection genesis to now, kept fresh by the existing
   listeners. This is the documented project history and the data foundation
   for leaderboards, provenance, and #48 accounting.
2. **Leaderboard** on the Discord Activity home screen (and, for free, the
   Telegram Mini App): time-filterable rankings across Users, NFTs, and BRIX.

## Non-goals

- Implementing the BRIX claim flow (#48). This design only ensures the data
  and schema support it.
- Marketplace/trade UI. Leaderboards are read-only.

## 1. History store

New per-network SQLite files `history_mainnet.db` / `history_testnet.db`
(gitignored, regenerable — same posture as `onchain_*.db`).

### Raw archive (source of truth)

```sql
CREATE TABLE xrpl_txs (
    tx_hash      TEXT PRIMARY KEY,
    ledger_index INTEGER,
    close_time   INTEGER,        -- unix seconds
    tx_type      TEXT,
    account      TEXT,           -- tx sender
    source_tag   INTEGER,
    raw_json     TEXT            -- verbatim {tx, meta}
);
```

Deduped by `tx_hash` across all scrape sources; inserts are idempotent
(`INSERT OR IGNORE`).

### Backfill sources (one-time, resumable)

`scripts/backfill_history.py --network testnet|mainnet`:

1. **`account_tx` over the NFT issuer** (`rLfgoMint…` mainnet / SEED account
   testnet) — mints, burns, issuer offers, transfer-fee-bearing sales,
   LFGO/BRIX payments touching the issuer.
2. **`account_tx` over the BRIX issuer** (`rLfgoBriX…`) — complete for BRIX:
   every IOU movement adjusts the issuer's RippleState nodes, so all
   payments, TrustSets, and AMM ops appear here.
3. **clio `nft_history` per `nft_id`** for every token known to
   `onchain_<net>.db` (5,562 on mainnet) — catches zero-price NFT transfers
   and third-party trades that never touch the issuer account (the one hole
   in issuer-only scraping). clio-only method → `CLIO_WS_URL`, never `WS_URL`.
4. **`account_tx` over the airdrop distributor wallet** — the historical
   daily BRIX airdrop was sent from a separate non-issuer wallet. Its address
   will be evident from the BRIX payment data (dominant non-issuer sender);
   confirm with the user, then record it in config and scrape it explicitly
   so airdrop history is first-class.

A `backfill_state(source, cursor, updated_at)` table stores per-source
pagination markers so the backfill resumes cleanly after interruption
(mainnet `nft_history` × 5,562 tokens is hours of work). Re-running is safe.

### Derived event tables (rebuildable from raw)

`scripts/derive_history_events.py` (also invoked by the backfill) parses
`raw_json` into:

```sql
CREATE TABLE nft_events (
    tx_hash    TEXT,
    nft_id     TEXT,
    nft_number INTEGER,
    event      TEXT,     -- mint | burn | transfer | sale | offer_create
                         -- | offer_cancel | modify (= trait swap)
    from_addr  TEXT,
    to_addr    TEXT,
    price_drops INTEGER, -- XRP sales
    price_token TEXT,    -- JSON {currency, issuer, value} for IOU sales
    ledger_index INTEGER,
    ts         INTEGER,
    PRIMARY KEY (tx_hash, nft_id)
);

CREATE TABLE brix_events (
    tx_hash      TEXT,
    account      TEXT,     -- whose balance changed
    counterparty TEXT,
    delta        REAL,     -- signed BRIX
    kind         TEXT,     -- payment | airdrop | amm_swap | amm_deposit
                           -- | amm_withdraw | trustset | claim (future, #48)
    ts           INTEGER,
    PRIMARY KEY (tx_hash, account)
);
```

`kind = airdrop` is tagged when the sender is the known distributor wallet.
The `claim` kind is reserved for #48. Derivation is a pure function of
`raw_json` → unit-testable from canned fixtures, and the derived tables can
always be dropped and rebuilt.

### Balance snapshots

Nightly job (cron via pm2 or listener timer):

```sql
CREATE TABLE balance_snapshots (
    snap_date TEXT,      -- YYYY-MM-DD
    account   TEXT,
    brix      REAL,
    lp_tokens REAL,      -- BRIX/XRP AMM LP balance
    PRIMARY KEY (snap_date, account)
);
```

Windowed richlist = delta between snapshots; All-Time richlist = live
trustline scan (no history needed).

## 2. Freshness (live sync)

Extend `lfg_core/nft_listener.py` — the same pm2 `lfg-index-*` processes —
with a second consumer on the tx stream it already reads: every relevant tx
is appended to `xrpl_txs` + derived tables. Add account subscriptions for the
BRIX issuer (and distributor wallet) alongside the existing NFT stream. No
new process.

## 3. Leaderboard API

`GET /api/leaderboard?board=<key>&period=today|week|month|year|all&start=<iso>`
in `lfg_service/app.py` (aiohttp, same pattern as existing routes).
`start` selects a specific past week/month/year; omitted = current period.

| Board key | Ranking | Windowed? |
|---|---|---|
| `users_nfts` | NFTs held (all-time = current holdings; windowed = net acquisitions) | yes |
| `users_swaps` | trait-swap txs (`modify` events) per wallet | yes |
| `users_builds` | assemble ops per wallet (economy data already exists) | yes |
| `nft_swaps` | most-swapped NFTs (`modify` count per `nft_id`) | yes |
| `nft_rarity` | rarity rank from the existing rarity engine over the live census | no (period-independent) |
| `brix_rich` | BRIX balance (live trustlines all-time; snapshot deltas windowed) | yes |
| `brix_lp` | AMM LP-token balance | yes |
| `brix_earned` | BRIX received from issuer + distributor (`kind IN (payment-from-issuer, airdrop, claim)`) | yes |

Response: top 25 rows — `{rank, wallet, display_name, value, image?}` —
display name resolved via the accounts/Users tables with truncated r-address
fallback; NFT boards include the token thumbnail. Plus `me: {rank, value}`
for the authenticated caller if outside the top list. 60-second in-memory
cache per `(board, period, start)`.

## 4. UI (Activity home)

A Leaderboard card on the home `mint-panel`, under the action buttons —
vanilla JS, no build step, existing sticker-card styling:

- **Time-filter chip row:** Today · Week · Month · Year · All Time.
  Week/Month/Year chips reveal a ‹ › stepper to walk to specific past
  periods (maps to the `start` param).
- **Board tabs** grouped **Users / NFTs / BRIX**, each group with its
  sub-boards.
- **Top-10 list**: rank, thumbnail (NFT boards), name, value; the caller's
  own row pinned beneath if outside the top 10.
- The `.actions` button row keeps layout room for the future "Claim BRIX"
  button (#48) — no stub code, just a layout that accommodates a 4th button.

Same client is served to the Telegram Mini App unchanged.

## 5. Airdrop catch-up ("owed BRIX") — schema support only

Historical rule: **1 BRIX per owned, UNLISTED LFG NFT per day** (an NFT with
an active sell offer earned 0 that day). Once the history DB exists, we can
compute per-wallet owed-vs-paid: daily holdings are reconstructable from
`nft_events`, listed status from offer events, and payments from
`brix_events` (`airdrop` kind).

To avoid flooding the market, catch-up will be delivered **mostly as in-app
credit** (e.g. pre-paid trait swaps) rather than on-chain BRIX. Design
implication now: the schema above is sufficient (owed amounts are derivable,
not stored); a future `credits` ledger belongs to the #48 implementation,
not this project. `brix_events.kind = 'claim'` is reserved for it.

## 6. Testing

- **Derivation unit tests** from canned tx JSON fixtures: NFTokenMint,
  AcceptOffer (XRP sale, IOU sale, zero-price transfer), Burn, Modify,
  Payment (BRIX, airdrop), AMM deposit/withdraw/swap.
- **API tests** over a seeded `history_*.db` (all boards × periods,
  `start` param, cache behavior, `me` row).
- **Conservation cross-check** (auditor-style): `nft_events` mints − burns
  must reconcile with the live census in `onchain_<net>.db`; drift = alarm.
- **Backfill idempotence**: re-running produces zero new rows.
- New test files follow the env-guard preamble convention.

## Build order (cutover-aware)

1. History schema + backfill scripts (kick off the mainnet backfill early —
   it runs in the background for hours).
2. Event derivation + tests.
3. Listener extension (live freshness).
4. Leaderboard API + tests.
5. Activity UI card.
6. Snapshots job (needed for windowed BRIX boards; richlist all-time works
   without it, so it can land last).
