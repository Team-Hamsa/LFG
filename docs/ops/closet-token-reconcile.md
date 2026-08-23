# Closet token ownership reconcile — runbook (#383)

Repairs `closet_tokens` rows that violate the Closet ownership invariant:

* **one Closet NFToken, one owner** — a Closet is soulbound, so it cannot be
  in two wallets;
* **that owner is never a project signing account** — the project does not
  play the dress-up game.

## 0. What produced these rows, and why you probably will not find any

A freshly minted Closet sits in the **issuer's** wallet until the user accepts
the offer. `nft_listener._apply_closet` keyed its `closet_tokens` row off the
token's on-ledger owner-of-record, so when it applied the streamed
`NFTokenMint` it wrote a second row under the issuer — pointing at the Closet
the real user's row already pointed at. One such row was observed on mainnet
(2026-08-17 14:35:57 UTC, ~37 s after `ensure_closet` wrote the real user's).
`closet_tokens` is keyed on `owner`, so every subsequent Closet mint overwrote
that same row rather than adding another — which is why there was only ever one.

Three things changed with #383:

1. **The write path is fixed.** `_apply_closet` now skips an issuer-held Closet
   and scrubs any stale project-account row, matching what
   `backfill_economy._reconcile_closet` has done since #190.
2. **The store refuses it.** `economy_store.set_closet_token` /
   `set_closet_contents` raise `ClosetOwnerError` for a project account.
3. **The database refuses it.** A `UNIQUE` index on `closet_tokens(nft_id)` is
   created on schema init. A database that *already* carries a duplicate cannot
   take the index — it is skipped with a `WARNING` naming this runbook, and
   created by step 4 below once the duplicate is gone.

`scripts/backfill_economy.py` also scrubs project-account rows as a side effect,
so **any recent backfill run will already have cleaned this up**. On mainnet a
run at 2026-08-23 00:58 UTC did exactly that. Expect step 3 to report *none* —
that is a pass, not a failed detection.

## 1. Detect

Nightly, via `lfg-economy-audit`:

```bash
.venv/bin/python scripts/audit_trait_economy.py --network mainnet
# ... Closet ownership: OK | ANOMALIES
```

A non-clean run exits 1 and writes `reports/trait-economy-audit-<net>-<ts>.md`
with a `## Closet ownership anomalies (#383)` section. Note
`ECONOMY_AUDIT_WEBHOOK_URL` is unset in prod today, so a non-clean run shows up
in the `lfg-economy-audit` pm2 log, not in Discord.

Or check by hand, without running the audit:

```bash
sqlite3 onchain_mainnet.db \
  "SELECT owner, nft_id, status FROM closet_tokens WHERE owner = 'rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ';
   SELECT nft_id, COUNT(*), GROUP_CONCAT(owner) FROM closet_tokens
     GROUP BY nft_id HAVING COUNT(*) > 1;"
```

## 2. Rehearse on staging (testnet)

```bash
cd ~/LFG-staging
.venv/bin/python scripts/reconcile_closet_tokens.py --network testnet
.venv/bin/python scripts/reconcile_closet_tokens.py --network testnet --apply
```

## 3. Mainnet dry run

Record the conservation numbers **before** the repair — deleting a
project-account row also deletes that "owner"'s loose `closet_assets` /
`closet_bodies`, which `trait_economy.asset_census` counts verbatim, so the
audit's drift figures can legitimately move:

```bash
mkdir -p reports
.venv/bin/python scripts/audit_trait_economy.py --network mainnet | tee reports/closet-reconcile-before.txt
.venv/bin/python scripts/reconcile_closet_tokens.py --network mainnet
```

The dry run opens the database **read-only** and writes nothing. Read its
output in full:

* *"Closets keyed to a project account: none"* → nothing to do, stop here.
* rows listed under *"would delete"* → proceed to step 4.
* anything under *"Duplicate Closet NFTokens between USER owners"* → **stop**.
  Two user wallets claiming one soulbound token cannot be resolved locally
  (Closet tokens never enter `onchain_nfts`). Ask clio which is real —
  `nft_info` is a **clio-only** method, so use `XRPL_CLIO_WS_URL`, not the
  plain rippled WS, which answers `unknownCmd` and would read as "not owned" —
  then correct the losing row by hand.

## 4. Apply

Back up first; `onchain_*.db` is derived and rebuildable, but a backup makes
the rollback instant:

```bash
sqlite3 onchain_mainnet.db ".backup onchain_mainnet.pre-closet-reconcile.db"
.venv/bin/python scripts/reconcile_closet_tokens.py --network mainnet --apply
.venv/bin/python scripts/audit_trait_economy.py --network mainnet | tee reports/closet-reconcile-after.txt
```

`--apply` deletes the project-account rows, then creates the
`idx_closet_tokens_nft_id` unique index. Exit 0 = repaired and clean; exit 1 =
unresolved user↔user duplicates remain (see step 3); exit 2 = refused before
touching anything (network mismatch, missing DB).

Diff the two audit outputs and note any conservation movement in the ops log —
it should be exactly the loose assets that were sitting under the project
account.

## Rollback

```bash
pm2 stop lfg-index-mainnet
cp onchain_mainnet.pre-closet-reconcile.db onchain_mainnet.db
pm2 start lfg-index-mainnet
```

`onchain_mainnet.db` is a derived index, so the real rollback of last resort is
`scripts/backfill_economy.py --network mainnet`, which rebuilds the Closet
tables from on-ledger state — and skips/scrubs project-account rows on its own.

## Notes

* The `--network` flag must match the ambient `XRPL_NETWORK`; the script exits
  2 otherwise, because the project-account set is frozen from config at import
  and would otherwise be compared against the wrong chain's issuer.
* This is a manual, dry-run-first repair. It is deliberately **not** wired into
  the unattended `lfg-economy-reconcile` nightly — a job that deletes rows does
  not belong there.
