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

## Step 0 — certify the eligibility archive (REQUIRED)

Nothing else in this runbook works until this is done. Eligibility means "this
wallet has never submitted a transaction carrying LFG's SourceTag", and that
question is only answerable from an archive that has proven itself complete.
`sponsored_mint.archive_is_usable` demands an `archive_state` row with
`baseline_complete=1`, a bound coverage document, and a fresh listener
heartbeat. Without it every reservation returns `eligibility_unavailable` and
the campaign admits nobody — inert and harmless, but a campaign that silently
sponsors zero wallets looks identical to one that is switched off.

This is now two steps: a manual one-time baseline (Step 0a) that establishes
the human audit claim, and an automatic re-verification (Step 0b, #340) that
keeps that claim current with zero manual action across deploys and listener
restarts, as long as Step 0a has run at least once per network.

### Step 0a — one-time baseline certification (manual)

Run once per network, and again after any testnet reset (a reset gives the
chain a new genesis, which invalidates every prior certification for that
network — see `genesis_mismatch` below). This is the only command that can
create the archive's first `archive_state` row; nothing downstream, including
the automatic re-verification in Step 0b, can bootstrap one from nothing.

Record the current validated tip first, then certify against it:

```bash
cd /home/hamsa/LFG           # staging: /home/hamsa/LFG-staging
.venv/bin/python scripts/backfill_history.py --network mainnet \
  --complete-audited-baseline \
  --genesis-hash <ledger-32570-hash> \
  --baseline-ledger-min 32570 \
  --baseline-ledger-max <current-validated-tip> \
  --baseline-provenance "<who audited this archive and how>" \
  --distributor <airdrop-distributor-wallet>
```

- `--baseline-ledger-min` **must** be `32570`
  (`history_store.EARLIEST_AVAILABLE_LEDGER`), not `1`: ledgers 1-32569 were
  lost in 2012, no node serves them, and `account_tx` rejects a lower bound
  outright. `--baseline-ledger-max` **must** equal the endpoint's current
  validated tip (the run refuses otherwise). The tip moves every few seconds
  and the run re-reads it at start, so read the tip and launch immediately;
  on `baseline maximum must equal the validated endpoint tip`, re-read and
  retry rather than reusing the stale number.
- `--sources` must be the **full default set** for a certification run — the
  run refuses to certify if any source is narrowed away (#331), and the
  coverage document records the exact source set swept (including `nfts`,
  which has no account address) so `_baseline_coverage_is_bound` can verify
  the sweep was not narrowed. Leave `--sources` at its default. The `nfts`
  sweep is by far the slowest part of the run; that is the cost of an
  attestable baseline, not a corner to cut.
- `--distributor` is **required** for a certification run (though optional for
  a plain backfill): the distributor source can only be swept when an address
  is supplied, so certifying without one would attest a sweep that never
  happened. Use the airdrop distributor wallet (`BRIX_DISTRIBUTOR_ADDRESS`).
- `--baseline-provenance` is a free-text operator attestation. It is evidence
  for a human reader, not a machine check: write who verified completeness.
- Watch for `skipped N entries carrying no explicit validated flag`. If a
  source reports that for every entry it is archiving **nothing**; stop and
  verify the endpoint's response shape before certifying.

A transaction stream carries no replay token, so a listener that restarts
cannot prove it missed nothing in the ledger it was cut off in. Any listener
restart — a deploy, a `pm2 restart`, a websocket error — therefore stamps
`continuity_gap_*` and clears `baseline_complete`, and admission fails closed
until the archive is re-certified. Historically that meant re-running Step 0a
by hand before every campaign; **that manual re-run is now automatic — see
Step 0b, below.**

### Step 0b — automatic re-verification (no action needed)

Every time an operator starts a campaign (`/admin` → Start Sponsored Mint, or
`POST /api/admin/sponsored-mint/start`), the service kicks one background job
per network (`lfg_service/app.py::kick_archive_reverify` →
`lfg_core.archive_reverify.reverify_archive`) that:

1. Re-sweeps `account_tx` for exactly the accounts Step 0a's coverage
   document recorded, from ledger 32570 to the current validated tip —
   nothing new to configure, it replays the same `--sources` Step 0a was
   certified with.
2. Re-certifies through the same `record_archive_baseline` writer Step 0a
   uses, but inherits the original attestation instead of asking for a new
   one: the stored provenance becomes `auto-reverify at <ts> (baseline:
   <original attestation>)`. Repeated re-verifies unwrap and reuse that same
   original claim rather than nesting wrappers, so the human audit claim from
   Step 0a survives indefinitely.
3. Waits (bounded, ~90s) for the listener to restamp its heartbeat, since
   certification always clears it (below).
4. Writes an `archive_reverify` row to `free_mint_audit` recording the actor,
   network, and outcome — win or lose.

This is single-flight per network (a Start while one is already running joins
it rather than launching a second) and is purely additive to the fail-closed
gate: `sponsored_mint.archive_is_usable` is still the sole admission
authority, unchanged from before #340. The job only exists to keep that gate
green without an operator having to remember to re-run Step 0a after every
deploy or restart.

Poll `GET /api/admin/sponsored-mint/status` — its `reverify` block reports
`{state: "idle"|"running"|"ok"|"failed", error, finished_at}` for that
network's most recent job. State is in-memory only: a service restart
forgets a finished job, but the next Start re-kicks one, so that's harmless,
not a data-loss concern.

**Failure reasons** (`reverify.error`). The first six are `reverify_archive`'s
closed, machine-readable set; the last two are wrapper conditions from the
service job itself (`lfg_service/app.py::run_archive_reverify`) around that
call, so together the table is exhaustive for everything `reverify.error` can
hold, not just the deterministic core:

| reason | meaning | operator action |
|---|---|---|
| `baseline_never_certified` | no Step 0a has ever run for this network | run Step 0a |
| `genesis_mismatch` | the live endpoint's chain identity doesn't match the certified baseline | wrong RPC endpoint, or the testnet chain was reset — re-run Step 0a |
| `coverage_unbound` | the stored coverage document is missing or empty | re-run Step 0a |
| `missing_required_sources` | the stored coverage doesn't cover `token_issuer` and `signing` | re-run Step 0a with the required `--sources` |
| `gap_not_covered` | the sweep completed but a continuity gap's bound lies past the reached tip | transient/ops — press Start again |
| `sweep_failed: <exc>` | an `account_tx` page or endpoint-identity request failed mid-sweep | transient/ops (RPC hiccup) — press Start again |
| `listener never restamped the heartbeat — it has no archive identity; set SPONSORED_MINT_*_GENESIS_HASH and restart the listener (not during a live campaign)` | the sweep and re-certification succeeded, but the listener process has no archive genesis identity at all, so it can never heartbeat | set `SPONSORED_MINT_*_GENESIS_HASH` (see the prerequisite below) and restart the listener — never during a live campaign |
| `internal_error` | an unexpected exception escaped the job entirely (the outer catch-all around the sweep/wait/audit sequence) | check the `lfg-activity` service logs for the traceback, then press Start again to retry |

### Prerequisite — set the genesis hash so the listener always has an identity

Set `SPONSORED_MINT_MAINNET_GENESIS_HASH` / `SPONSORED_MINT_TESTNET_GENESIS_HASH`
in each stack's `.env` so the listener has an archive identity at **every**
startup, not only after a certification has already happened once. This is
what makes Step 0a truly one-time per network (plus testnet resets, which
mint a new genesis) instead of something that has to precede every deploy:
with the hash set, a freshly started listener always has an identity to
heartbeat against, so Step 0b's automatic re-verify is always sufficient on
its own.

Without the hash set, a **brand-new** archive (no `archive_state` row yet)
still needs the old manual dance once, because nothing can certify against an
identity the listener doesn't have and Step 0b cannot bootstrap a first
baseline: certify (Step 0a) → restart the listener (it loads the
just-certified identity; the restart stamps a fresh, bounded continuity gap)
→ certify again → verify. Once any identity exists — from the env var, or
from that first certification — every later listener start already has it,
and Step 0b handles everything from there.

Constraints that are still true and still matter:

- **Never restart, promote, or redeploy either stack during a live
  campaign.** Every certification (manual or automatic) clears the
  listener's heartbeat, so a mid-campaign restart forces the archive back to
  fail-closed on top of dropping connections — there is no live-campaign path
  that avoids this, automatic re-verify included.
- **Every certification clears the listener's heartbeat** (`heartbeat_at` and
  `validated_ledger_index` are reset to NULL), and `archive_is_usable` fails
  closed while the heartbeat is absent. The running listener restamps it on
  the next transaction it streams — this is exactly why Step 0b waits (up to
  ~90s) instead of declaring success right after the sweep. If you're
  driving Step 0a by hand and the readiness audit fails right after
  certifying, wait for the listener to process a transaction (check the row:
  `SELECT heartbeat_at FROM archive_state;`) and re-run the audit — do not
  re-certify, which would only clear the heartbeat again.
- Verify with the readiness audit immediately before starting a campaign; a
  green audit is the only proof the archive is currently usable.

**Recovering from a continuity gap — bounded catch-up (#329).** A restart's
gap is bounded (`continuity_gap_after` = the last ledger the stream provably
covered), so recovery does not require re-paging the full range from ledger
32570. Run:

```bash
.venv/bin/python scripts/backfill_history.py --network mainnet \
  --catch-up-from-gap \
  --baseline-provenance "<who ran this catch-up and why>" \
  --distributor <airdrop-distributor-wallet>
```

This pages `account_tx` over only `[continuity_gap_after, current validated
tip]` for the full source set (the same #331 rules apply: no `--sources`
narrowing, `--distributor` required), sweeps `nft_history` for any
`onchain_nfts` token not already cursor-complete, then records a **cumulative**
`[32570, tip]` baseline — sound because the prior certification plus the live
stream already cover everything below the gap, and raw inserts are
INSERT OR IGNORE, so bounded-run ∪ existing-archive equals a full re-page in
coverage. `--genesis-hash` is optional here (the archive's recorded identity is
cross-checked against the endpoint either way). The run refuses (non-zero
exit) when there is no gap, when the gap has no lower bound
(`continuity_gap_after IS NULL` — see "Repairing an archive stuck with an
unbounded gap" below), when the archive was never fully certified, or when the
endpoint tip cannot reach the gap; those cases are full certification's job.
If the gap's upper extent lies above the tip this run certified, the gap
survives, the archive stays fail-closed, and the run exits 1 — re-run against
a fresher tip. This is still an explicit operator command with a provenance
attestation: nothing self-heals, and a restart still fails closed until an
operator runs it.

Note the honest limit: **both** full and bounded certification re-prove a
window at `account_tx` breadth only — not at the firehose breadth the live
listener archives at (see "What the historical baseline can and cannot see"
below). The expensive full re-page never bought extra coverage over the gap
window; the bounded run proves exactly as much.

Step 0b's automatic re-verify (a full-range sweep over the certified
accounts) can also clear a bounded gap as a side effect, but the bounded
catch-up above is the cheap, explicit tool for it. Either way, gap recovery
never starts from scratch: Step 0a is only needed when no certified baseline
exists at all (Step 0b and the catch-up can only extend an existing baseline,
not create one). Never hand-edit `archive_state` to clear the gap flags; they
are the only record that the archive was, at some point, not provably
complete.

### What the historical baseline can and cannot see

The live listener half of the archive is complete — it subscribes to the
whole-network transaction stream and archives every validated
SourceTag-carrying transaction regardless of which accounts it touches. The
**historical** half is not: `account_tx` is affected-account-scoped, so the
certified baseline can only see transactions that touched a swept account
(NFT issuer, BRIX issuer, LFGO token issuer, signing account, distributor)
or an indexed character token (`nft_history` over `onchain_nfts`). Requiring
the full source set closes the operator-shortcut hole; two narrow blind
spots remain inherent to the design:

- **Trait-token offer create/cancel.** A trait-token sell offer's only
  affected account is the offeror, and trait tokens live in `trait_tokens`,
  not `onchain_nfts`, so the `nfts` sweep never visits them. A wallet whose
  sole tagged activity is listing/cancelling an off-platform-acquired trait
  token is invisible to the baseline.
- **`tec`-failed tagged transactions.** Their metadata touches only the
  sender, so they appear in no other account's `account_tx`. (Asymmetry: the
  live listener *does* treat a `tec` tagged transaction as evidence.)

The consequence of a leaked wallet is bounded: `free_mint_claims` has
`UNIQUE (network, wallet)`, so it costs at most one of the campaign's slots
plus one 1-LFGO burn on a wallet that already counted toward the
unique-wallet metric — never funds or a double mint.

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
`continuity_gap_after` is now non-NULL, then re-certify — either Step 0a by
hand, or start a campaign to let Step 0b's automatic re-verify do it.

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

0. Complete **Step 0a** above on staging. The readiness audit in step 1 reports
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

8. On production, complete **Step 0a** against `history_mainnet.db` (the
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
