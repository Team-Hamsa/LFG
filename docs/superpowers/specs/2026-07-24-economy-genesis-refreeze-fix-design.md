# Stop re-freezing genesis — close the burn/shrinkage gap and automate reconcile+audit — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #322

## Problem

The trait-economy conservation model is sound: freeze the collection composition
**once** into a genesis baseline, then record every intentional growth/shrinkage
in the append-only `supply_changes` ledger, so the invariant
`census == genesis + Σ supply_changes` holds forever
(`lfg_core/trait_economy.py::verify_conservation`, folded over
`effective_genesis`). A new mint writes a `+1` row (`nft_listener._apply_possible_growth`)
and the equation stays balanced.

In practice, every mainnet/staging go-live has needed a **manual re-freeze**
(`scripts/freeze_genesis.py` + hand-deleting baked-in supply rows). A re-freeze
redefines "correct" as "whatever we have now" — it makes the audit go green by
destroying its ability to detect a real problem. Each re-freeze was compensating
for an **incomplete/wrong supply ledger**, not a design limit. Because nothing
runs the audit automatically, drift is only found by hand, long after the fact.

The issue (#322) diagnosed three ledger-bug families on staging (2026-07-22).
Two are already fixed in code:

1. **Foreign-issuer mints credited as our growth** (pre-#178) — fixed by the
   `token["issuer"] == config.SWAP_ISSUER_ADDRESS` hard-gate in
   `nft_listener.apply_economy_tx` (line ~279); cleaned with
   `scripts/purge_foreign_supply_changes.py`.
2. **Listener-missed real mints** — fixed by `scripts/reconcile_supply_growth.py`
   (#289) wrapping `lfg_core/supply_reconcile.reconcile_growth`.

The **third is still open and is structural**:

3. **Burns are never recorded as shrinkage.** In `apply_economy_tx` the burn
   branch (`lfg_core/nft_listener.py:263-269`) only calls
   `economy_store.delete_trait_token(nft_id)` and `continue`s — its comment says
   "characters/closets need no economy action on burn", true for the economy's
   own flows (they write their own supply rows) but **not** for a character
   burned out of band (admin burn via `/admin`, legacy pre-blank harvest, old
   paths). A mint records `+1`; an out-of-band burn records nothing. On staging
   this left a uniform **-4 per-slot** drift traced to 8 dressed editions
   (3560, 3562, 3564, 3565, 3566, 3568, 3570, 3571) minted `+1` and later burned
   with no compensating `-1`. This asymmetry will keep producing drift on mainnet.

The fix is: (a) close the burn asymmetry with an idempotent shrinkage recorder,
(b) a one-time/periodic reconcile sweep for historical burns, and (c) automate
reconcile + audit under pm2 cron so drift becomes a real, owned signal instead of
a re-freeze trigger.

## Constraints discovered

- **Genesis is immutable by design.** `effective_genesis(genesis, supply_changes)`
  never mutates the frozen `Genesis`; it folds signed `trait_deltas` (mint `+`,
  burn `-`) and add/removes `edition_bodies`. The whole point of this issue is that
  *nothing in normal operation should ever re-freeze* — the ledger, not a
  re-freeze, absorbs every legitimate change.
- **Blank characters contribute nothing to the census.** `asset_census` skips
  `is_blank(rec)` characters — a harvested (modify-in-place) blank's 9 asset
  values live in the owner's Closet (`closet_assets`), which the census counts
  instead. **Therefore burning a BLANK character must write NO shrinkage row** —
  its assets survive in the Closet and are still conserved. Only a **dressed**
  character's burn destroys on-token assets and needs a `-1`.
- **Ordering:** `scripts/onchain_listener.py::_apply` calls `nft_listener.apply_tx`
  (which flips `is_burned=1` via `nft_index.mark_burned`, preserving
  `attributes_json`) **before** `apply_economy_tx`. So at burn time the burned
  character's traits/body/edition are still readable from the local
  `onchain_nfts` row — the correct source for reconstructing the `-1` deltas
  (`nft_info` returns `None` post-burn, which is why the burn branch routes by
  `nft_id` alone).
- **Membership must be authoritative, never taxon-from-ID.** The XLS-20 taxon is
  scrambled/forgeable; ours-ness is decided by presence in `onchain_nfts` (the
  character index) — trait tokens live in `trait_tokens`, closets in
  `closet_tokens`. A burned `nft_id` with a character row in `onchain_nfts` is our
  character edition.
- **No double-counting with flows that already log their own burn.** The legacy
  flag-24 harvest upgrade (`economy_flow.py:453`) writes its own `-1` (and a
  matching `+1` remint, net-zero, same edition). The listener's out-of-band
  recorder must not add a second `-1` for that same burned token. Idempotency
  must be keyed on the **burned `nft_id`**, and every burn-writing flow must stamp
  the burned `nft_id` on its row.
- **Net-zero-per-slot swap substitution is benign, not drift.** A trait swap
  substitutes one value for another within a slot (`-1 old`, `+1 new`) with a
  net-zero slot total. The automated audit must distinguish this benign pattern
  from a real conservation violation (the existing per-`(slot,value)` drift map
  already exposes this, but the alert wording/classification must call it out).
- **SourceTag / memos:** this feature builds **no new on-ledger transactions** —
  it is pure local-DB accounting over the existing index/ledger. The SourceTag
  `2606160021` + provenance-memo requirements apply to any tx path; none is added
  here. (The burns being *accounted for* already carried their SourceTag/memos
  when they were signed.)

## Design

Three independent seams: (A) the accounting core + store change, (B) the listener
recorder + reconcile sweep, (C) the pm2-cron automation + alerting.

### A. Store: stamp burned `nft_id` on shrinkage rows (idempotency key)

Add a nullable, self-migrating `nft_id` column to `supply_changes`
(`lfg_core/economy_store.py`, `init_economy_schema` ALTER-if-missing, mirroring
the market-store self-migrating columns). Extend
`economy_store.record_supply_change(...)` with an optional `nft_id: str | None = None`
param (back-compatible; existing call sites unchanged) and add:

- `economy_store.supply_change_exists_for_nft(conn, nft_id, kind="burn") -> bool`
  — `SELECT 1 FROM supply_changes WHERE kind=? AND nft_id=? LIMIT 1`.

`read_supply_changes` gains `nft_id` in its returned dicts (ignored by
`effective_genesis`, which only reads `trait_deltas`/`kind`/`edition`).

The legacy flag-24 harvest burn (`economy_flow.py:453`) passes
`nft_id=rec.nft_id` on its `-1` row so the listener recognises it as already
accounted for. (The `+1` remint row may pass the new nft_id or `None`; it is a
mint of a *known* edition so growth is a no-op regardless.)

### B. Listener: idempotent out-of-band burn recorder + reconcile sweep

**Add `nft_index.nft_by_id(conn, nft_id) -> OnchainNft | None`** — `SELECT * FROM
onchain_nfts WHERE nft_id=?` mapped via the existing `_row_to_nft` (returns the
row even when `is_burned=1`, which is exactly what we need post-`mark_burned`).

**New pure helper `trait_economy.burn_shrinkage_deltas(rec) -> dict[str,int] | None`**
— given a character `OnchainNft`, return the `{"slot|value": -1}` deltas + a way
to get body_value/body_class for a **dressed** character, or `None` if the
character `is_blank(rec)` (assets conserved in Closet → no shrinkage) or its
attributes are unreadable (skip, never guess — parallel to `supply_reconcile`).

**Extend `apply_economy_tx`'s burn branch** (after the existing
`delete_trait_token`): when `genesis is not None`, look up
`nft_index.nft_by_id(conn, nft_id)`; if it is our character edition
(`rec.nft_number is not None`, edition in `effective_genesis.edition_bodies`),
not blank, and `not economy_store.supply_change_exists_for_nft(conn, nft_id)`,
record a `burn` `-1` shrinkage row (actor `"listener"`, reason
`f"out-of-band burn {nft_id}"`, `nft_id=nft_id`). Wrapped in the existing
per-`nft_id` try/except so it never breaks index maintenance. This keeps the
`token["issuer"]` gate implicit — membership comes from the local `onchain_nfts`
index, which the issuer gate already governs at write time.

**New sweep `lfg_core/supply_reconcile.reconcile_shrinkage(conn, *, dry_run)`**
(mirror of `reconcile_growth`): scan `onchain_nfts` for **burned** character
editions of ours whose edition is in the effective genesis, that are **dressed**
(not blank), and have **no** `burn` supply_change for their `nft_id`; write the
`-1` from the stored attributes. Idempotent; skips + reports unreadable rows.
Backing CLI `scripts/reconcile_supply_shrinkage.py --network <net> [--apply]`
(dry-run default, exactly like `reconcile_supply_growth.py`).

### C. Automation: pm2-cron reconcile + audit + alert

Register two cron pm2 processes alongside `lfg-snapshot` in
`ecosystem.prod.config.js` / `ecosystem.staging.config.js` (`autorestart:false`,
`cron_restart`), running **after** the nightly snapshot:

1. `lfg-economy-reconcile` — runs `reconcile_supply_growth.py --apply` **and**
   `reconcile_supply_shrinkage.py --apply` (a tiny wrapper script,
   `scripts/economy_nightly_reconcile.py --network <net>`, so both sweeps run in
   one process and share one connection/commit).
2. `lfg-economy-audit` — runs `scripts/audit_trait_economy.py --network <net>`
   (already exits 1 on drift). On non-zero exit, post the report summary +
   conservation drift table to a Discord webhook if `ECONOMY_AUDIT_WEBHOOK_URL`
   is set (new optional env var; posted via `aiohttp`, the existing dep), else
   just log. Wire this by extending `audit_trait_economy.py` with an optional
   `--alert-webhook` (defaulting to `os.environ.get("ECONOMY_AUDIT_WEBHOOK_URL")`)
   that fires only when the run is non-clean. The audit already writes a
   timestamped markdown report to `reports/`.

The audit's drift table is per-`(slot, value)`; the alert body labels a
**net-zero-per-slot** pattern (Σ over a slot's values == 0) as "benign swap
substitution — review, likely not a leak" vs a **non-zero slot total** as "real
conservation drift — investigate," so the human owner isn't paged for benign
swap noise.

### Docs

Update `docs/runbooks/mainnet-mvp-launch.md` and the economy sections of
`CLAUDE.md`: **freeze genesis exactly once per network**; re-freezing is
**break-glass only**, never a routine ops step; drift is a bug to diagnose (run
the reconcile sweeps, then the audit), not a state to normalize with a re-freeze.

## Out of scope

- Reworking `freeze_genesis.py` itself or the genesis schema — genesis stays a
  one-time frozen baseline; we only stop *re*-freezing.
- Trait-token (Extract/Deposit/Shop) supply accounting — already correct and
  supply-neutral (Extract/Deposit) or self-logging (`shop_flow`).
- Any new on-ledger transaction, mint/burn behavior change, or the mainnet
  historical cleanup itself (issue notes mainnet numbers must be reviewed
  before any cleanup — an ops decision, run the dry-run first).
- Trait-swap substitution accounting changes — the net-zero pattern is already
  correct in the ledger; we only classify it in the alert.

## Open questions / decisions for maintainer

1. **Alert channel:** a new `ECONOMY_AUDIT_WEBHOOK_URL` Discord webhook (simplest
   for a cron process that runs outside the bot) vs routing through the running
   bot's `ADMIN_LOG_CHANNEL_ID`? Webhook is proposed; confirm.
2. **Reconcile autonomy:** run the nightly reconcile with `--apply` (self-heal)
   as #322 proposes, or dry-run + alert only and require a human to apply? #322
   asks for self-heal; growth reconcile is already safe/idempotent and shrinkage
   mirrors it, so `--apply` is proposed — confirm you want it unattended on
   mainnet.
3. **Historical mainnet cleanup:** run `reconcile_supply_shrinkage.py` dry-run on
   mainnet first and review the editions before `--apply`? (Issue explicitly
   flags mainnet as untouched.) Proposed: yes, dry-run + human sign-off once,
   then let the nightly job maintain it.
4. **Blank-then-burned confirmation:** confirm the invariant that a harvested
   blank's assets always remain in `closet_assets` after the character is burned
   (i.e. no flow deletes them on character burn) — the design relies on this to
   correctly skip blank burns.
5. **Legacy `-1` rows without `nft_id`:** existing `economy_flow` legacy-harvest
   burn rows predating this change have `nft_id=NULL`. Is there any live legacy
   flag-24 harvest burn on mainnet whose token could be re-seen by the recorder?
   (Legacy harvest also *reminted* the same edition, so it's live again — the
   recorder only fires on a *currently burned* index row, so this is safe, but
   confirm.)

## Testing

**Unit (`lfg_core`, pytest with the env-guard preamble):**
- `trait_economy.burn_shrinkage_deltas`: dressed char → `{slot|value:-1}` + body;
  blank char → `None`; unreadable attrs → `None`.
- `economy_store`: `record_supply_change(..., nft_id=...)` round-trips;
  `supply_change_exists_for_nft` true/false; self-migration adds the column to a
  pre-existing DB with no `nft_id` column.
- `verify_conservation` end-to-end: mint `+1` then out-of-band burn `-1` → drift
  clears; blank-then-burn → no shrinkage needed, still `ok`.

**Integration (listener):**
- Feed a `NFTokenBurn` tx for a dressed character through `apply_economy_tx`
  (after `apply_tx` marks it burned) → exactly one `-1` burn row keyed on nft_id;
  re-run → no second row (idempotent).
- Feed the legacy-harvest sequence (flow wrote `-1` with nft_id) → listener does
  **not** add a second `-1`.
- Blank character burn → no shrinkage row.
- `reconcile_shrinkage` on a DB with 8 dressed burned editions and no burn rows →
  writes exactly 8, dry-run writes nothing, second `--apply` is a no-op.

**Manual smoke:**
- On staging (already reconciled for #322 items 1 & 2): run
  `reconcile_supply_shrinkage.py --network testnet --apply` then
  `audit_trait_economy.py --network testnet` → `Conservation: OK` with no
  re-freeze. Confirm the 8-edition -4/slot drift is gone.
- Trigger `pm2 start` on the two new cron entries; verify one full nightly cycle
  (reconcile → audit) runs and, when clean, posts nothing; force a synthetic
  drift and confirm the webhook alert fires with the benign-vs-real label.
