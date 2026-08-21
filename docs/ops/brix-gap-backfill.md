# BRIX drip gap reimbursement — runbook (#412)

Spec: `docs/superpowers/specs/2026-08-20-brix-gap-reimbursement-design.md`.
Window: 2025-09-15 (day after the last real payout run) → yesterday.

## 0. Prerequisites (once, after this code is deployed)
1. Re-derive the archive so `nft_events` carries `offer_index`/`offer_flags`
   (also repairs the stale `brix_events` — ~14,500 2025 payouts missing):
   `.venv/bin/python scripts/derive_history_events.py --network mainnet --distributor rwr84Q12nwokykgmyaB16gTo8zD85AHCzJ`
2. Confirm the archive is certified and gap-free:
   `sqlite3 history_mainnet.db "select baseline_complete, continuity_gap_reason, datetime(validated_close_time,'unixepoch') from archive_state"`
   → `1 | NULL | <recent>`. If not, run the catch-up
   (`scripts/backfill_history.py --network mainnet --catch-up-from-gap --baseline-provenance "…" --distributor rwr84Q…`).
3. Confirm the nightly job is healthy: `pm2 logs lfg-brix-accrue --lines 20` shows `accrued=` lines with `unknown=0`.

## 1. Rehearse on staging (testnet)
`cd ~/LFG-staging && .venv/bin/python scripts/backfill_brix_gap.py --network testnet --from <d1> --to <d2>` then `--apply`; claim one backfilled balance via the Activity.

## 2. Mainnet dry run
`.venv/bin/python scripts/backfill_brix_gap.py --network mainnet > reports/brix_gap_dryrun.txt`
Review: total vs the ~1.23M NFT-day upper bound, top-wallet concentration,
`DEFERRED` epochs (must be none), `unknown` (must be 0), treasury headroom.

## 3. Apply
`.venv/bin/python scripts/backfill_brix_gap.py --network mainnet --apply | tee reports/brix_gap_apply.txt`
Then `scripts/brix_admin_report.py --network mainnet` and
`scripts/audit_brix_distribution.py --network mainnet` (must PASS).
Re-running `--apply` is a no-op. The nightly cursor is never moved.

## Rollback
Rows are DB-only until claimed. To withdraw an unclaimed backfill:
`DELETE FROM brix_accruals WHERE claim_id IS NULL AND epoch_date BETWEEN '2025-09-15' AND '<to>'`
(never delete rows with a `claim_id` — a claim may be in flight).
