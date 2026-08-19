# Animated-trait grid rendering strategy — design

**Date:** 2026-07-24
**Status:** shipped — PR #378, merged 2026-08-17 · historical draft, see §0
**Issue:** #298 (closed 2026-08-17)

## 0. What actually shipped (reviewed 2026-08-19)

Drafted 2026-07-24 during a backlog triage sweep and never merged before the work
shipped. Kept as the design record linked from #298.

- **Shipped in:** PR #378 "Animated-trait grid strategy: static grids, badged
  tiles, lazy detail-view video (#298)" (`7f4179d`), merged 2026-08-17.
- **Followed this design:** in outline only — the governing rule ("dense grids
  never hold a decoder; only the focused/detail view plays video") and the
  Node-tested pure decision module both landed.
- **Diverged:**
  - **Scope moved from per-layer art to whole-NFT composite art.** The shipped
    PR touched no layer-tier code. `layerSrc()` in `webapp/client/app.js` is
    unchanged and still appends only `thumb=1`, so `renderCanvas`,
    `renderCloset` and `renderTraitStrip` still build tiles from the animated
    GIF thumb tier via `layerMediaEl`. The Closet grid-density problem this
    draft was written around was not addressed.
  - **No `layers/.stills/` tier.** `lfg_core/layer_thumbs.py` has no
    `STILLS_DIR` / `still_path_for` / `scan_stills`; `scripts/make_layer_thumbs.py`
    and `lfg_service/app.py::handle_layer` are untouched and there is no
    `still=1` query param anywhere in the tree. The "still" in shipped grids is
    the NFT's own metadata `image` (already a PNG first frame for animated
    editions), not a generated per-layer frame.
  - **No IntersectionObserver lazy-load** (zero occurrences in `app.js`).
    Laziness is `preload="metadata"` on the one detail `<video>` plus the
    native `loading="lazy"` the composite grids already carried.
  - **No video budget.** Seam 3's `MAX_LIVE_VIDEOS` / LRU release / iOS-budget-0
    machinery did not ship. The shipped rule is simpler and needs no budget:
    grids are always `<img>`, and exactly one `<video>` exists at a time — the
    listing-detail overlay's, paused by `closeListingDetail()` on close.
  - **Different pure module, different unit of decision.**
    `webapp/client/media_pure.js` (`isAnimated` / `gridMedia` / `detailMedia` /
    `videoFallback`, tested by `tests/test_media_pure_js.py`) decides per **row**
    — not `layer_media_pure.js` deciding per query string. It accepts both field
    dialects (`image`/`video` from market rows, `image_url`/`video_url` from
    `webapp/economy_api.py`).
  - **Detail asset is the composite MP4, not a per-layer WebM**, which required
    a server seam the draft did not anticipate: `onchain_nfts.video` (added by
    #377, `08e0af8`) surfaced onto rows by `lfg_service/app.py::_attach_character_meta`
    and `_serialize_listing_row`. `mediaEl` renders it `muted autoplay loop
    playsinline preload=metadata` with the still as `poster`, and degrades back
    to the poster via `mediaPure.videoFallback` on playback error.
  - **An "animated" badge was added** (`.anim-badge` "▶" on market cards and the
    GO picker, matching the swap grid's #250 marker) — an affordance this draft
    never proposed, and the substitute for per-tile hover animation.
  - The four open questions were resolved by omission rather than answered: no
    iOS VP9-alpha smoke was needed (the detail asset is MP4 with a poster
    fallback), no per-tile hover video, no parallel `.stills/` tree, no
    `MAX_LIVE_VIDEOS` constant.
- **Live behaviour is now documented in:** the module header of
  `webapp/client/media_pure.js`, and the `mediaEl` / `setMedia` /
  `closeListingDetail` comments in `webapp/client/app.js`.

## Problem

The layer thumbnail tier (#296 follow-up, spec
`docs/superpowers/specs/2026-07-21-layer-thumbnails-design.md`, shipped) gave
`/api/layer?thumb=1` a 512px preview tier under `layers/.thumbs/`. But it made
one deliberate simplification that this issue exists to refine: **animated
source art (`.gif`/`.webm`/`.mp4`) is re-encoded to an animated GIF thumb** so
it renders in a plain `<img>` (`lfg_core/layer_thumbs.py::_THUMB_EXT` maps
`.gif/.webm/.mp4 → .gif`). That solved "broken video tiles", but it did not
solve the *grid density* problem the issue names:

- The per-layer trait grids — `renderCloset` (`webapp/client/app.js` ~L2216),
  `renderTraitStrip` (~L2285), and the live `renderCanvas` stack (~L1951) —
  build every tile with `layerMediaEl(layerSrc(...))`, and `layerSrc()`
  (L1858) **always** appends `thumb=1`. A 200-trait Closet therefore decodes up
  to 200 **animated GIFs** on the main thread, all at once, with no lazy-load
  and no hardware assist — exactly the jank + memory pressure #298 describes.
- There is no *intentional* detail-view upgrade to the small, hardware-decoded
  WebM. The only `<video>` path for per-layer art is `layerMediaEl`'s **error
  fallback** (L1885): it swaps `<img>`→`<video>` only when the thumb 404s, and
  blanks when the webview can't play `video/webm; codecs="vp9"`. So the good
  animated experience (one crisp looping WebM) never happens on purpose; the
  janky one (N animated GIFs) happens everywhere.
- Discord mobile = iOS WKWebView caps simultaneous autoplaying `<video>`
  elements and does not support VP9 alpha — a naive "video everywhere" swap
  would freeze most tiles and/or paint alpha traits on black.

The marketplace/shop grids already use static composite `<img loading="lazy">`
(`renderMarketGrid` L2960, `renderShopGrid` L3425) with a per-listing detail
overlay (`openListingDetail`, #203) — that pattern is the model to generalize
to the per-**layer** trait grids.

## Constraints discovered

- **No transactions are built by this work** — it is pure display/asset
  plumbing. SourceTag `2606160021` and provenance memos are therefore N/A here;
  nothing on the on-ledger path changes.
- **`/api/layer` fallback invariant:** `handle_layer` (`lfg_service/app.py`
  L5079) only *prefers* a thumb when it exists on disk; a missing thumb falls
  through to the full asset, so a new query param **must never introduce a new
  404**. The stills tier must extend, not replace, that chain.
- **`layer_store` is local-disk / CDN, network-agnostic** — `_trait_image_url`
  (L972) resolves through `layer_store.get_layer_store()` +
  `LocalLayerStore.find_display_body`, independent of the ECONOMY_NETWORK vs
  XRPL_NETWORK seam. No network coupling to preserve here.
- **Hidden-dir enumeration:** the thumb tier is dot-prefixed (`.thumbs`) so
  `LocalLayerStore.list_bodies()` (skips hidden dirs) never treats it as a body
  or mint-pool source. Any new tier must be dot-prefixed the same way, or it
  poisons the mint pool.
- **Thumb winner-selection:** `layer_thumbs.scan` already resolves same-stem
  collisions (`X.gif` + `X.webm` → one thumb) by `LAYER_EXTENSIONS` priority so
  the thumb shows the art `resolve()` would serve. The stills tier must reuse
  that priority or a still could show different art than the full layer.
- **Cache-buster:** any `webapp/client/app.js` edit must bump
  `app.js?v=<n>` in `webapp/client/index.html` (currently `?v=32`) in the same
  commit, or Discord's webview serves stale JS.
- **iOS WKWebView:** `video.canPlayType('video/webm; codecs="vp9"')` is the
  ground-truth capability probe already relied on at L1891; VP9-alpha support
  on iOS is unverified and needs a real device smoke on the #296 staging
  deploy (open question below).

## Design

Three independent seams: a new **static-frame asset tier** (server +
generator), a **grid renderer that serves the static frame lazily**, and a
**detail-view upgrade to a budgeted WebM `<video>`**.

### Seam 1 — static first-frame tier (`layers/.stills/`)

A second preview tier mirroring `.thumbs/`, but **always PNG** — the static
first frame of every layer, so a grid tile is a single non-animating image.

- **`lfg_core/layer_thumbs.py`:** add `STILLS_DIR = ".stills"`,
  `still_path_for(src_path, base_dir) -> str | None` (maps *every* layer format
  → `.stills/<stem>.png`, with the same outside-base / already-inside-tier /
  non-layer guards `thumb_path_for` uses), and `scan_stills(base_dir)` that
  reuses the existing `LAYER_EXTENSIONS`-priority winner selection (a
  parameterized helper shared with `scan`, so `X.webm`+`X.gif` resolve to one
  still from the format `resolve()` serves). Static `.png` sources map to a
  downscaled PNG; animated sources map to their first-frame PNG.
- **`scripts/make_layer_thumbs.py`:** extend the generator to also emit the
  `.stills/` tree. Static PNG → lanczos downscale to `STILL_SIZE` (512). Animated
  (`.gif`/`.webm`/`.mp4`) → ffmpeg first-frame extract (`-frames:v 1`),
  forcing `-c:v libvpx-vp9` on `.webm` inputs (native VP9 decoder drops alpha —
  same rule as compose and the GIF thumb path), lanczos to 512, alpha
  preserved. mtime-idempotent + `--check` drift mode, mirroring the thumb tier.
- **Serving — `handle_layer` (`lfg_service/app.py` L5079):** add a `still=1`
  query param. When `thumb=1 && still=1` and the store is local, prefer
  `layer_thumbs.still_path_for(...)` **first**, then fall to
  `thumb_path_for(...)`, then the full asset — an ordered chain that keeps the
  never-a-new-404 invariant (a still that hasn't been generated yet degrades to
  the GIF thumb, then to the full asset).
- **`_trait_image_url` (L972):** append `&still=1` alongside the existing
  `&thumb=1` — the shop/market **trait** tiles are dense grids and want the
  static frame.

### Seam 2 — lazy static grid tiles (client)

- **New pure module `webapp/client/layer_media_pure.js`** (node-testable like
  `build_pure.js`): `layerParams(body, trait, value, {still, full})` returns
  the query string (`thumb=1`, `+still=1` for grids, neither for `full`
  detail); and the video-budget LRU logic (Seam 3) so both are unit-tested off
  the DOM.
- **`layerSrc(body, trait, value, opts)` (L1858):** delegate the query build to
  `layerParams`. Default (grid) callers pass `{still: true}`; the detail
  upgrade passes `{full: true}`.
- **`layerStillEl(src, alt, onMissing)`:** a grid-tile builder that renders a
  plain `<img>` and, on error, walks the **server** fallback chain (still→thumb
  →full is server-side already) and only calls `onMissing` on a genuine 404 —
  it never tries a client-side `<video>` (grid tiles stay static, so the iOS
  video cap and VP9-alpha issue simply don't apply to grids). `renderCloset`,
  `renderTraitStrip`, and the shop/market trait tiles switch to this.
- **Lazy-load — `observeLazy(imgEl, src)`:** a single shared
  `IntersectionObserver` (rootMargin ~`200px`, so tiles fetch just before entering
  the viewport). Grid builders set `data-src` and register the tile; the
  observer assigns `.src` on intersection and unobserves. This covers the
  per-layer trait grids that currently eager-load; the composite-image grids
  keep their native `loading="lazy"` (already present at L2962/L3427). One
  observer instance, reused across renders, disconnected on panel teardown.

### Seam 3 — budgeted detail-view WebM upgrade (client)

The "living" trait is shown only where one or two videos are cheap:

- **`renderCanvas` (L1920)** — the live composited character *is* the detail
  view for the equipped set. Its stack keeps animation, but via the full WebM
  in a capped `<video>` rather than N animated GIFs: each animated slot renders
  a still `<img>` first, then upgrades to a `layerSrc(..., {full:true})`
  `<video muted autoplay loop playsinline>` **through the budget**.
- **Video budget (`layer_media_pure.js`):** `MAX_LIVE_VIDEOS` (desktop 4, iOS/
  WKWebView 1) with LRU release — acquiring a slot over budget pauses + reverts
  the least-recently-focused video back to its still `<img>` and frees the
  decoder. iOS detection = `!video.canPlayType('video/webm; codecs="vp9"')`
  (reuse L1891) → budget 0, i.e. never upgrade; the still `<img>` (or the
  animated GIF thumb, if VP9-alpha proves broken on iOS) stands in.
- **Trait grid focus/zoom (optional, behind the budget):** a Closet/trait tile
  gaining focus/hover/selection may request one full WebM for *that* tile; on
  blur it releases the slot. Kept strictly to one live video via the same
  budget so a fast scrub can never spawn a decoder storm.

### Data-model / asset summary

| Tier | Path | Format | Consumer |
|------|------|--------|----------|
| full | `layers/<body>/<T>/<V>.{png,gif,webm,mp4}` | source | compose/mint (unchanged); detail `<video>` (webm/mp4) |
| thumb (#296) | `layers/.thumbs/…/<V>.{png,gif}` | animated=GIF | fallback + non-iOS detail-when-no-still |
| **still (new)** | `layers/.stills/…/<V>.png` | always PNG | **every grid tile** |

No compose/mint change: minted output stays PNG/H.264 MP4.

## Out of scope

- Mint/swap **preview** hero + reveal video (the composite `image`/`video`
  metadata pair via `mediaEl`/`setMedia`, `/api/img` proxy) — that is **#204**.
  #298 owns the **per-layer trait grid** strategy and the shared
  static-tier/lazy/video-budget primitives; #204 consumes the same primitives
  for the composite reveal. Cross-reference #204.
- Compose-side ffmpeg concurrency under bulk mint (issue notes it as separate).
- Any on-ledger / metadata change.

## Open questions / decisions for maintainer

1. **iOS VP9-alpha smoke (blocking the fallback branch):** does
   `<video>` VP9-alpha actually render (not on black) in Discord iOS WKWebView
   on the #296 staging deploy? If **no**, the iOS detail fallback is the
   animated **GIF thumb** (`thumb=1` without `still`), not a dead `<video>`;
   if **yes**, iOS can still upgrade one WebM. The design defaults iOS budget
   to 0 (static) until this is answered — safe either way.
2. **Do the trait grids even need per-tile focus animation**, or is a static
   grid + animated *composited character* (renderCanvas) enough? The latter is
   simpler and covers the "see it move" need for the Build panel; per-tile hover
   video may be gold-plating.
3. **Still tier vs. reusing the thumb tier with a suffix** —
   `.stills/<V>.png` (parallel tree, chosen here for symmetry with `.thumbs/`)
   vs. `.thumbs/<V>.still.png` (one tree, suffix). Parallel tree keeps
   `still_path_for` a clean mirror of `thumb_path_for`; confirm the extra tree
   is acceptable (both are gitignored, regenerable).
4. **`MAX_LIVE_VIDEOS` values** (desktop 4 / iOS 1) — need a device check; these
   are guesses.

## Testing

- **Unit (`tests/test_layer_thumbs.py`):** `still_path_for` mapping (every
  layer ext → `.stills/<stem>.png`; rejects outside-base, inside-`.thumbs`/
  `.stills`, non-layer); `scan_stills` (missing/stale/fresh/orphan, hidden dirs
  ignored, same-stem winner by priority).
- **Unit (`webapp/test_smoke.py`):** `handle_layer` with `still=1` prefers the
  still, falls to thumb then full (never a new 404), no-param behavior
  unchanged; `_trait_image_url` carries both `thumb=1` and `still=1`.
- **Unit (`tests/test_layer_media_pure_js.py`, node harness):** `layerParams`
  emits `thumb=1&still=1` for grids, no thumb params for `full`; video-budget
  LRU (acquire over budget evicts least-recent; iOS cap 0).
- **Generator smoke:** run `make_layer_thumbs.py` on a fixture tree with one
  `.png`, one `.gif`, one alpha `.webm`; assert each `.stills/*.png` is 512×512
  and the webm still preserves alpha (first-frame, not black).
- **Manual (staging Activity):** 200-trait Closet scrolls smoothly (static
  tiles, lazy fetch — verify via network panel that off-screen tiles don't load);
  the Build canvas character animates; **iOS device** check for question 1.
