# Layer thumbnail tier (`layers/.thumbs/`) — design

**Date:** 2026-07-21 · **Status:** approved

## Problem

The five animated Body layers (Diamond/Irridescent) are VP9-alpha `.webm`
(plus one `.mp4` background). `/api/layer` serves whatever
`layer_store.resolve()` finds, and the clients render it in `<img>` —
WebM/MP4 don't decode there, so the trait shop and the Discord Activity show
broken tiles (Discord's webview also fails the `<video>` fallback for these).
Separately, every preview grid ships full 1080px assets (multi-MB for
animated art) where a small thumbnail would do.

## Decision (approach A)

A pre-generated thumbnail tree mirroring `layers/`:

- **Location:** `layers/.thumbs/<body|shared>/<TraitType>/<Value>.{png,gif}`.
  Dot-prefixed, so `LocalLayerStore.list_bodies()` (which skips hidden dirs)
  never sees it — zero enumeration changes. Gitignored (inside `layers/`),
  fully regenerable.
- **Mapping:** `.png` → 512×512 PNG (ffmpeg lanczos); `.gif`/`.webm`/`.mp4` →
  512×512 GIF (RGBA frames → gifski, alpha preserved; `.webm` decoded with
  `libvpx-vp9` explicitly — ffmpeg's native VP9 decoder drops alpha). GIF
  renders in a plain `<img>` everywhere, sidestepping the video problem.
- **Generator:** `scripts/make_layer_thumbs.py` — mtime-idempotent (rebuilds
  missing/stale thumbs only), prunes orphans, `--check` mode exits non-zero
  on drift. Pure scan/mapping logic lives in `lfg_core/layer_thumbs.py` so
  the service and tests need no media tooling.
- **Serving:** `/api/layer?thumb=1` remaps the resolved full path into
  `.thumbs/`; a missing thumb falls back to the full asset (today's exact
  behavior — the param can never introduce a new 404). Local store only.
- **Clients:** the Activity's `layerSrc()` and the server-built
  `_trait_image_url` (shop/market trait tiles) append `thumb=1`. The
  `layerMediaEl` `<video>` fallback stays as a safety net for un-thumbnailed
  art. Full-res assets remain exclusively for server-side compose/mint.

## Rejected alternatives

- **On-demand generation with cache:** puts ffmpeg/gifski in the request
  path and on the service's dependency list.
- **Regenerating 1080 display GIFs next to the WebMs:** no new code, but
  keeps shipping huge GIFs and reintroduces the source-of-truth ambiguity
  #296 removed.

## Ops

One-time `make_layer_thumbs.py` run per tree (prod `~/LFG/layers`, staging
`~/LFG-staging/layers`); re-run whenever art is added/replaced (added to the
add-a-trait checklist in CLAUDE.md). Deploy order is free: `thumb=1` with
fallback is safe before thumbs exist, and thumbs are inert before the code.

## Testing

`tests/test_layer_thumbs.py`: path mapping (incl. `.thumbs`/outside/non-layer
rejection), scan (missing/stale/fresh/orphan, hidden dirs ignored), handler
thumb-preferred + fallback + no-param behavior, `_trait_image_url` carries
`thumb=1`. Generator conversion verified by smoke run (512×512, alpha OK).
