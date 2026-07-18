# Self-healing on-chain listener: ledger-gap catch-up

**Date:** 2026-07-18
**Status:** Approved design
**Motivation:** 2026-07-17 incident — `lfg-index-mainnet` restarted 6× (deploys),
missed the `NFTokenAcceptOffer` txs for mints #4020/#4037/#4052, and the stale
`onchain_nfts.owner` rows made delivered NFTs look undelivered during an
owed-users audit. The listener has reconnect backoff but no memory of where it
stopped: every restart — deploy, crash, reboot — silently loses whatever landed
while it was down, until someone runs a manual backfill.

## Goal

`scripts/onchain_listener.py listen` heals its own gaps. After any downtime it
replays the missed ledger range through the exact same apply pipeline as the
live stream, so the index (`onchain_nfts`), economy tables, `market_listings`,
and the history archive all catch up with no manual intervention. Deploys may
keep restarting the listener; it stops mattering.

## Non-goals

- Changing the deployer / pm2 restart lists (considered, deliberately out of
  scope — correctness lives in the listener, not the deploy flow).
- Historical backfill. The cursor is born "now"; everything before it remains
  the job of `backfill_onchain.py` / `backfill_history.py` / the Bithomp CSV
  import.
- A periodic owner-reconciliation sweep (possible later belt-and-suspenders;
  not needed once gaps self-heal).

## Key constraint that shapes the design

The listen loop subscribes to the **whole-network transaction stream**
(`Subscribe(streams=[TRANSACTIONS])`) and filters per tx. A third-party
`NFTokenAcceptOffer` (buyer accepts a 0-drop offer; issuer not a party, issuer
account untouched) is visible on that stream but is NOT guaranteed to appear in
the issuer's `account_tx`. Replay must therefore have identical coverage to the
stream: **walk the missed ledgers themselves**, not any account's tx list.

## Design

### 1. Ledger cursor — `listener_state`

New key-value table in the per-network `onchain_<net>.db`, created by
`nft_index.init_db` (self-migrating, like every other store there):

```sql
CREATE TABLE IF NOT EXISTS listener_state (
    key   TEXT PRIMARY KEY,   -- 'last_ledger'
    value INTEGER NOT NULL
)
```

- `last_ledger` = highest ledger index the listener has **fully processed**.
- The live loop upserts it whenever a processed tx's `ledger_index` advances
  past the stored value, committed together with (never before) that tx's own
  writes. A cursor write must never precede the tx writes it covers.
- **Bootstrap:** if the key is absent at startup, write the current validated
  ledger index and skip replay entirely.

### 2. Replay on every (re)connect

One code path used both at startup and on mid-run reconnects (the existing
`while True` reconnect loop):

1. **Subscribe first**, and buffer incoming stream messages in memory.
2. Read `last_ledger`; fetch the current validated ledger index. The gap's
   upper bound is `first_buffered_ledger - 1` when a stream message has
   already been buffered, else `current_validated`; the gap is
   `(last_ledger, upper]` (empty gap → skip straight to draining).
3. For each ledger in the gap, issue a `ledger` request with
   `transactions=true, expand=true`, normalize each returned tx into the shape
   `_normalize_stream_tx` produces (tx dict + `meta` + `hash` +
   `ledger_index`), and feed it through the existing `process_stream_tx` — the
   single seam that already drives index, economy, market, and history writes.
   The cursor advances per replayed ledger, same rule as live.
4. **Drain the buffer** through `process_stream_tx`, then continue live.

Overlap between replay and buffered stream messages is harmless: every store
is keyed on `tx_hash` / `nft_id` and upsert-idempotent (the manual backfills
already re-apply overlapping txs routinely).

### 3. Gap cap — bounded, honest degradation

`REPLAY_MAX_LEDGERS` (default **3600** ≈ 4 h at ~4 s/ledger; env-overridable).
If the gap exceeds it: do NOT walk it. Log CRITICAL naming the exact recovery
commands (`backfill_onchain.py --network <net>`, `backfill_history.py
--network <net>`), set the cursor to the current validated ledger, and go
live. A multi-hour ledger walk would hammer clio and delay live processing for
little benefit over the purpose-built backfills.

### 4. Error handling

- A failed `ledger` fetch during replay retries with the existing
  reconnect-style backoff; a replay abandoned mid-gap is safe because the
  cursor only ever covers applied txs — the next (re)connect resumes the
  remainder.
- A tx that raises inside `process_stream_tx` during replay follows the same
  policy as the live loop (today: the exception propagates to the reconnect
  handler). No new swallowing.
- clio occasionally lacks a ledger (pruning): treat a definitive
  `lgrNotFound` as "advance past it with a WARNING" — the stream-vs-replay
  equivalence only holds for ledgers clio can serve.

### 5. What does not change

- `process_stream_tx` and everything below it: untouched. Replay is a second
  driver of the same seam.
- The whole-network subscribe + per-tx filtering.
- SourceTag/memos are irrelevant here (read-only consumer).

## Components

| Unit | Responsibility |
|---|---|
| `nft_index.init_db` | + `listener_state` table |
| `nft_index.get_last_ledger / set_last_ledger` (or a tiny `listener_state` helper module) | cursor read/upsert |
| `scripts/onchain_listener.py::_replay_gap(conn, hconn, ctx, fetch_ledger, from_ledger, to_ledger)` | walk gap, normalize, drive `process_stream_tx`, advance cursor; extracted and injectable for tests |
| `scripts/onchain_listener.py::_listen` | subscribe → buffer → `_replay_gap` → drain → live loop; cursor upkeep on live txs |

## Testing

Unit tests with a fake ledger-fetcher / fake stream (no websocket), following
the existing `process_stream_tx`-is-testable pattern:

1. Gap replayed in ledger order through `process_stream_tx`; cursor advances
   only after each ledger's txs are applied.
2. Bootstrap: empty `listener_state` → cursor written to current ledger, no
   replay calls.
3. Over-cap gap: no ledger walks, CRITICAL logged, cursor jumped to current.
4. Replay/buffer overlap: a tx present in both applies once (idempotent
   stores), final state correct.
5. Mid-replay failure: cursor reflects only applied ledgers; a second replay
   resumes and completes the remainder.
6. Cursor persistence across a simulated restart (new conn, same DB file).

Env-guard preamble per the repo test convention for any new test module that
imports `lfg_core` at module top.

## Rollout

Ships as normal code → staging (`main`) → prod (`promote.sh`). First prod
start bootstraps the cursor to "now"; before that first start there is one
last unhealed window — run `backfill_onchain.py --network mainnet` once after
deploy (or accept the usual manual patch if anything looks stale). No config
required; `REPLAY_MAX_LEDGERS` env knob optional.
