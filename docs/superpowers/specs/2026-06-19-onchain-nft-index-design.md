# LFG On-Chain NFT Index — Design

**Date:** 2026-06-19
**Status:** Approved

## Problem

LFG trait-swap tooling needs an authoritative, always-fresh view of **every live
NFToken** in the collection — including the **multiple tokens per edition number**
that prior swaps and reminting create. The app DB (`lfg_nfts.db` `LFG` table) is
keyed `nft_number` (one row per edition) and physically cannot hold those
duplicates, so the layer-coverage auditor currently scrapes the chain live. On
testnet that's fast (BunnyCDN metadata); on mainnet it scrapes IPFS, which is
slow and unreliable (~10–20% of fetches time out), so mainnet audits are not
authoritative.

Concrete failure that motivated this: testnet `#3547` exists twice on-chain (a
`0018…` original and a `0019…` "Wonder" variant). The DB held only the original;
the Wonder variant — the one that broke a real swap — was invisible to any
DB-based logic.

## Goal

A per-`nft_id` SQLite index of every NFToken in the LFG collection, kept
continuously fresh, so the auditor (and future swap tooling) can query it fast
and offline on **both** networks instead of scraping the chain.

## Prior art surveyed (and what we take)

- **`~/xrpl-nft-fetcher/fetcher.js`** — already produces an `nft_id`-keyed SQLite
  via `nfts_by_issuer` + `nft_info`; it built today's `lfg_nfts.db` (the
  duplicates were collapsed at the *populate* step, not the fetch). We **port its
  approach to Python** rather than reuse the Node tool.
- **`~/baysed/.../nft_ownership_sync.py`** — reference architecture: `nft_id`-keyed
  tables, clean testnet/mainnet DB separation, a sync worker with `snapshot` +
  `listen` subcommands. We **mirror this shape**.
- **Kinesis-SDK** (MIT) — its `NFTokenModify` owner-resolution (resolve current
  owner via Clio `nft_info`, attach the XLS-46 `Owner` field). We **lift that
  pattern** for the listener's modify/transfer handling.
- Aaditya's `xrpl-indexer` (no NFT support) and `xrpl-nft-minter` (bulk-mint
  service) — **not used**.

## Decisions

- **Language:** Python, under `lfg_core`, reusing `xrpl_ops`, `swap_meta`,
  `layer_store`, `config`. One stack, one pm2 family.
- **DB layout:** separate per-network files (`onchain_testnet.db` /
  `onchain_mainnet.db`), selected by `XRPL_NETWORK`. Hard safety boundary.
- **Location:** new `nft_id`-keyed table owned by `lfg_core`, in the LFG repo.

## Schema

One table per network DB:

```sql
CREATE TABLE onchain_nfts (
    nft_id          TEXT PRIMARY KEY,   -- every token; duplicates per edition kept
    nft_number      INTEGER,            -- edition (NOT unique)
    owner           TEXT,
    is_burned       INTEGER DEFAULT 0,
    mutable         INTEGER,            -- lsfMutable flag (0x0010)
    uri_hex         TEXT,               -- current on-chain URI (hex)
    body            TEXT,               -- detected body class (male/female/ape/skeleton)
    attributes_json TEXT,               -- normalized attributes (swap_meta.normalize_attributes)
    image           TEXT,
    ledger_index    INTEGER,            -- ledger of last update
    last_synced_at  TIMESTAMP
);
CREATE INDEX idx_onchain_number ON onchain_nfts(nft_number);
CREATE INDEX idx_onchain_live   ON onchain_nfts(is_burned);
```

`attributes_json` stores the normalized attribute list so the auditor can read it
directly; `body` is cached for fast filtering. Burned tokens are kept (with
`is_burned=1`) for history; the auditor filters them out.

## Components

A shared module plus three thin entry points, sequenced as three phases (each its
own spec addendum / plan / PR).

### `lfg_core/nft_index.py` (shared)

- `index_db_path(network) -> str` — resolves the per-network DB file (env
  override `ONCHAIN_DB_PATH`, else `onchain_<network>.db`).
