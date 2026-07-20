# Repoint on-chain-index images to the CDN + clobber-guard

**Status:** Design approved 2026-07-20
**Issue:** (marketplace/Activity broken-image follow-up to PR #282)

## Problem

`onchain_nfts.image` (the per-`nft_id` on-chain index) holds a raw `ipfs://`
image URL for **3,479 of 4,128 live mainnet editions**. Those CIDs are largely
**unpinned**, so any consumer that fetches the stored URL gets a broken image:

- The Discord/web **Activity** — mitigated for most editions by the archive-first
  `/api/img` proxy (serves `images_mainnet/<edition>.png` when present), but the
  proxy falls through to fetching the stored URL for editions the local archive
  doesn't hold, and that stored `ipfs://` is dead.
- The **OG / X share-card page** reads `onchain_nfts.image` **raw**
  (`_og_fetchable_image_url(onchain.image)`), so an unpinned `ipfs://` = a broken
  card.

The operator has been manually `UPDATE`-ing the column to CDN URLs, but the fix
does not stick: the ledger **listener** re-derives `image` from the on-chain
metadata (still `ipfs://` for the original collection) whenever it processes a
later transaction for that token, overwriting the manual repoint. This is the
recurring treadmill.

### Ground truth established by CDN scan (2026-07-20)

A probe of every live edition against `https://lfgo.b-cdn.net/LFGO/...`:

- **Images resolve on the CDN for 4,125 / 4,128 editions (99.9%).**
- Only **3 editions — `220`, `736`, `2419` — have no image anywhere** (absent from
  both the CDN and the local archive). These are known issuer-parked /
  rejected-offer orphans; out of scope here (need original art sourced).
- The CDN image path is **NOT deterministic**: the variant suffix
  (`LFGO/<ed>/<ed>_<N>.png`) increments with each swap/re-mint. Observed hit
  distribution: `_0` ≈ 2,800, stored-URL ≈ 640, `_1` ≈ 340, `_2` ≈ 124,
  `_3`–`_8` ≈ 180. So the correct URL cannot be *constructed* from the edition
  number — it must be **probed** and then **stored**.

The metadata JSON resolves at a similar ~99% rate but is irrelevant to serving
(the Activity is index-driven and never fetches live metadata JSON), so it is
not part of this work.

## Goal

`onchain_nfts.image` should hold the **working CDN URL** for every edition that
has one, and nothing in steady state should overwrite it back to `ipfs://`.

Non-goals: changing `/api/img` (archive-first + `_fetch_cdn` already serves a
stored CDN URL correctly); sourcing art for the 3 orphan editions; touching the
metadata JSON; any change to how art is minted/uploaded.

## Design

Two components: a one-time repair and a permanent guard.

### Component 1 — `scripts/repoint_images_to_cdn.py`

An idempotent, rerunnable ops script (same posture as
`scripts/backfill_nft_numbers.py`).

- Iterate every live edition (`onchain_nfts`, `is_burned=0`, `nft_number NOT
  NULL`) in the network's index DB.
- **Target rows** are those whose stored `image` is **ipfs-shaped or empty**
  (`ipfs://`, `.../ipfs/<cid>`, `<cid>.ipfs.<host>`, or `''`). Rows already
  holding a non-ipfs (CDN) URL are **skipped without any HTTP call** — they are
  app-mint/swap-written and current; re-verifying 640+ good rows over HTTP buys
  nothing. (A `--force` flag may re-probe CDN-shaped rows too, for the rare case
  a swap left a stale variant; off by default.)
- For each target row, probe an ordered candidate list until one returns HTTP 200:
  1. `https://lfgo.b-cdn.net/LFGO/<ed>/<ed>_<N>.png` for `N` in `0..8`,
  2. `https://lfgo.b-cdn.net/LFGO/lfg_<ed>.png`.
- On a hit, `UPDATE onchain_nfts SET image = <winning URL> WHERE nft_id = ?`
  (all live `nft_id`s for that edition — the index is per-`nft_id`, an edition
  can have duplicate live tokens).
- On no hit, leave the row untouched and collect the edition into a printed
  "no CDN image found" list (expected: `220`, `736`, `2419`).
- Async `aiohttp` prober with bounded concurrency (~30) and a per-request
  timeout, mirroring `scratchpad/cdn_scan.py`.
- Network-aware via `--network` (default from `XRPL_NETWORK`), resolving the
  index DB through `nft_index.index_db_path`. Dependency-light like
  `backfill_nft_numbers.py` (no `lfg_core.config` secrets required).
- Prints a summary: scanned / repointed / already-CDN / no-hit, and the no-hit
  edition list. The prober is injectable so it is unit-testable offline.
- `--dry-run` flag: probe and report what *would* change without writing.

Host is fixed to `lfgo.b-cdn.net` (the raw pull zone, matching existing stored
URLs), confirmed with the operator.

### Component 2 — clobber-guard in `nft_index.upsert`

Replace the `image` line of the `ON CONFLICT(nft_id) DO UPDATE SET` clause so a
write can never replace a resolvable (non-ipfs) URL with an ipfs one:

```sql
image = CASE
  WHEN excluded.attributes_json='[]' THEN image          -- fetch failed: keep (existing behavior)
  WHEN (excluded.image LIKE 'ipfs://%' OR excluded.image LIKE '%/ipfs/%'
        OR excluded.image LIKE '%.ipfs.%')
       AND onchain_nfts.image <> ''
       AND NOT (onchain_nfts.image LIKE 'ipfs://%' OR onchain_nfts.image LIKE '%/ipfs/%'
                OR onchain_nfts.image LIKE '%.ipfs.%')
    THEN image                                            -- incoming ipfs, stored is CDN: keep CDN
  ELSE excluded.image
END
```

Properties:
- **Host-agnostic:** prefers *any* non-ipfs URL over an ipfs one; no CDN hostname
  baked into the SQL. The only non-ipfs URLs in play are CDN URLs, so this is
  equivalent to "prefer CDN."
- Recognizes all three ipfs shapes the index stores: `ipfs://` (raw),
  `.../ipfs/<cid>` (path gateway, e.g. dweb.link), `<cid>.ipfs.<host>`
  (subdomain gateway).
- **Swaps still win:** a swap/re-mint writes a CDN `image`, so
  `is_ipfs(excluded.image)` is false → `ELSE` branch → the new CDN URL is taken.
  The guard only fires when the *incoming* value is ipfs, i.e. the listener
  re-processing an untouched original-collection token.
- Twin of the existing `nft_number = COALESCE(...)` and
  `ledger_index = COALESCE(...)` guards in the same statement.

Note this intentionally decouples `image` from the `body`/`attributes_json`
"ride along" grouping: in the guard-fires case the incoming attributes are the
same trait values already stored (an untouched token), so taking incoming
attributes while keeping the stored image is harmless.

## Data flow / steady state

1. **One-time:** run `repoint_images_to_cdn.py --network mainnet` against prod →
   ~3,476 ipfs rows become CDN URLs; 3 orphans reported.
2. **Ongoing:** the listener keeps writing from the ledger. App mints + swaps
   carry CDN image metadata → stored as CDN natively. Transfers of untouched
   original-collection tokens arrive with ipfs metadata → **guard keeps the
   repointed CDN URL.** The column stays CDN with no manual intervention.
3. Consumers unchanged: `/api/img` (archive-first, then `_fetch_cdn(CDN)`) and
   the OG card (`_og_fetchable_image_url(CDN)`) both work.

## Testing

`tests/test_nft_index.py`:
- guard: incoming ipfs over stored CDN → keeps CDN.
- guard: incoming ipfs over stored ipfs → takes incoming (no worse option).
- guard: incoming CDN over stored ipfs (swap) → takes incoming CDN.
- guard: empty fetch (`attributes_json='[]'`) → keeps stored (regression).
- guard: gateway-form ipfs (`.../ipfs/<cid>` and `<cid>.ipfs.<host>`) recognized.

`tests/test_repoint_images_to_cdn.py`:
- with an injected fake prober: writes the first resolving candidate; leaves
  no-hit editions untouched and reports them; `--dry-run` writes nothing;
  idempotent second run is a no-op.

## Rollout

1. Land the guard + repoint script via a normal reviewed PR to `main` (touches
   `lfg_core/nft_index.py`).
2. After merge + promote + deploy, run
   `repoint_images_to_cdn.py --network mainnet` against prod once. (Order is
   guard-first so the very next listener tx can't undo the repoint mid-run.)
3. Spot-check the OG card and an Activity browse image for a formerly-ipfs
   edition. Follow-up (separate): source art for `220`, `736`, `2419`.
