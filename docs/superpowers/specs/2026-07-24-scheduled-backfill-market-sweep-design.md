# Scheduled backfill_market sweep — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #288

## Problem

The `market_listings` index in `onchain_<net>.db` is a derived, droppable
mirror of on-ledger `NFTokenOffer` state, kept current by three sync layers:
the live listener (`lfg_core/nft_listener.apply_market_tx`, wired into
`scripts/onchain_listener.py`), the service finalize-writes in
`lfg_core/market_flow.py`, and the manual `scripts/backfill_market.py` sweep.

The listener has no memory of where it stopped, so every restart — deploy,
crash, reboot — silently loses whatever `offer_create` / `offer_cancel` /
`accept` txs landed while it was down. The #203 offer-coverage audit
(2026-07-20) found the mainnet index had drifted: a single full
`backfill_market.py --network mainnet` run (4,128 characters, 0 fetch
failures) surfaced **27 live sell offers the listener had missed**
(164 → 191 live rows) and closed **4 stale rows**. The drift was repaired
manually, but nothing runs the sweep on a schedule — browse coverage
(`GET /api/market/listings`) only self-heals when a human remembers to run it.

`backfill_market.py` is already idempotent, timestamp-preserving
(`market_store.upsert_listing` `COALESCE`s `created_ledger`/`created_ts` on
conflict), and RPC-failure-safe (a per-token fetch failure excludes that token
from the stale-close pass, so a transient blip can never falsely close a live
listing). It just needs to be run periodically, per network, wired into the
pm2 stacks the same way `lfg-snapshot` is.

## Constraints discovered

- **Must not fight the live listener.** Both the listener
  (`stg-index-testnet` / `lfg-index-mainnet`) and the backfill open the SAME
  per-network `onchain_<net>.db` and write to `market_listings` / `buy_offers`.
  `nft_index.init_db` (`lfg_core/nft_index.py:107`) opens a plain
  `sqlite3.connect(path)` with **no `busy_timeout` and no WAL journal mode** —
  a write-write collision raises `sqlite3.OperationalError: database is
  locked`. The sweep's writes are short upserts dominated by RPC wait, but a
  collision is possible; the connection needs a `busy_timeout` so a brief lock
  is waited out rather than crashing the cron. The COALESCE-on-conflict upsert
  and the fetch-failure-exempt stale-close already make the two writers
  *logically* safe to interleave (neither clobbers the other's creation facts
  nor false-closes a live row) — the only gap is transaction-level locking.
- **No transaction built.** The sweep is read-only against the ledger
  (`xrpl_ops.get_nft_sell_offers` / `get_nft_buy_offers`, `get_ledger`
  time) and writes only the local index. No `NFTokenOffer`,
  no XUMM payload, no signing — so **no SourceTag / provenance-memo surface
  here** (both apply only to txs the app submits or asks a wallet to sign).
- **Per-kind network seam.** `backfill_market.py` sweeps whatever network its
  `--network` resolves — characters and traits both live in the same
  `onchain_<net>.db`. Prod runs mainnet, staging runs testnet; the cron entry
  in each ecosystem file must pass the matching `--network`, exactly like
  `lfg-snapshot` / `stg-snapshot` and the two index listeners.
- **Cost.** ~8k RPCs per mainnet run at current collection size (one
  `get_nft_sell_offers` + one `get_nft_buy_offers` per live character and per
  trait token, `FETCH_CONCURRENCY = 16`). Off-peak, low frequency.
- **`--no-autorestart` cron semantics.** A pm2 cron process (`autorestart:
  false` + `cron_restart`) shows as "stopped" between runs — that is normal
  (documented for `lfg-snapshot`), not a failure. The script must `exit(0)` on
  success so pm2 parks it cleanly instead of thrashing.

## Design

Two independent seams: (1) a small hardening of `scripts/backfill_market.py`
so a scheduled run logs drift legibly and can't crash on a lock, and (2) the
pm2 cron wiring in both ecosystem files.

### 1. Script hardening (`scripts/backfill_market.py`)

- **Structured logging instead of bare `print`.** Add
  `logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s
  %(message)s")` in `_amain` (mirrors `scripts/snapshot_balances.py:92`), so
  the pm2 log carries timestamped lines. Keep the existing human summary but
  emit it through `logging.info`.
- **Drift signal.** `backfill_market()` already returns
  `closed_stale`, `live_listings`, `fetch_failures`, `live_bids`,
  `bids_closed_stale`, `bid_fetch_failures`. The sweep does not currently
  distinguish "rows the listener already had" from "rows this sweep newly
  discovered", so surface drift as the two facts we can cheaply compute:
  **`closed_stale + bids_closed_stale`** (rows the listener left dangling) and
  **`fetch_failures + bid_fetch_failures`** (coverage gaps this run). Log a
  single machine-greppable summary line, e.g.
  `logging.warning("backfill_market drift: net=%s closed_stale=%d
  bids_closed_stale=%d fetch_failures=%d ...")` at WARNING level **only when
  `closed_stale or bids_closed_stale or fetch_failures or bid_fetch_failures`
  is non-zero**, INFO otherwise. This makes drift visible to a
  `pm2 logs lfg-market-backfill --err` / `grep "backfill_market drift"` check
  without any new alerting infra, and keeps the door open for a later webhook.
- **Lock safety.** Set `PRAGMA busy_timeout = 30000` on the connection opened
  in `_amain` (via `conn.execute`) before the sweep, so a momentary listener
  write lock is waited out rather than raised. This is the minimal change; a
  repo-wide WAL switch in `nft_index.init_db` is deliberately left as an open
  question (it would benefit every concurrent reader/writer but is a broader
  change than this issue).
