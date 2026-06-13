# Collection DB rebuild tooling

One-shot operational scripts that rebuild the `LFG` table in `lfg_nfts.db` with
**real on-chain trait data** for the mainnet collection.

## Why this exists

The original `lfg_nfts.db` shipped with 3535 *placeholder reservation rows* —
every trait was literally the string `"placeholder"` and no `nft_id` — inserted
only so `get_next_nft_number()` (= `MAX(nft_number)+1`) would start new mints at
3536. There was never any real trait data, so anything derived from it (e.g. the
rarity engine) was meaningless. These scripts replace those rows with the actual
traits resolved from the live on-chain collection.

## The data sources (and the mess)

- **On-chain (XRPL mainnet)** is the source of truth for *which* NFTs are live.
  Minter `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ`, taxon `1760`. Trait swaps burn the
  old NFT and re-mint a new one, so "ever minted" ≫ "live".
- **CDN** (`lfgo.b-cdn.net/LFGO/<n>/<n>_0.json`) holds the *original* mint metadata
  for ~half the collection. Post-swap re-mints were never mirrored there.
- **IPFS** holds the re-mint metadata, spread across a few shared directory CIDs
  plus standalone CIDs. Public gateways are **slow and flaky** for this content —
  step 3 is resumable and usually needs 2–3 runs to mop up transient timeouts.

### Schema quirks

- Metadata trait_type `Head` → LFG column **`Hat`**.
- Metadata trait_type `Accesory` (misspelled in the source) → LFG column **`Accessory`**.
- The metadata `edition` field is unreliable for Season 1 (returns `1`); the real
  edition number is parsed from `name` (`"Let's Effing Go! #N"`).

## Pipeline

Run in order from this directory (intermediate files land in `work/`):

```bash
python 01_enumerate_onchain.py            # XRPL -> work/onchain.json (live + burned)
python 02_scan_cdn.py                      # CDN  -> work/cdn_scan.json (fast traits)
python 03_resolve_traits.py                # CDN+IPFS -> work/traits.json  (re-run until errors=0)
python 04_populate_lfg.py --db ../../lfg_nfts.db            # DRY RUN: inspect the plan
python 04_populate_lfg.py --db ../../lfg_nfts.db --apply --prune   # commit
```

**Back up `lfg_nfts.db` before `--apply`.** `--prune` deletes mainnet rows whose
edition has no live on-chain NFT (burned-and-never-re-minted) so they don't
pollute rarity counts.

After populating, rebuild the rarity cache:

```bash
python ../../rarity_admin.py --network mainnet refresh
```

## Notes

- Requires `xrpl` and `requests` (already in `requirements.txt`).
- `work/` is intermediate scratch; safe to delete between full runs. The per-URI
  IPFS cache under `work/meta_cache/` is what makes step 3 resumable — keep it
  between re-runs so you don't refetch what already succeeded.