- `init_db(path)` — creates the schema (idempotent).
- `enumerate_tokens(clio, issuer, taxon) -> [TokenRef]` — pages `nfts_by_issuer`,
  returning `nft_id, nft_number?, owner, is_burned, flags, uri_hex` per token
  (moved here from the auditor's `enumerate_onchain`).
- `token_record(token, metadata) -> OnchainNft` — normalize metadata
  (`swap_meta.normalize_attributes` + `detect_body`), build the row.
- `upsert(conn, record)` — INSERT … ON CONFLICT(nft_id) DO UPDATE.
- `live_nfts(conn) -> [OnchainNft]` — query non-burned tokens (auditor source).

Pure helpers (`token_record`) are unit-testable without I/O.

### Phase 1 — Index foundation + backfill

`scripts/backfill_onchain.py --network testnet|mainnet`:
- Enumerate all tokens, fetch metadata (shared aiohttp session, bounded
  concurrency), upsert each. Idempotent. Tokens whose metadata can't be fetched
  are still recorded (uri_hex + flags) with a null `attributes_json` so they are
  visible, not dropped.
- Run for both networks to populate the index.

### Phase 2 — Repoint the auditor

`scripts/audit_layer_coverage.py`:
- Default source becomes `nft_index.live_nfts()` (DB) for the configured network
  — instant, offline, complete on both networks.
- `--live` flag retains the direct on-chain enumeration (for when the index is
  stale or unavailable).
- Must reproduce the current testnet result (4 NFTs incl. #3547) from the DB.

### Phase 3 — Live listener

`scripts/onchain_listener.py --network … {snapshot|listen}`:
- `snapshot` delegates to the Phase-1 backfill.
- `listen` websocket-subscribes (clio per network) to the transaction stream and
  upserts on:
  - **NFTokenMint** — new token (fetch metadata).
  - **NFTokenAcceptOffer** — ownership change (resolve current owner via
    `nft_info`, the Kinesis pattern).
  - **NFTokenBurn** — set `is_burned=1`.
  - **NFTokenModify** — re-fetch metadata, update `uri_hex` / `attributes_json` /
    `body` / `image` (the gap in the existing Node listener; essential because
    LFG swaps mutate in place).
- pm2 services `lfg-index-testnet` + `lfg-index-mainnet` run `listen`; a one-off
  `snapshot` precedes first start.

## Data flow

```
nfts_by_issuer (clio) ─┐
                        ├─► nft_index.token_record ─► onchain_nfts (per-network DB)
metadata (CDN/IPFS) ───┘                                   ▲
NFToken* tx stream (clio ws) ─► listener handlers ─────────┘
onchain_nfts ─► live_nfts() ─► layer-coverage auditor (fast, offline)
```

## Error handling

- Metadata fetch failure → record the token with null `attributes_json`; never
  drop it. Backfill/auditor report the count of such tokens.
- Listener: per-transaction try/except with logging; a bad tx must not kill the
  stream. Reconnect with backoff on ws drop. On reconnect, an optional catch-up
  `snapshot` reconciles anything missed while down.
- `nft_info` owner-resolution failure on transfer → keep prior owner, log.

## Network config

clio endpoints: mainnet `wss://s2-clio.ripple.com`, testnet
`wss://clio.altnet.rippletest.net:51233`. Issuer/taxon from `config`
(mainnet `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ`/1760; testnet
`config.SWAP_ISSUER_ADDRESS`/1760). All overridable by flag.

## Testing

- `token_record` / `upsert` / `live_nfts` — unit tests over a temp SQLite with
  synthetic tokens (clean NFT, the #3547 duplicate pair, burned token,
  unreadable-metadata token).
- Backfill — injected enumerator + metadata fetcher, asserts rows upserted and
  re-run idempotency.
- Auditor (Phase 2) — existing tests carry over; add a test that the DB source
  yields the same `NftResult`s as the live source for a fixture.
- Listener (Phase 3) — injected ws message stream; assert each tx type produces
  the right upsert (mint adds, accept changes owner, burn flips is_burned, modify
  updates attributes).

## Out of scope

- Backfilling the app's edition-keyed `LFG` table (separate concern).
- A REST/query API over the index (future, if other surfaces need it).
- Mainnet metadata reliability beyond "record what we can, flag the rest" — the
  index makes this a one-time backfill cost, not a per-audit cost.
