# Fix misspelled "Iridescent Skeleton" Body value on 4 mainnet tokens — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #301

## Problem

`scripts/audit_trait_files.py --network mainnet` (2026-07-21, post-WebM cutover)
flags the Body value **`Iridescent Skeleton`** (single-r) as "absent everywhere
(needs art)". The canonical spelling is **`Irridescent Skeleton`** (double-r) —
prod's layer tree only ever carried the double-r art; the single-r spelling
existed as a stray staging-only GIF. Verified on disk today: `layers/skeleton/
Body/` contains exactly `Irridescent Skeleton.webm` and no single-r file.

Because `lfg_core/swap_compose.resolve_layer` / `missing_layers` fail-closed on
any Body value that does not resolve to an art file (own dir → `shared/` →
matrix-permitted foreign dir → ape structural extras), **any swap/economy op
touching one of these characters aborts on the Body slot** with a
`missing_layers` gap. The rendered image is fine (it was composed from the
correct art); only the stored attribute *string* is wrong.

Grounding the affected set (queried against `onchain_mainnet.db` and
`lfg_nfts.db` on the box, 2026-07-24):

- **Live on-chain tokens** carrying `"Body": "Iridescent Skeleton"` with
  `is_burned != 1`:
  - edition **64** — `00191B58…4943AAA` (index `mutable=1`)
  - edition **77** — `00091B58…00001207` (index `mutable` NULL/unknown — must be
    re-checked on-ledger)
- **App-DB `LFG` rows** (edition-keyed) with `Body = 'Iridescent Skeleton'`:
  editions **64** and **77** (row 64's stored `nft_id` `…00001223` is a
  superseded pre-swap token; the *live* edition-64 token is `…4943AAA`).
- Index `onchain_nfts` rows for those two editions carry the bad string in
  `attributes_json` (their `body` column is already the lowercased `skeleton`,
  so no `body`-column change is needed).

That is the "×4 [LFG/onchain]" the audit reported: 2 live on-chain tokens + 2
app-DB editions (plus their index rows), all resolving to the same two
editions. All burned duplicates (11 more index rows) are irrelevant.

## Constraints discovered

- **SourceTag + provenance memos are mandatory on every tx.** The correction is
  an `NFTokenModify`; `lfg_core/xrpl_ops.modify_nft` already stamps
  `source_tag=config.SOURCE_TAG` (2606160021) and
  `memos.build_memo_models(INITIATOR_BACKEND, platform, ACTION_MODIFY)` by
  construction — the script must go through `modify_nft`, never build a raw tx.
- **NFTokenModify requires the mutable flag.** Per CLAUDE.md, legacy flag-24
  (non-mutable) tokens cannot be modified in place. Edition 64's live token
  reports `mutable=1`; edition 77's is unknown in the index and MUST be verified
  on-ledger via `xrpl_ops.nft_info` before attempting a modify. A non-mutable
  token has no clean modify path and is a maintainer decision (Option 1 alias
  art, or a one-time burn+remint) — not something the script should force.
- **No forced burns / ledger is source of truth.** The on-chain metadata is
  authoritative; the DB/index mirror it. So the script modifies the token FIRST,
  then updates the index + app DB from the confirmed result — never the reverse.
