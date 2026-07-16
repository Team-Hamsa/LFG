# Build UI nitpicks — design

**Date:** 2026-07-16
**Scope:** `webapp/client/` (`app.js`, `index.html`, `style.css`) — five contained UX fixes to the Dressing Room ("Build") panel of the Discord Activity. No service/API changes.

## Background

User feedback on the builder:

1. The entry button says "👗 Dress Up".
2. There is no back button inside the Build panel.
3. Opening the panel default-lands on an unindexed token ("#null · still indexing…").
4. The GO (character NFT) selector is an unlabeled strip of small thumbnails at the bottom — poor UX.
5. Closet trait tiles whose layer art doesn't exist for the selected GO's body render as broken images instead of being hidden.

## Design

### 1. Rename entry button

`index.html:34`: `👗 Dress Up` → `🏗️ Build`. Retitle other user-visible "Dress Up / Dressing Room" strings in the panel (flow titles stay as-is where they name specific ops like Closet/Harvest).

### 2. Back button

Add a `← Back to the job site` button to `dressup-panel`, same class/pattern as the swap panel's `swap-done-btn` (`index.html:91`), returning to the home panel via the existing panel-switch machinery.

### 3. Default GO selection

`openDressup()` picks `characters[0]` blindly (`app.js:1277`); an unindexed token (empty `body`) yields "#null · still indexing…". Change the default to the **first character with a truthy `body`**, falling back to `characters[0]` only when none are indexed. Apply the same rule to the post-harvest re-selection (`app.js:1520`).

### 4. GO picker overlay (replaces the bottom roster strip)

- Under the canvas: a caption showing the active GO (`#3521 · male`) plus a **Switch GO** button.
- Tapping it opens a full-panel overlay (same overlay styling approach as `confirmDialog`): a wrapping grid of labeled tiles — thumbnail, `#edition`, body name. The active GO is highlighted (border + check). Unindexed GOs are greyed out, labeled "indexing…", and not selectable. The `＋ Assemble` tile lives in the grid (disabled while the Closet gate is up, as today).
- Selecting a GO closes the overlay and re-renders canvas/closet (existing `selectCharacter`).
- The old `roster-strip` element and its render path are removed.

```
Build view:            Overlay:
┌───────────┐          ┌─ Your GOs ─────── ✕ ┐
│  canvas   │   tap →  │ ┌─────┐ ┌─────┐     │
│ #3521·male│          │ │ img │ │ img │ ... │
│ [Switch GO]          │ │#3521│ │#398 │ [＋] │
└───────────┘          │ │ male│ │ ape │     │
                       └──────────────────────┘
```

### 5. Hide non-rendering Closet tiles

In `renderCloset()` and `renderTraitStrip()`:
- Attach `img.onerror` handlers that remove the tile/chip when the layer fetch 404s for the selected body.
- Tiles that would already fall back to `BLANK_IMG` (incomplete data / no active char render path) are hidden rather than rendered blank.
- Hidden tiles reappear when the user switches to a GO whose body has that art.

**Accepted caveat:** a hidden tile's Extract button is hidden with it. A trait that renders on *no* owned GO can't be extracted from this panel; the market trait-sell wizard remains the path for that. Acceptable for now.

## Error handling

No new failure modes: item 5 converts an existing silent failure (broken `<img>`) into removal; everything else is presentation. Server prechecks on equip/extract are unchanged and remain authoritative.

## Testing

- Extend the webapp pure/smoke tests where logic is unit-testable (default-GO selection rule; picker tile labeling/disabled states if factored into pure helpers).
- Overlay behavior and onerror-hide are DOM-driven — verify manually with `WEBAPP_DEV_MODE=1` and a full Activity relaunch (Discord client caches `app.js`).
