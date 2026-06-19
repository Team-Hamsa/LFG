# Layer Coverage Auditor — Design

**Date:** 2026-06-18
**Status:** Approved (revised — source pivoted from DB to on-chain, see below)

## Revision note: why the source is on-chain, not the DB

The first cut audited the `LFG` table. That is **structurally wrong** for this
purpose and produced a false negative (it passed edition #3547, which a live
swap then rejected). Root cause: `LFG.nft_number` is the **primary key — one row
per edition** — but the chain holds **multiple NFTokens per edition** (duplicate
/ divergent variants created by prior swaps and reminting). The swap reads live
on-chain metadata, so the only faithful source is an on-chain enumeration. The
audit now pages clio's `nfts_by_issuer` (live tokens only), fetches each token's
real metadata, and checks that. The DB is no longer consulted.

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
- The `LFG` DB as a source (it cannot represent per-edition duplicates).

## Data Sources

- **On-chain enumeration** via clio's `nfts_by_issuer` (issuer + taxon), which
  returns every NFToken — live and burned — with its metadata URI inline. Burned
  tokens are skipped. Endpoints: mainnet `wss://s2-clio.ripple.com`, testnet
  `wss://clio.altnet.rippletest.net:51233` (plain altnet rippled does NOT serve
  this method). Mainnet issuer is the fixed collection issuer; the testnet issuer
  is the SEED minter account (from `config.SWAP_ISSUER_ADDRESS`). All overridable
  via `--issuer/--taxon/--clio`; `--network` selects the defaults.
- **Per-NFT metadata** fetched from each token's URI (`swap_meta.fetch_metadata`,
  shared `aiohttp` session, bounded concurrency).
- CDN layer tree via `lfg_core.layer_store` (the same store the swap uses).

This is primarily a **testnet** tool — testnet metadata is on BunnyCDN, so a run
completes in seconds and is authoritative. `--network mainnet` works but mainnet
metadata is on IPFS (slow/flaky at collection scale); a meaningful chunk lands in
the "unreadable metadata" bucket, so a mainnet run is best-effort, not
authoritative. Making mainnet authoritative is deferred (see DB-sync follow-up).

Body class is derived with `swap_meta.detect_body` (`Straight`→male,
`Curved`→female, `Ape`→ape, else `skeleton`), matching the swap path.

## Behavior / Faithfulness

Each NFT's metadata is run through `swap_meta.normalize_attributes` (fixes the
`Accesory` typo, fills missing traits with `None`, relocates Angel-Wings to
`Back`) before checking — the same normalization the swap uses, so results can't
drift. `None`/empty values are skipped (no layer needed), exactly as
`swap_compose._ordered_traits` skips them.

Existence is checked against a **cached set** of available values per
`(body, trait_type)`, built from `store.list_values()` (reads only the cached
directory listing). The auditor never calls `store.resolve()`, so it performs
**no layer downloads**. Because the available sets cover all four bodies, a value
that exists for one body but not another is correctly flagged — covering the
cross-body case where a swap moves a trait onto a body that lacks its layer.

## Components / Boundaries

- `enumerate_onchain(clio, issuer, taxon) -> [{nft_id, uri_hex}]` — pages clio,
  live tokens only. The single networked enumeration step.
- `build_available_sets(store) -> dict[(body, trait_type), set[str]]` — warms the
  CDN listings. I/O-bound, async.
- `meta_attributes(metadata) -> (body, attributes)` — pure: normalize + body.
- `audit_attributes(body, attributes, available) -> list[Missing]` — pure, no
  I/O. Fully unit-testable with a synthetic `available` dict.
- `run_audit(enumerate_fn, fetch_meta_fn, store)` — orchestrates with injected
  enumerator + metadata fetcher (so it is testable without a network), bounded
  by a fetch-concurrency semaphore. Returns one `NftResult` per token.
- `format_reports(results, timestamp, network)` — pure: builds the report.
- `main()` — wires real clio enumeration + HTTP fetch, writes the report, prints
  a summary, sets exit code.

`Missing` is a frozen dataclass `(body, trait_type, value)`. `NftResult` carries
`nft_id, number, body, missing, error` — keyed on `nft_id` since edition numbers
duplicate on-chain. NFTs whose metadata can't be fetched land in an `error`
bucket and are reported separately (not silently dropped).

## Reports

Console summary plus a written Markdown report at
`reports/layer-coverage-<network>-<timestamp>.md`. Three views:
1. **Aggregated worklist** — unique `(body, trait_type, value)` missing across
   live NFTs, each with the count of NFTs it blocks, sorted by impact. The
   actionable list of layer files to upload.
2. **Per-NFT failures** — `# | body | nft_id | missing traits`. The NFTs that
   currently cannot be swapped.
3. **Unreadable metadata** — tokens that could not be audited (surfaced, not
   hidden, so coverage gaps in the audit itself are visible).

Exit code is non-zero when any gap is found (CI-ready, Approach 2).

## Testing

Unit tests using `LocalLayerStore` over a fixture tree plus synthetic metadata,
with injected enumerator/fetcher for the end-to-end path:
- Clean NFT — no gaps.
- The real #3547 `Wonder` variant — reports its three missing female layers.
- Cross-body gap — a value present for `male` but not `female` is flagged on a
  female NFT (the class the per-edition DB audit could not see).
- All-`None` NFT — zero gaps.
- Aggregation — two NFTs missing the same asset collapse to one worklist entry.
- End-to-end — two on-chain tokens sharing edition #3547 (clean + `Wonder`); the
  clean one passes, the duplicate fails, and a URI-less token lands in `error`.

## Location

`scripts/audit_layer_coverage.py`. Tests in `tests/test_audit_layer_coverage.py`.
