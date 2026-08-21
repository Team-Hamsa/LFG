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
3. **Derived-completeness pre-check (MANDATORY before `--apply`).** The replay
   reads `nft_events`, which is *derived* from `xrpl_txs` — a successful
   ledger transaction with no derived row is invisible to certification and
   silently misattributes ownership (on mainnet 2026-08-20 this was ~380
   accepts, moving 197 of 4777 live tokens to the wrong wallet). This query
   MUST return `0`:
   ```sql
   SELECT COUNT(*) FROM xrpl_txs t
   WHERE t.tx_type IN ('NFTokenMint','NFTokenBurn','NFTokenAcceptOffer',
                       'NFTokenCreateOffer','NFTokenCancelOffer','NFTokenModify')
     AND json_extract(t.raw_json,'$.meta.TransactionResult') = 'tesSUCCESS'
     AND NOT EXISTS (SELECT 1 FROM nft_events e WHERE e.tx_hash = t.tx_hash);
   ```
   A non-zero count means **rederive first** (step 1) — do not apply. The
   script's own owner-drift check (it compares the replay's current owner —
   advanced to today's archived state, not to `--to` — against `onchain_nfts`
   and REFUSES `--apply`, exit 2, writing nothing) is
   the backstop, not the primary control.
4. Confirm the nightly job is healthy: `pm2 logs lfg-brix-accrue --lines 20` shows `accrued=` lines with `unknown=0`.

## 0b. Expect the first post-deploy nightly to pay nothing
**The very first nightly run after deploy, before the rederive, DEFERS (or
accrues ≈nothing) — this is expected, not a bug.** A stale derived table makes
listing state unknown for most tokens, and the mass-unknown guard
(`brix_drip.UNKNOWN_DEFER_FRACTION`, 10%) stops the job rather than let it
advance the cursor over a zero-pay day. Likewise, **after every listener
restart the nightly defers** until #402's auto catch-up clears the
`continuity_gap_*` columns — and a gap present during a backfill makes EVERY
epoch report `DEFERRED`, so heal the archive first. Re-run
`derive_history_events.py`, then `backfill_brix_gap.py` reimburses whatever
those epochs missed.

## 1. Rehearse on staging (testnet)
`cd ~/LFG-staging && .venv/bin/python scripts/backfill_brix_gap.py --network testnet --from <d1> --to <d2>` then `--apply`; claim one backfilled balance via the Activity.

## 2. Mainnet dry run
`.venv/bin/python scripts/backfill_brix_gap.py --network mainnet > reports/brix_gap_dryrun.txt`
Review: total vs the ~1.23M NFT-day upper bound, top-wallet concentration,
`DEFERRED` epochs (must be none), `unknown` (must be 0 — `--apply` refuses
otherwise), the owner-drift warning (must be absent — `--apply` refuses
otherwise), `ineligible` (Closet/trait tokens correctly excluded), and
treasury headroom.

## 3. Apply
`.venv/bin/python scripts/backfill_brix_gap.py --network mainnet --apply | tee reports/brix_gap_apply.txt`
Then `scripts/brix_admin_report.py --network mainnet` and
`scripts/audit_brix_distribution.py --network mainnet` (must PASS).
Re-running `--apply` is a no-op. The nightly cursor is never moved.

## Rollback
Rows are DB-only until claimed. To withdraw an unclaimed backfill:
`DELETE FROM brix_accruals WHERE claim_id IS NULL AND epoch_date BETWEEN '2025-09-15' AND '<to>'`
(never delete rows with a `claim_id` — a claim may be in flight).
