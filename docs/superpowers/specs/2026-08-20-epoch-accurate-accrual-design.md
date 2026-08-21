# Epoch-accurate BRIX accrual from the archive (#411 option 2)

**Status:** implemented — branch feat/411-412-epoch-accrual (PR number to be stamped on open)
**Date:** 2026-08-20
**Issue:** [#411](https://github.com/Team-Hamsa/LFG/issues/411)
**Companion spec:** `2026-08-20-brix-gap-reimbursement-design.md` (#412) — shares
the `epoch_state` foundation defined here.

## Problem

`scripts/accrue_brix.py` decides who earns by asking the ledger, live, once per
token: `brix_drip.fetch_sell_offer_state` issues one `nft_sell_offers` RPC per
eligible NFT. The first mainnet run reported `4710 live tokens, 4510 eligible;
checking offers…` and spent ~15 minutes there.

PR #416 parallelizes those lookups (semaphore, 15 concurrent) and cuts the wall
time to well under a minute. This spec replaces the lookups altogether.

Two properties of the current design are worth keeping explicitly in view,
because the replacement must preserve or improve both:

1. **Fail-closed.** `evaluate_accruals` treats `None` (lookup failed) as
   never-pay. A BRIX grant cannot be clawed back, so paying a listed NFT
   through a clio outage is unrecoverable while a missed BRIX is not.
2. **Idempotent.** `(epoch_date, nft_id)` is the accruals PK, so a re-run —
   or a catch-up pass overlapping an already-accrued day — is a no-op.

The current design also has a defect that is easy to miss: because the PK
blocks a later grant for the same `(epoch, nft_id)`, an unreadable offer state
means that NFT-day is lost **permanently**. Fail-closed today means silent,
irreversible under-payment.

## What the epoch model actually needs

A daily live check is not required. What accrual needs is listed-state and
owner-of-record **as of the close of epoch D** — and both are already recorded
in the history archive, which the pm2 listeners dual-write on every streamed
transaction.

The issue proposed reading `market_listings` after the 03:30 `lfg-market-sweep`.
That was rejected on inspection: `market_listings` on mainnet holds 472 rows
with `created_ts` no earlier than 2026-07 (263 rows carry no `created_ts` at
all, having been backfilled). It is a *current-state* index, adequate for
"what is listed now" and useless for "what was listed on 2025-11-04". Gating
accrual on the sweep would also couple the drip's schedule to an unrelated job.

`history_<net>.db` is the better source: `xrpl_txs` holds raw transactions back
to 2022 and `nft_events` holds offer events back to 2023.

## Design

### 1. Extend `nft_events` derivation

`history_events.derive_nft_events` records `offer_create` with `from_addr`,
`to_addr` and price, but **not**:

- the offer's sell/buy flag — so a bid (#283 native buy offer) is
  indistinguishable from a listing, and
- the created `offer_index` — so an offer cannot be matched to the
  `offer_cancel` or accept that later closes it.

Add both. `Flags` is on the `NFTokenCreateOffer` transaction and the created
offer's index is in its metadata; `offer_cancel` already reads deleted offer
nodes and can carry `offer_index` the same way. Two new columns on
`nft_events`, self-migrating like every other schema addition in this repo.

`nft_events` is explicitly derived, droppable and rebuildable from `xrpl_txs`,
so populating the new columns for history is a `derive_history_events.py`
rerun — no chain scraping.

### 2. New module: `lfg_core/epoch_state.py`

```
def state_at_epoch(hconn, oconn, epoch: str) -> dict[str, EpochToken]
```

Replays the archive up to the close of `epoch` (23:59:59 UTC) and returns, per
`nft_id`: `owner` (owner-of-record at epoch close), `listed` (a sell offer
owned by that same holder was open at epoch close), and `live` (minted, not
burned).

Rules, mirroring the semantics the live path already implements:

- Owner-of-record follows `mint` → `transfer`/`sale` → `burn`.
- A sell offer counts as a listing only while its **creator is still the
  current holder** — an offer left behind by a previous owner is unfillable,
  exactly as `brix_drip.classify_sell_offers` decides today.
- Destination-locked sell offers **do** count; that is how brokered
  marketplaces list (unchanged from today's rule).
- Buy offers never count.

Pure over the two databases: no network, no XRPL client, no clock.

### 3. Certification gate

The replay is only as good as the archive's continuity. `archive_state`
already carries `baseline_complete`, `continuity_gap_after`,
`validated_close_time` and a listener heartbeat — the same machinery
sponsored-mint eligibility fails closed on.

An epoch is **payable** only when all hold:

- `baseline_complete = 1`
- `validated_close_time` is past the epoch's close (the archive has seen the
  whole epoch)
- no continuity gap overlaps the epoch's window

Otherwise the epoch is **deferred**: nothing is written, and
`brix_meta.last_accrued_epoch` is **not advanced past it**, so a later run
completes it once the listener's auto catch-up (#402) has healed the gap. The
accruals PK makes that completion safe by construction.

This is the substantive improvement over today's posture. An uncertifiable
epoch pays nobody until the archive proves itself, then pays everybody
correctly. It can never pay a listed token, and it can no longer under-pay
forever in silence.

### 4. `accrue_brix.py`

Per owed epoch: `state_at_epoch` → certification gate → existing
`evaluate_accruals` → `INSERT OR IGNORE`. Zero RPCs on the happy path, so a
run is DB-bound and finishes in seconds.

`verify_endpoint_chain` stays — the chain-identity check still guards against
running against the wrong network — but the per-token sweep is gone.

`brix_drip.fetch_sell_offer_state` is retained (unused by this path) as the
live-verification helper, keeping PR #416's parallelism useful for ad-hoc
checks and any future live audit.

## Testing

- `epoch_state` unit tests over a synthetic archive: mint-then-hold; transfer
  mid-window (credit follows the holder at epoch close); sale; burn; sell
  offer opened and cancelled inside one epoch; offer left by a previous owner
  (must NOT count as listed); destination-locked offer (must count); buy offer
  (must not count).
- Certification gate: epoch inside a continuity gap defers and leaves the
  cursor behind it; the following run, with the gap healed, writes the same
  epoch exactly once.
- Idempotence: two full runs produce identical rows.
- A regression test asserting a listed token earns nothing, driven purely from
  archive fixtures rather than a mocked RPC.

## Ops notes

- The 00:40 UTC `lfg-brix-accrue` slot (#415) stays as-is; with no dependency
  on `lfg-market-sweep` there is no reason to move it to 04:00.
- `archive_state.baseline_coverage` on mainnet still names the retired
  distributor `rnqvoyr…`. The next certification run must pass
  `--distributor rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ`.

## Out of scope

Changing what an epoch pays, who is excluded, or the claim flow. This spec
changes only where the answer comes from.
