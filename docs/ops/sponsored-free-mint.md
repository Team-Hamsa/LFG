# Sponsored free mint: release, recovery, and rollback

This runbook is for the durable sponsored-mint campaign. The deployable default
is OFF: a deploy or restart must never create an active campaign. Only a Discord
administrator may start or stop one from `/admin`.

Sponsored admission is supported on `testnet` for the isolated staging rehearsal
and on `mainnet` for production. Any other network is rejected before campaign or
claim rows are created.

Before running readiness on either stack, set the approved operator/test-wallet
exclusions exactly in both environment files:

```bash
# /home/hamsa/LFG-staging/.env
SPONSORED_MINT_EXCLUDED_WALLETS=rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ,rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf

# /home/hamsa/LFG/.env
SPONSORED_MINT_EXCLUDED_WALLETS=rHU8nu9zSnCpkL3gShG4aGawHzaRVfmKwQ,rHaMsAjoAN21s1XG5TCAM6ErAefzrggsHf
```

The signing account and token issuer are always excluded in code, but they do
not satisfy this explicit readiness check. A nonempty or arbitrary configured
wallet list also fails unless both approved addresses above are present.

## Step 0 — certify the eligibility archive (REQUIRED, and not one-time)

Nothing else in this runbook works until this is done. Eligibility means "this
wallet has never submitted a transaction carrying LFG's SourceTag", and that
question is only answerable from an archive that has proven itself complete.
`sponsored_mint.archive_is_usable` demands an `archive_state` row with
`baseline_complete=1`, a bound coverage document, and a fresh listener
heartbeat. **The only writer of that row is the command below.** Without it
every reservation returns `eligibility_unavailable` and the campaign admits
nobody — inert and harmless, but a campaign that silently sponsors zero
wallets looks identical to one that is switched off.

Record the current validated tip first, then certify against it:

```bash
cd /home/hamsa/LFG           # staging: /home/hamsa/LFG-staging
.venv/bin/python scripts/backfill_history.py --network mainnet \
  --complete-audited-baseline \
  --genesis-hash <ledger-32570-hash> \
  --baseline-ledger-min 32570 \
  --baseline-ledger-max <current-validated-tip> \
  --baseline-provenance "<who audited this archive and how>"
```

- `--baseline-ledger-min` **must** be `32570`
  (`history_store.EARLIEST_AVAILABLE_LEDGER`), not `1`: ledgers 1-32569 were
  lost in 2012, no node serves them, and `account_tx` rejects a lower bound
  outright. `--baseline-ledger-max` **must** equal the endpoint's current
  validated tip (the run refuses otherwise). The tip moves every few seconds
  and the run re-reads it at start, so read the tip and launch immediately;
  on `baseline maximum must equal the validated endpoint tip`, re-read and
  retry rather than reusing the stale number.
- `--sources` must include `token_issuer` and `signing`. Both are in the
  default set — do not narrow `--sources` for a certification run, or the
  coverage document will attest less than the archive is treated as proving.
- `--baseline-provenance` is a free-text operator attestation. It is evidence
  for a human reader, not a machine check: write who verified completeness.
- Watch for `skipped N entries carrying no explicit validated flag`. If a
  source reports that for every entry it is archiving **nothing**; stop and
  verify the endpoint's response shape before certifying.

**This is not one-time.** A transaction stream carries no replay token, so a
listener that restarts cannot prove it missed nothing in the ledger it was
cut off in. Any listener restart — a deploy, a `pm2 restart`, a websocket
error — therefore stamps `continuity_gap_*` and clears `baseline_complete`,
and admission fails closed until Step 0 is re-run. Consequences for planning:

- Certify **after** the deploy that will serve the campaign, not before —
  and **after the listener is back up**, in that order. The listener reads
  the certified archive identity **once at startup** and latches it for the
  process lifetime: a listener started against an uncertified archive logs
  `no certified archive genesis identity; SourceTag eligibility archiving is
  DISABLED` and never stamps a heartbeat, so the audit's archive and
  freshness checks fail no matter how long it runs. Certifying first and
  restarting the listener afterwards does not work either — the restart
  stamps a fresh gap that clears `baseline_complete` again. The working
  order is: deploy → let the listener subscribe → certify → audit.
