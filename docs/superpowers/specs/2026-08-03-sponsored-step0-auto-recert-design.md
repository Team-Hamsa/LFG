# Sponsored free mint — Step 0 auto-recertification (design)

Issue: #340
Date: 2026-08-03
**Status:** shipped — PR #341, merged 2026-08-15; extends Step 0 of `docs/ops/sponsored-free-mint.md` · design record

## Problem

Archive certification (Step 0 of the sponsored-mint runbook) is a manual CLI
step that must be re-run after **every** stack restart before a campaign can
admit anyone: any restart clears the listener heartbeat and any schema
migration or continuity gap flips `baseline_complete` to 0, and the fail-closed
`sponsored_mint.archive_is_usable` gate then returns `eligibility_unavailable`
for everybody. The operator has to know the runbook, read the live validated
tip, and launch `scripts/backfill_history.py --complete-audited-baseline …`
within seconds of reading it. In practice this makes "click Start Sponsored
Mint" insufficient to actually start a working campaign.

Almost all of Step 0 is a deterministic computation over the ledger. The one
part that is not — and must stay human — is the **first-ever** baseline
attestation (`--baseline-provenance`): a person vouching that the archive is
complete from ledger 32570. Everything after that is "sweep from the persisted
markers to the current tip and confirm continuity", which a machine can do.

## Design

### Concept split

- **Baseline certification** (one-time per network): unchanged. Manual CLI run
  of `backfill_history.py --complete-audited-baseline` with a human
  `--baseline-provenance` attestation. This is an accountability claim, not a
  computation, and is deliberately not automated.
- **Re-verification** (every subsequent green-light): a new deterministic
  operation. Preconditions: an `archive_state` row for the network already
  exists with a non-empty `baseline_provenance` (i.e. a human baseline was
  certified at least once — with none, re-verify refuses with an actionable
  "run Step 0 baseline certification first" error). It then:
  1. Reads the live validated tip from the configured endpoint and confirms
     the endpoint's genesis hash (ledger 32570) matches the stored
     `archive_state.genesis_hash` — a mismatch is a hard, non-retryable
     failure (wrong endpoint / wrong network).
  2. Runs the existing top-up sweep (the same `account_tx` paging + derivation
     the backfill does) from the persisted per-source markers to that tip.
  3. Certifies via the existing `history_store.record_archive_baseline` with
     `baseline_ledger_min = EARLIEST_AVAILABLE_LEDGER (32570)`,
     `baseline_ledger_max = tip`, and provenance
     `auto-reverify at <utc timestamp> (baseline: <original attestation>)` —
     the original human attestation is always carried forward verbatim, never
     replaced.
  4. If the tip moved past `baseline_ledger_max` between read and certify (the
     existing "max must equal live tip" refusal), it retries the read-sweep-
     certify cycle up to 3 times before failing.
  5. The existing gap-clearing rule in `record_archive_baseline` applies
     unchanged: a `continuity_gap_after` bound only clears when the certified
     sweep reaches past it, so re-verify naturally heals post-migration /
     post-disconnect invalidations (#337/#338) without special-casing.
  6. Waits (bounded, ~90 s) for the listener to restamp `heartbeat_at`
     (certification clears it by design), then runs the same freshness check
     `archive_is_usable` uses. Only then does re-verify report green.

### Trigger: campaign start kicks it; the gate stays the authority

`POST /api/sponsored-mint/start` (`handle_sponsored_mint_start`) behaves as
today — it activates the campaign immediately — and additionally schedules a
background **re-verify job** for the network (in-process asyncio task in
`lfg_service`, same pattern as the settlement/shop sweeps; single-flight per
network, a second start while one is running is a no-op join).

Deliberately **no new campaign states**: the fail-closed `archive_is_usable`
gate already means an active campaign with an unusable archive admits nobody.
Automation fixes the archive instead of modelling its absence — eligibility
unlocks the moment the archive goes green, with no state machine changes and
no behavior change if the job is never run or fails.

Re-verify outcomes are recorded in `free_mint_audit`
(`action="archive_reverify"`, result `ok` / `failed:<reason>`), and the status
endpoint (`handle_sponsored_mint_status`) gains a `reverify` block:
`{state: idle|running|ok|failed, error, finished_at}` so the Discord admin
panel can show *why* eligibility is unavailable instead of a silent inert
campaign.

### Sweep execution: importable, not a subprocess

The top-up sweep + certify logic currently lives in `scripts/backfill_history.py
main()`. The re-verify path needs it callable in-process, so the
sweep-to-tip + certify core is extracted into an importable function (new
module `lfg_core/archive_reverify.py`; the CLI keeps its behavior by calling
the same function for the non-baseline path). No subprocess management, no
CLI-arg plumbing from the service.

### Listener bootstrap prerequisite

The listener only heartbeats if it latched an archive genesis identity at
startup (env `SPONSORED_MINT_<NET>_GENESIS_HASH`, else a previously certified
row). The certify→restart→certify-again cold-start dance exists only when
neither is present at startup. Ops requirement, documented in the runbook as
part of this change: **set the genesis-hash env vars on both stacks**. With
them set, re-verify never needs a restart. If the heartbeat wait times out,
the job fails with an explicit "listener has no archive identity — set
SPONSORED_MINT_*_GENESIS_HASH and restart the listener (not during a live
campaign)" error rather than a generic freshness failure.

## Error handling

- No prior baseline / empty provenance → `failed: baseline_never_certified`
  (points at the manual Step 0 command).
- Endpoint genesis mismatch → `failed: genesis_mismatch` (non-retryable).
- Tip moved on all 3 attempts, sweep RPC failure, or heartbeat wait timeout →
  `failed: <reason>`; campaign stays active-but-inert (fail-closed gate), the
  status endpoint surfaces the reason, and a later `start` (or a manual
  re-run) retries.
- The job never restarts processes, never mutates campaign rows, and writes
  the history DB only through the existing `record_archive_baseline` /
  backfill machinery — a crash mid-sweep leaves resumable markers exactly as a
  Ctrl-C'd backfill does today.

## Testing

- `lfg_core/archive_reverify.py` unit tests: refuses without prior baseline;
  inherits original attestation verbatim; retries on tip-moved then fails;
  genesis mismatch hard-fails; clears a `continuity_gap_after` only when the
  sweep covers past it; heartbeat wait success/timeout paths (fake clock).
- CLI regression: `backfill_history.py --complete-audited-baseline` behavior
  unchanged after the extraction.
- Service tests: start schedules exactly one job (single-flight); status
  exposes the `reverify` block; audit rows written for both outcomes;
  eligibility flips from `eligibility_unavailable` to admitting once the fake
  reverify lands green.

## Out of scope

- Scheduled/periodic re-verification (cron-style). Campaign start is the only
  trigger; YAGNI until a campaign is ever left running across a restart.
- Automating the one-time human baseline attestation.
- Any change to eligibility semantics, campaign states, or the burn pipeline.