- **CDN cache poisoning.** Re-uploading corrected metadata must use a fresh CDN
  stem (random suffix), mirroring `swap_flow._build_and_upload`'s discipline —
  BunnyCDN caches ~30d per URL, so reusing the old metadata path would keep
  serving the typo'd JSON. `lfg_core/cdn.upload_to_bunny(config.SWAP_CDN_FOLDER,
  …)` is the upload primitive.
- **Idempotency + dry-run gate.** Follow the `--apply`-gated data-correction
  pattern already in `scripts/reconcile_supply_growth.py` /
  `scripts/purge_foreign_supply_changes.py`: dry-run by default, mutate only
  with `--apply`; re-running after success is a no-op (tokens already double-r
  are skipped).
- **Network seam.** These are characters, so they resolve on
  `config.XRPL_NETWORK` (mainnet) via the single-network `xrpl_ops` globals and
  `nft_index.index_db_path("mainnet")` / the app DB. No `ECONOMY_NETWORK`
  involvement.

## Design

### Corrective script — `scripts/fix_iridescent_body.py`

A small, idempotent, `--apply`-gated CLI. Constants:

```python
BAD  = "Iridescent Skeleton"   # single-r
GOOD = "Irridescent Skeleton"  # double-r (has art)
```

Steps (per network, default `mainnet`):

1. **Discover targets.** Open `nft_index.index_db_path(network)` and select live
   rows (`is_burned IS NULL OR is_burned=0`) whose `attributes_json LIKE
   '%Iridescent Skeleton%'` but NOT `'%Irridescent Skeleton%'`. Also read the
   app DB `LFG` rows with `Body = BAD`. Print the reconciled target set
   (nft_id, edition, owner, mutable).
2. **Per live token — verify mutability.** Call `await
   xrpl_ops.nft_info(nft_id)` and confirm the mutable flag. If not mutable, log
   `SKIP non-mutable — maintainer decision` and continue (do NOT attempt modify).
3. **Rebuild metadata.** Fetch the token's current metadata JSON from its
   `uri_hex` (reuse `nft_index.fetch_metadata_multi(http, uri_hex)`), replace the
   Body attribute value `BAD → GOOD` in the `attributes` list (leave every other
   field — `image`, `video`, `edition`, `burnCount`, etc. — untouched; the art is
   byte-identical, so **no recomposition**). Re-serialize.
4. **Upload corrected metadata.** `await cdn.upload_to_bunny(
   config.SWAP_CDN_FOLDER, f"{edition}/{edition}_fix_{uuid4().hex[:8]}",
   json.dumps(meta, indent=2).encode(), "application/json")` → new
   `metadata_url`.
5. **Modify on-ledger.** `await xrpl_ops.modify_nft(nft_id, owner, metadata_url,
   platform=memos.PLATFORM_BACKEND)` — this carries SourceTag=2606160021 and
   `action=modify` memo. On `None` (definitive failure) log and continue; on
   `IndeterminateResultError` re-raise (never blind-retry an unknown modify).
6. **Sync mirrors from the confirmed result.** Update the `onchain_nfts` row
   (rewrite `attributes_json`, set `uri_hex` to the new URL's hex) via
   `nft_index.upsert(conn, rec)`, and set `LFG.Body = GOOD` for the edition in
   the app DB. `body` column stays `skeleton`.
7. **Journal** each token's before/after + tx hash to `reports/` (gitignored),
   matching the audit/reconciler convention.

Dry-run (default) performs steps 1–3 and prints the planned modify + the mirror
updates without uploading, modifying, or writing anything.

**On-ledger tx shape** (via `modify_nft`, unchanged): `NFTokenModify` with
`account=config.SIGNING_ACCOUNT`, `nftoken_id`, `owner` (when owner ≠ signer),
`uri=convert_str_to_hex(new_metadata_url)`, `source_tag=2606160021`, `memos=[…
initiator=backend, platform=backend, action=modify …]`.

### Recurrence guard

The recurrence vector is bad Body-value *data* re-entering (a stray single-r art
file on disk re-appearing, or a manual DB edit). Two layers, both grounded in
existing infra:

1. **Standing gate — `scripts/audit_trait_files.py`.** It already flags this
   exact class (it did) and is CI/pre-deploy-ready (exit 0 clean / 1 gaps / 2
   index missing). Document it as the pre-promote gate for mainnet in the ops
   note; after the fix it returns clean for this value.
2. **Layer-tree lint (small).** Add a check to the existing
   `validate-trait-config` pre-push hook path (or a tiny sibling check) that
   rejects a *known-typo* Body stem — specifically a single-r `Iridescent *`
   file under any `layers/<body>/Body/` dir — so the stray staging art can never
   be re-committed/synced into the pool. Keep it a narrow denylist (the two
   canonical spellings differ by one character), not a spell-checker.

## Out of scope

- Recomposing/re-uploading the NFT *image* — the art is byte-identical to the
  double-r WebM; only the attribute string is wrong.
- Option 1 (alias art `layers/skeleton/Body/Iridescent Skeleton.webm`). It would
  heal resolution instantly but injects the typo value into the live mint pool
  as a distinct trait — rejected per the issue's Option-2 preference. Kept only
  as the documented fallback for a **non-mutable** target token.
- Any change to burned duplicate tokens (11 index rows) — they are dead.
- Backfilling `history_*.db` events — no economy/BRIX impact.

## Open questions / decisions for maintainer

1. **Confirm Option 2 (modify) over Option 1 (alias art)** as the primary path.
   Issue text prefers Option 2; this design assumes it.
2. **Non-mutable target handling.** Edition 77's live token (`…1207`) has unknown
   mutability in the index. If on-ledger it is non-mutable (legacy flag 24), it
   cannot be `NFTokenModify`'d — do we (a) fall back to Option-1 alias art for
   that one token, (b) burn+remint it as a corrected blank, or (c) leave it and
   accept its swaps fail? Script SKIPs and reports; needs a call.
3. **Metadata-only correction OK?** Confirm we do NOT recompose the image (art
   identical), so the visible NFT is unchanged and only the on-chain attribute +
   mirrors move.
4. **Edition-64 app-DB nft_id drift.** The `LFG` row for edition 64 stores the
   superseded token `…1223`; the live token is `…4943AAA`. Confirm we correct by
   *edition* in the app DB (set `LFG.Body`) while modifying the *live* on-chain
   token, and do not touch the burned `…1223`.
5. **Guard scope.** Is the narrow layer-tree denylist lint wanted, or is keeping
   `audit_trait_files` green as a pre-promote gate sufficient on its own?

## Testing

- **Unit — resolution.** With a fixture layer store exposing `Irridescent
  Skeleton.webm`, assert `swap_compose.missing_layers([...Body=GOOD...],
  "skeleton", store)` returns `[]`, and the single-r `BAD` value returns a Body
  gap (documents the guard).
- **Unit — rewrite is surgical.** Given a real captured metadata JSON, assert the
  rewrite changes only the Body attribute value `BAD→GOOD` and leaves `image`,
  `video`, `edition`, `burnCount`, and all other attributes byte-identical; and
  that a JSON already double-r is returned unchanged (idempotency).
- **Unit — memo/sourcetag preserved.** Assert the script routes through
  `xrpl_ops.modify_nft` (monkeypatched) rather than building a raw tx, so
  SourceTag + `action=modify` memo are guaranteed.
- **Integration (dry-run).** Point the script at a temp copy of the index + app
  DB seeded with the two editions; run without `--apply` and assert zero
  mutations and a correct plan printout; run with `--apply` and a stubbed
  `modify_nft`/`upload_to_bunny` and assert `attributes_json` + `LFG.Body` are
  rewritten and re-running is a no-op.
- **Manual smoke.** On the box: `audit_trait_files.py --network mainnet` before
  (flags the value) and after `--apply` (clean); spot-check one modified token's
  live metadata via `nft_info` shows the double-r spelling; confirm a swap on the
  edition-64 character no longer fails `missing_layers`.