- Do not promote, restart, or redeploy either stack during a live campaign.
- Verify with the readiness audit immediately before starting a campaign; a
  green audit is the only proof the archive is currently usable.

Recovering from a continuity gap is exactly Step 0 again — re-run the
certification. Never hand-edit `archive_state` to clear the gap flags; they
are the only record that the archive was, at some point, not provably
complete.

### Repairing an archive stuck with an unbounded gap

A gap clears only when a certification sweep provably reaches its upper
extent, so a gap stored with **no bounds at all** cannot be cleared by
re-certifying — the symptom is a certification run that reports success while
the audit still says `archive provenance incomplete, mismatched, or stale`
and `baseline_complete` stays `0`. Check with:

```bash
sqlite3 -readonly "$HISTORY_DB" \
  "SELECT baseline_complete, continuity_gap_at, continuity_gap_after,
          continuity_gap_before, continuity_gap_reason FROM archive_state;"
```

Versions before the fix for #337 could record that state after a listener
disconnect that followed a certification. Re-record the gap once — the writer
now backfills a bound from the row's own certified tip — then certify again:

```bash
cd /home/hamsa/LFG           # staging: /home/hamsa/LFG-staging
.venv/bin/python - <<'PY'
from lfg_core import history_store
conn = history_store.init_history_db(history_store.history_db_path("mainnet"))
history_store.invalidate_archive_continuity(
    conn, network="mainnet", reason="rebound unbounded gap (#337)"
)
PY
```

This preserves the gap — it does not erase the record that continuity was
lost — it only gives it the bound the sweep can be measured against. Confirm
`continuity_gap_after` is now non-NULL, then re-run Step 0.

## Safety rules

- Never re-mint a `minting`, `minted`, `offered`, `accepted`, or
  `failed_terminal` claim. A `minting` row without corroborated transaction and
  NFT evidence is uncertain and stays held for manual reconciliation.
- Startup releases NOTHING. A reversible `reserved` row is a durable promise to
  that wallet, so it is never reclaimed on a timer: the wallet's next mint
  request rebinds the existing claim to the new session, and an explicit cancel
  releases it. `reservation_expires_at` records the campaign's end, not a
  reservation TTL. The practical consequence is that a `reserved` row abandoned
  mid-session holds one of the 100 slots for the rest of the campaign; if that
  matters during a live event, have the wallet retry (which rebinds) rather
  than editing the row.
- Never delete, truncate, or recreate `free_mint_claims` or
  `free_mint_burns`. A code rollback leaves both tables in place.
- Stopping a campaign closes only new sponsored admission. Paid minting remains
  live, admitted/consumed claims remain consumed, offer recovery remains
  available, and the sponsored burn worker continues until debt is resolved.
- A readiness `FAIL` is a stop signal. Do not activate or promote around it.
- Sponsored admission remains disabled until startup recovery completes
  successfully. A recovery failure leaves paid minting live and creates no new
  sponsored claim; `/api/admin/sponsored-mint/status` reports
  `recovery_ready: false` until a successful service restart.

## Release rehearsal

0. Complete **Step 0** above on staging. The readiness audit in step 1 reports
   the archive check as FAIL until you do, and the `free_mint_*` tables do not
   exist until the service has started once on this code (the audit opens the
   database read-only and never creates them) — so deploy, let the service
   start, certify, then audit.

1. Run the full suite and read-only readiness audit on staging:

   ```bash
   cd /home/hamsa/LFG-staging
   .venv/bin/python -m pytest -q
   .venv/bin/python scripts/audit_sponsored_mint_readiness.py \
     --network testnet \
     --app-db /home/hamsa/LFG-staging/lfg_nfts_testnet.db \
     --history-db /home/hamsa/LFG-staging/history_testnet.db
   ```

   The staging environment must have `XRPL_NETWORK=testnet`; production must
   have `XRPL_NETWORK=mainnet`. The audit rejects a `--network` mismatch before
   any configuration-bound balance RPC.

   The audit must show `PASS` for schema, inactive campaign, archive,
   listener freshness, unique count, exclusions, signing-wallet LFGO balance,
   zero burn debt, and zero backend-incomplete claims. `--json` emits the same
   checks for captured evidence. It does not initialize or migrate a database,
   change campaign state, or submit a transaction.

