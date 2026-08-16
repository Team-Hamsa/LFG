# Leaderboard two-tier board selector — design

**Date:** 2026-07-04
**Status:** Approved (user)
**Scope:** Frontend-only (`webapp/client/`). No API, backend, cache, or DB changes.

## Problem

The Activity home-screen leaderboard exposes its 8 boards as a single flat row
of chips (`webapp/client/index.html` `#lb-boards`). On desktop Discord the row
horizontally scrolls — bad UX, and the grouping of boards is invisible.

Also: the "Hot NFTs" label was unclear. It is the `nft_swaps` board (NFTs
ranked by trait-swap count in the period). It will be renamed **"Swaps"**.

## Design

Replace the flat chip row with a two-tier selector: a category row and a
sub-board row. Max 3 chips per row — nothing scrolls on desktop or mobile.

```
[ Users ] [ NFTs ] [ BRIX ]          ← category tabs (#lb-cats)
[ Holders ] [ Swappers ] [ Builders ] ← sub-board chips (#lb-boards)
```

### Category → board mapping

| Category | Boards (key → label) |
|----------|----------------------|
| Users | `users_nfts` → Holders, `users_swaps` → Swappers, `users_builds` → Builders |
| NFTs | `nft_swaps` → Swaps, `nft_rarity` → Rarest |
| BRIX | `brix_rich` → Richlist, `brix_lp` → LP, `brix_earned` → Earned |

Board keys are unchanged — the `/api/leaderboard` contract is untouched.

### Markup (`index.html`)

- New `#lb-cats` row with 3 category tab buttons (`data-cat="users|nfts|brix"`),
  `role="tablist"`.
- `#lb-boards` remains but is emptied in HTML; its chips are rendered by JS
  from the category map so the two levels can never drift.

### State & behavior (`app.js`)

- `CATEGORIES` const: ordered map of `cat → [{board, label}, ...]` per the
  table above.
- `lbState` gains `cat` (default `'users'`); `board` default stays
  `'users_nfts'`.
- Clicking a category tab: sets `lbState.cat`, re-renders `#lb-boards` with
  that category's chips, auto-selects the category's **first** board, and
  calls `loadLeaderboard()`.
- Clicking a sub-board chip: unchanged behavior (set `lbState.board`, load).
- Active-state rendering in `loadLeaderboard()` extends to also mark the
  active category tab.
- `NFT_BOARDS` (image-row rendering) is unchanged.

### CSS (`style.css`)

- Category tabs styled slightly heavier (weight/underline or filled pill) than
  sub-chips so the hierarchy reads visually.
- Both rows `flex-wrap`; remove any horizontal-scroll styling from
  `.lb-boards`.

## Error handling

No new failure modes: category switching is pure client state; the only
network call remains the existing `loadLeaderboard()` fetch with its existing
error/empty handling.

## Testing

- Update existing webapp tests that assert the old 8-chip static row.
- New checks: (1) switching category replaces the sub-chip set and fetches the
  category's first board; (2) sub-chip click within a category still switches
  boards; (3) "Swaps"/"Rarest" render NFT image rows (NFT_BOARDS path intact).

## Out of scope

- Any `/api/leaderboard` change, period/anchor controls, `me` row, caching.
- Persisting the selected category across sessions.
