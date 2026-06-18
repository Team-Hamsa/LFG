# Layer Coverage Auditor — Design

**Date:** 2026-06-18
**Status:** Approved

## Problem

Trait swaps recompose an NFT's image on demand from the CDN layer tree
(`layers/<body>/<TraitType>/<Value>.png|gif|mp4`). A swap aborts (fail-safe,
before any burn) when a trait value on the NFT has no backing layer file —
surfaced to the user as `Missing trait layer files: …`.

This was hit in practice: testnet NFT #3536 carries `Accessory = "Super Soaker"`,
but `layers/male/Accessory/` has no such file. The NFT's own baked image
(`minttest/nft_3536.png`) is fine — the gap is in the *layer tree*, not the
per-NFT image. Such gaps most likely originated when the layer source was cut
over to CDN-only and some local layer files were not uploaded (or were renamed).

We need to know, across the whole minted collection, which NFTs cannot be
swapped and exactly which layer assets are missing.

## Scope

In scope:
- **Layer coverage (check B):** for every minted NFT in the `LFG` table, verify
  every non-`None` trait value resolves to a file in the CDN `layers/` tree.
- Both networks (`mainnet` + `testnet`) — the layer tree is one shared CDN
  folder, so a single pass covers both.

Out of scope:
- Per-NFT baked image liveness (check A — whether `image_url` returns 200).
- Uploading the missing layer files.
- Live on-chain enumeration / metadata fetch (the audit reads the DB, the
  stated source of truth).

## Data Sources

- `lfg_nfts.db` → `LFG` table (3552 rows). Relevant columns:
  `nft_number, network, body_type, Body, Background, Back, Clothing, Mouth,
  Eyebrows, Eyes, Hat, Accessory`.
- CDN layer tree via `lfg_core.layer_store` (the same store the swap uses).

### Column → trait-type mapping

The DB trait columns map onto layer trait-types 1:1 **except** `Hat → Head`
(the layer tree and `swap_meta.TRAIT_ORDER` use `Head`). Full set checked:
`Background, Back, Body, Clothing, Mouth, Eyebrows, Eyes, Head, Accessory`.

Body class is derived with `swap_meta.detect_body(Body)` (`Straight`→male,
`Curved`→female, `Ape`→ape, else `skeleton`) so it matches the swap path rather
than trusting the stored `body_type` column.

## Behavior / Faithfulness

To guarantee the audit cannot drift from real swap behavior, each row is run
through `swap_meta.normalize_attributes` (fixes the `Accesory` typo, fills
missing traits with `None`, relocates Angel-Wings values to `Back`) before
checking. Values equal to `None` (or empty) are skipped — they need no layer
file, exactly as `swap_compose._ordered_traits` skips them.

Existence is checked against a **cached set** of available values per
`(body, trait_type)`, built from `store.list_values()` — which reads only the
(cached) directory listing. The auditor never calls `store.resolve()`, so it
performs **no layer downloads**.

## Components / Boundaries

- `build_available_sets(store) -> dict[(body, trait_type), set[str]]`
  Warms one set per `(body, trait_type)` for the four bodies. I/O-bound, async.
- `row_attributes(row) -> tuple[str, list[dict]]`
  Pure: maps a DB row to `(body, normalized_attributes)`.
- `audit_row(body, attributes, available) -> list[Missing]`
  Pure, no I/O. Returns missing `(trait_type, value)` for one NFT. Fully
  unit-testable with a synthetic `available` dict.
- `format_reports(results) -> (per_nft_md, worklist_md)`
  Pure: builds the two report views.
- `main()` — sqlite read, orchestrates the above, writes the report file,
  prints a summary, sets exit code.

`Missing` is a small dataclass/namedtuple: `(body, trait_type, value)`.

## Reports

Console summary plus a written Markdown report at
`reports/layer-coverage-<timestamp>.md` (timestamp passed into `main()`).

Two views:
1. **Per-NFT failures** — table of `nft_number | network | body |
   missing traits`. These are the NFTs that currently cannot be swapped.
2. **Aggregated worklist** — unique `(body, trait_type, value)` missing across
   the collection, each with the count of NFTs it blocks, sorted by impact.
   This is the actionable list of layer files to upload to the CDN.

Exit code is non-zero when any gap is found, so the script can later be wired
into CI as a regression gate (Approach 2) without changes.

## Testing

Unit tests using `LocalLayerStore` over a small fixture tree plus synthetic
rows:
- Clean NFT — no gaps reported.
- Missing accessory (the #3536 case) — reports `male/Accessory/Super Soaker`.
- All-`None` NFT (the #3538 case) — reports zero gaps.
- `Hat → Head` mapping — a value present under `Head` is found when the row
  supplies it in the `Hat` column.
- Aggregation — two NFTs missing the same asset collapse to one worklist entry
  with count 2.

## Location

`scripts/audit_layer_coverage.py`, alongside `scripts/rebuild_collection_db/`.
Tests in `scripts/test_audit_layer_coverage.py` (or the repo's test location).