2. Start the testnet campaign from Discord `/admin` using **Start sponsored
   mint**. The persisted safety limits stay identical to production: 60 minutes
   and 100 slots. Make the rehearsal short operationally by using **Stop
   sponsored mint** immediately after steps 3–5; there is no duration or cap
   override.

3. With the testnet campaign active, exercise both paths:

   - use a wallet already present in `history_testnet.db` with LFG's SourceTag
     and confirm it receives the unchanged paid flow;
   - use an unseen, non-excluded wallet and confirm it receives the sponsored
     flow with no XRP/LFGO payment request.

4. Complete the unseen-wallet flow and capture evidence for all of the
   following:

   - one validated NFT mint and one destination-locked zero-price offer;
   - an acceptance payload carrying LFG's SourceTag;
   - one validated acceptance by the intended wallet;
   - the authoritative unique tagged-wallet count increments once;
   - exactly one `free_mint_burns` row reaches `burned` (or the documented
     `self_issuer_noop` fulfillment on a self-issued test setup).

5. Rehearse restart recovery with one fresh `reserved` claim and one pending
   burn. Record the rows before restart, then restart the service:

   ```bash
   cd /home/hamsa/LFG-staging
   pm2 restart stg-activity --update-env
   pm2 logs stg-activity --lines 300 --nostream
   ```

   Verify `/api/admin/sponsored-mint/status` reports
   `recovery_ready: true` and the log contains `sponsored startup recovery
   ready`. Then verify the fresh reversible reservation was not silently
   consumed or reminted, any `minting` claim remains held unless its persisted
   evidence was corroborated, the headroom overlay still counts irreversible work, and the
   existing burn worker reclaims the pending/expired-lease obligation. A
   `minted` claim with no offer must reconcile a live locked offer before
   creating one. Strict reconciliation accepts only protocol-shaped amount and
   destination fields; a malformed or non-authoritative response records that
   claim's error and never creates a duplicate. Recovery continues across the
   remaining claims, then raises one aggregate failure if any claim failed, so
   `recovery_ready` remains false. Resumable paid bulk jobs are reattached before
   that aggregate error is propagated. The user's existing pending-offers tray then
   exposes each successfully reconciled or newly created locked offer.

6. Use `/admin` **Stop sponsored mint**, then rerun the readiness audit from
   step 1. It must report the testnet campaign inactive and all controlled
   claims/burns complete. Merge to `main`, let the staging deployer update
   `/home/hamsa/LFG-staging`, and rerun step 1 against the deployed files and
   databases. Also confirm:

   ```bash
   pm2 status
   pm2 logs stg-deployer --lines 200 --nostream
   pm2 logs stg-activity --lines 300 --nostream
   ```

7. Promote only after the staged evidence is green:

   ```bash
   cd /home/hamsa/LFG-staging
   scripts/promote.sh
   ```

   Review the printed `origin/deploy..origin/main` commits before answering the
   confirmation prompt. Then watch `pm2 logs lfg-deployer --lines 200
   --nostream` on production.

8. On production, complete **Step 0** against `history_mainnet.db` (the
   promote in step 7 restarted the stack, so any prior certification is
   already invalidated), then verify the feature is OFF using both the audit
   and Discord `/admin` **Refresh**:

   ```bash
   cd /home/hamsa/LFG
   .venv/bin/python scripts/audit_sponsored_mint_readiness.py \
     --network mainnet \
     --app-db /home/hamsa/LFG/lfg_nfts.db \
     --history-db /home/hamsa/LFG/history_mainnet.db
   ```

   Only after every check is green, use `/admin` **Start sponsored mint** for
   one controlled wallet, complete mint → locked offer → tagged acceptance →
   unique increment → one burn, then use **Stop sponsored mint**.

9. For operational rollback, immediately use Discord `/admin` **Stop
   sponsored mint**. This must not stop the service: paid mint stays live and
   debt/offer recovery continues. If a code rollback is also required:

   ```bash
   cd /home/hamsa/LFG-staging
   git fetch origin --prune
   git push origin <known-good-sha>:deploy --force-with-lease

   cd /home/hamsa/LFG
   .venv/bin/python scripts/deployer.py prod --once --force-reset
   ```

   Replace `<known-good-sha>` with the reviewed deploy ancestor. Code rollback
   never authorizes `DROP TABLE`, `DELETE FROM`, file removal, or restoration of
   an older database over the current `free_mint_claims` and
   `free_mint_burns` tables.

