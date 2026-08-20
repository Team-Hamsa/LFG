# BRIX drip gap reimbursement (#412)

**Status:** approved design, not yet implemented
**Date:** 2026-08-20
**Issue:** [#412](https://github.com/Team-Hamsa/LFG/issues/412)
**Depends on:** `2026-08-20-epoch-accurate-accrual-design.md` (#411 option 2) —
this spec is a second consumer of the `epoch_state` foundation defined there.

## Corrected premise

The issue states payouts stopped on **2025-04-16**, giving a 491-day gap and a
~1.41M–2.16M BRIX bill. The ledger says otherwise.

`history_mainnet.db` holds distributor (`rnqvoyrWAP95mqssc9yBu6oBeayQUbrteu`)
BRIX Payments — issuer `rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px` — continuing well
past that date:

| Month | Archived distributor Payments | Derived `brix_events` |
|---|---|---|
| 2025-03 | 3,682 | 3,682 |
| 2025-04 | 2,816 | 932 |
| 2025-05 | 3,825 | 0 |
| 2025-06 | 3,806 | 0 |
| 2025-07 | 3,949 | 0 |
| 2025-08 | 3,839 | 0 |
| 2025-09 | 1,765 (through the 14th) | 0 |

The last real payout run is **2025-09-14** (105 recipients; the preceding days
run 127–128, the usual shape). What stops on 2025-04-16 is the **derived**
`brix_events` table, not the payouts.

Two corrections follow:

1. **The gap is 2025-09-15 → yesterday, ~340 days**, not 491.
2. **`brix_events` is stale on mainnet**, missing roughly 14,500 real payouts.
   Every query built on it is wrong today — including the `brix_earned` and
   `brix_rich` leaderboards, and any "what have we already paid" check used to
   size this reimbursement.

### On the rate question

The issue framed "current rules (~2.16M)" and "old program shape (~1.41M)" as
alternative rates. They are not alternative rules: the old program was also
1 BRIX per unlisted NFT per day. The spread is population — ~2,877 NFTs earning
per day in early 2025 versus ~4,395 unlisted today, after ~1,528 mints. An
honest historical reconstruction, in which an NFT cannot earn before its own
mint, produces something close to the lower figure on its own.

A naive per-day live-token walk over `nft_events` gives **1,234,717 NFT-days**
across the 340-day window. That is an upper bound: it does not yet subtract
listed tokens or system-held inventory. The real figure comes from the dry run.

## Decisions taken

- **Rate:** current rules — 1 BRIX per unlisted live NFT per epoch. No second
  historical rule, no fractional multiplier.
- **Eligibility:** strict historical. Each NFT earns for exactly the epochs it
  was live, unlisted and held by a non-system wallet, credited to whoever held
  it at each epoch's close.
- **Delivery:** ordinary `brix_accruals` rows, claimed through the existing
  `POST /api/brix/claim` flow. No new payout path, no forced payments,
  unclaimed backpay never leaves the treasury.

Strict historical is also what closes the exploit the issue identified: a
`accrue_brix.py --date` replay would credit *today's* owner for days they did
not hold, making a floor NFT bought now worth ~340 BRIX and the collection-wide
arbitrage worth millions against the AMM. Crediting the holder-of-record per
epoch removes the attack rather than mitigating it.

## Design

`scripts/backfill_brix_gap.py` — dry-run by default, `--apply` to write, in the
posture of `reconcile_supply_growth.py` / `reconcile_supply_shrinkage.py`.

**Window.** `--from` defaults to `2025-09-15` (the day after the last real
payout run), `--to` defaults to yesterday. Both overridable for rehearsal on
testnet.

**Per epoch**, identical to the nightly path once #411 option 2 lands:

```
state_at_epoch(hconn, oconn, epoch)   # shared foundation
  -> evaluate_accruals(...)           # existing pure evaluator
  -> INSERT OR IGNORE brix_accruals   # existing PK, existing claim flow
```

Everything below falls out of that rather than needing its own rule:

- an NFT earns only from its mint epoch onward;
- an NFT burned mid-window stops earning at its burn;
- listed tokens earn nothing, per epoch, using that epoch's listing state;
- system and issuer wallets earn nothing, including the durably-excluded
  retired distributor (PR #417);
- re-running is a no-op, and a partial run resumes.

**The cursor is never moved.** `brix_meta.last_accrued_epoch` is left untouched:
this script writes historical rows only, and must not be able to make the
nightly job skip forward.

**Certification.** The same gate as the nightly path. Any epoch that fails it
is reported as uncertified and left unwritten rather than paid on an
unverifiable replay.

**Report** (printed on dry run, and again on `--apply`):

- total BRIX, distinct wallets, distinct NFTs;
- per-epoch series, so an anomalous day is visible;
- top-N wallets by amount, for a concentration read before committing;
- count and list of epochs that failed certification;
- comparison against the treasury balance in
  `rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ` (35.2M BRIX) and against the outstanding
  liability `brix_admin_report.py` already computes.

## Ops prerequisites

Both must complete **before the dry-run numbers are trusted**:

1. `derive_history_events.py --network mainnet` — rebuild `brix_events` from
   the raw archive so the 2025-04-16 → 2025-09-14 payouts are represented.
   Fixes the leaderboards as a side effect.
2. A certification run naming the current distributor
   (`--distributor rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ`); `baseline_coverage`
   still names the retired one.

Then: rehearse on testnet, dry-run on mainnet, review the report, `--apply`.

## Testing

- Reconstruction fixtures: an NFT minted mid-window earns only from its mint;
  a token transferred mid-window splits credit between the two holders at the
  right epoch boundary; a token listed for part of the window earns only the
  unlisted epochs; a burned token stops earning.
- The exploit regression: a token whose *current* holder acquired it after the
  window must credit that holder nothing for pre-purchase epochs.
- Idempotence: two `--apply` runs leave identical rows and identical totals.
- Cursor safety: an `--apply` run does not change
  `brix_meta.last_accrued_epoch`.
- Claim integration: a backfilled accrual is claimable through the existing
  flow and binds under the one-open-claim index like any other.

## Out of scope

Per-wallet caps, vesting, fractional rates, and any separate reimbursement
ledger. If the dry-run total reads too hot against ~1.29M circulating BRIX,
that is a decision to take with the real number in hand — not a knob to build
speculatively now.
