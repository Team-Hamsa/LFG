# Legacy Linode snapshots (synthesized 2026-06-11)

Curated from the `~/Linode` rsync of the old production server. Of the many
overlapping copies, only the newest/unique content survives here; everything
else was byte-identical duplication and was discarded.

## Provenance / lineage

| Server copy | Verdict |
|---|---|
| `discord/` | TraitSwapper repo @ bdb1743 (2024-04-24) — ancestor of pushed `master`, superseded |
| `ghost/` | byte-identical duplicate of `discord/` |
| `hamsa/TraitSwapper` + `hamsa/CDNer` | byte-identical duplicates of `traitswapper/*` |
| `traitswapper/TraitSwapper` | repo @ 614d342 (2024-08-30, = GitHub `LetsEffingGo/TraitSwapper` master) **plus uncommitted work** → preserved in `traitswapper/` |
| `Mint-Bot/` | newest lineage (Nov 2025) → preserved in `mint-bot/` |

The current LFG repo's `ts_helpers.py`/`main.py` descend from the
`helpers4.py` lineage and are ahead of it — the repo remains the canonical
working code. These files are kept for reference and for features not yet
ported.

## mint-bot/ (Nov 2025 — newest, post-dates this repo's base)

- `swapper.py` — Discord trait-swap bot entry point. **Incomplete**: imports
  `swap_service` (SwapSessionTracker, run_swap) which did not exist anywhere
  on the server copy.
- `nft_db.py` — DB layer with features NOT yet in `lfg_core`:
  `NFT_Ownership` cache table, `mutable` column on LFG table, swap history
  recording (`update_nft_after_swap`, `record_swap_in_history`). Candidate
  for porting.
- `season3_unique_traits.json`, `season3_exclusion_list.txt`,
  `Collection - Current Nov 28 2025.csv` — season 3 planning data.

## traitswapper/ (Aug 2024 + uncommitted)

Final working tree of the original TraitSwapper bot. `helpers4.py`,
`bunnycdn.py`, `download_image.py` and the modifications to
`main2.py`/`helpers2.py` were never committed/pushed.
`swap-records/` holds per-NFT swap metadata snapshots (`<nft>_<n>.json`).

## cdner/

XRPL NFT fetcher scripts + `lfgo_nftIDs.json` (full collection NFT-ID dump).

## Related (outside this directory)

- Canonical swap layer images (`ape/female/male/skeleton`, 1185 files,
  ~680 MB): `~/LFG/layers/` (gitignored). CDN `layers/` folder is still
  EMPTY — upload with `scripts/upload_layers_cdn.py` when ready.
- Old production `.env` files (different SEED/MintSeed than current repo):
  `~/.lfg-legacy-env/` (kept out of the repo).
- Git history: pushed to GitHub `LetsEffingGo/TraitSwapper`.
- `~/Linode/audit` and `~/Linode/xparrot` were left in place (separate
  projects, not LFGO mint/swap copies).