## Read-only database checks

Set the database path to the stack being inspected:

```bash
APP_DB=/home/hamsa/LFG-staging/lfg_nfts_testnet.db
NETWORK=testnet
# Production: APP_DB=/home/hamsa/LFG/lfg_nfts.db NETWORK=mainnet
```

Campaign and claim state:

```bash
sqlite3 -readonly "$APP_DB" "
  SELECT id, network, status, started_at, enabled_until, stopped_at, cap
  FROM free_mint_campaigns
  WHERE network = '$NETWORK'
  ORDER BY started_at DESC LIMIT 5;

  SELECT id, wallet, session_id, status, reservation_expires_at,
         mint_tx_hash, nft_id, offer_id, accept_tx_hash, last_error, updated_at
  FROM free_mint_claims
  WHERE network = '$NETWORK'
  ORDER BY created_at, id;
"
```

Burn debt and leases:

```bash
sqlite3 -readonly "$APP_DB" "
  SELECT b.id, b.claim_id, c.wallet, b.status, b.amount, b.memo_id,
         b.tx_hash, b.signed_tx_hash, b.attempt_count, b.next_attempt_at,
         b.lease_until, b.last_error, b.fulfillment
  FROM free_mint_burns AS b
  JOIN free_mint_claims AS c ON c.id = b.claim_id
  WHERE c.network = '$NETWORK'
  ORDER BY b.created_at, b.id;

  SELECT b.status, COUNT(*) AS obligations,
         COALESCE(SUM(CAST(b.amount AS REAL)), 0) AS lfgo
  FROM free_mint_burns AS b
  JOIN free_mint_claims AS c ON c.id = b.claim_id
  WHERE c.network = '$NETWORK'
  GROUP BY b.status ORDER BY b.status;
"
```

Archive freshness and authoritative unique count:

```bash
HISTORY_DB=/home/hamsa/LFG-staging/history_testnet.db
# Production: HISTORY_DB=/home/hamsa/LFG/history_mainnet.db
SOURCE_TAG=$(.venv/bin/python -c 'from lfg_core.config import SOURCE_TAG; print(SOURCE_TAG)')

sqlite3 -readonly "$HISTORY_DB" "
  SELECT MAX(ledger_index) AS latest_ledger,
         datetime(MAX(close_time), 'unixepoch') AS latest_utc
  FROM xrpl_txs;

  SELECT COUNT(DISTINCT account) AS tagged_wallets
  FROM xrpl_txs
  WHERE source_tag = $SOURCE_TAG
    AND account IS NOT NULL AND trim(account) != '';
"
```

The raw count above is diagnostic only; the readiness audit is authoritative
because it removes the configured signing, issuer, treasury, operator, and test
wallet exclusions.

## Logs and expected patterns

Use the stack's service process (`stg-activity` or `lfg-activity`):

```bash
pm2 logs stg-activity --lines 500 --nostream | \
  rg "sponsored startup recovery|bulk resume sweep failed|sponsored offer recovered|sponsored offer recovery failed|sponsored burn worker pass failed"
```

- `sponsored startup recovery: held_minting=N missing_offers=N burn_debt=N`
  appears before the API binds after a restart and is scoped to the configured
  network.
- `sponsored startup recovery ready` confirms sponsored admission was enabled.
  `bulk resume sweep failed at startup; sponsored admission remains disabled`
  means the service continues with paid minting only; do not start or continue
  the campaign until the recovery fault is fixed and a restart reports ready.
- `sponsored offer recovered` identifies the claim/NFT/offer/wallet repaired
  through the pending-offers surface.
- `sponsored offer recovery failed` identifies each failed claim after its
  `last_error` is recorded. Any such failure is aggregated after the other
  claims are processed, keeps startup recovery not ready, and requires
  investigation before restarting. `sponsored burn worker pass failed` also
  requires investigation; inspect the durable rows before restarting again.
- Campaign starts/stops are authoritative in `free_mint_audit`; Discord's
  admin-log message is supporting evidence, not the source of truth.

Do not infer success from an absent error line. Confirm claim, offer,
acceptance, unique-count, and burn state from the read-only queries and audit.