- **Optional `reports/` drift log (belt-and-suspenders).** Append a one-line
  JSON record (`{ts, network, closed_stale, fetch_failures, live_listings,
  ...}`) to `reports/backfill_market_drift.log` (gitignored, same posture as
  `reports/trait_dashboard_audit.log`) so drift history is inspectable after
  the fact. Gate behind a `--report` flag (default off) to keep the manual CLI
  behavior byte-identical; the cron entry passes `--report`.
- Exit code stays `0` on a completed sweep (drift is a WARNING, not a
  failure) so the pm2 cron parks cleanly; the existing DB-missing / bad-network
  paths are untouched.

### 2. pm2 cron wiring

Add one cron process to each ecosystem file, mirroring the `lfg-snapshot` /
`stg-snapshot` entries exactly (`autorestart: false`, `cron_restart`,
`interpreter: PY`, correct `--network`):

`ecosystem.prod.config.js` (mainnet):
```js
{ name: "lfg-market-backfill", cwd: CWD, script: "scripts/backfill_market.py",
  interpreter: PY, args: ["--network", "mainnet", "--report"],
  cron_restart: "30 3 * * *", autorestart: false },
```

`ecosystem.staging.config.js` (testnet):
```js
{ name: "stg-market-backfill", cwd: CWD, script: "scripts/backfill_market.py",
  interpreter: PY, args: ["--network", "testnet", "--report"],
  cron_restart: "30 3 * * *", autorestart: false },
```

**Cadence choice: nightly at 03:30 UTC.** Nightly (not N-hourly) because drift
accrues slowly (it's downtime-driven — a handful of deploys/restarts a day) and
each mainnet run is ~8k RPCs; a nightly cadence bounds worst-case browse
staleness to ~24h while keeping RPC load negligible. 03:30 is off-peak and is
**deliberately offset from `lfg-snapshot`'s 00:10** so the two RPC-heavy crons
don't stack. Both stacks use the same wall-clock slot; they hit different
networks/endpoints so there is no contention between them.

### Ops rollout (documented, not executed by the plan)

The ecosystem files are read on `pm2 start ecosystem.*.config.js`; adding an
app to the file does not start it on already-running stacks. Going live is an
ops step per stack: `pm2 start ecosystem.prod.config.js --only
lfg-market-backfill` (and the staging equivalent), then `pm2 save`. Document
this in the PR body / CLAUDE.md "Running" table alongside the existing cron
processes.

## Out of scope

- The self-healing listener ledger-gap catch-up
  (`docs/superpowers/specs/2026-07-18-listener-gap-catchup-design.md`) — a
  complementary, stream-level fix. This issue is the belt-and-suspenders
  periodic sweep that spec explicitly deferred ("a periodic
  owner-reconciliation sweep … not needed once gaps self-heal"); the two are
  independent and both wanted.
- A repo-wide WAL migration of `onchain_<net>.db` (see open questions).
- Real push alerting (Discord webhook / admin channel) — the WARNING log line
  + optional `reports/` record is the MVP; a webhook is a clean follow-up on
  top of the same drift signal.
- The `onchain_nfts` / `trait_tokens` / `nft_events` populations —
  `backfill_market` only rebuilds `market_listings` / `buy_offers`; those other
  stores have their own backfills (`backfill_onchain.py`,
  `backfill_history.py`) and are out of scope here.

## Open questions / decisions for maintainer

1. **Cadence:** nightly (proposed) vs N-hourly. Nightly bounds RPC cost and
   fits the slow, downtime-driven drift profile; N-hourly tightens the browse
   staleness window at ~8k RPC/run cost. Pick one.
2. **Time slot:** 03:30 UTC proposed (off-peak, offset from `lfg-snapshot`).
   Confirm this is genuinely off-peak for the user base.
3. **WAL vs `busy_timeout`:** this design does the minimal `busy_timeout` on
   the backfill connection only. Should `nft_index.init_db` switch the DB to
   WAL journal mode repo-wide (helps the listener + every reader too), or is the
   per-connection timeout enough for now?
4. **Alerting depth:** is a greppable WARNING log line + `reports/` record
   sufficient, or should drift over a threshold post to the admin Discord
   channel (would need a webhook URL env var — no bot token in the cron)?
5. **Staging value:** testnet drift matters little operationally. Wire the
   staging cron for parity/testing, or prod-only? (Proposed: wire both for
   parity, matching every other cron.)

## Testing

- **Unit (`tests/`):** with the env-guard preamble, drive `backfill_market()`
  against an in-memory / temp `onchain` DB seeded with (a) a live
  `market_listings` row whose offer no longer exists on-ledger (stub
  `get_nft_sell_offers` → `[]`) and assert it's closed `stale` and the returned
  `closed_stale >= 1`; (b) a token whose fetch raises, asserting it lands in
  `fetch_failures` and its row is NOT closed. Assert the WARNING drift line is
  emitted (via `caplog`) when any of the four drift counters is non-zero and
  suppressed when all are zero. Assert `--report` appends a parseable JSON line
  and default (no flag) writes no file.
- **Integration:** run `backfill_market.py --network testnet` against the real
  testnet index twice in a row; second run reports `closed_stale=0`,
  `fetch_failures=0`, and `live_listings` unchanged (idempotence + timestamp
  preservation — spot-check a row's `created_ts` is untouched).
- **Concurrency smoke:** run the sweep while `stg-index-testnet` is live and
  confirm no `database is locked` crash (the `busy_timeout` path).
- **Manual/ops smoke:** `pm2 start ecosystem.staging.config.js --only
  stg-market-backfill`, confirm it runs, logs the summary, then shows
  "stopped" (parked) — matching `lfg-snapshot` behavior — and re-fires on the
  cron.
