# Trait Shop filtering — design (2026-07-20)

## Problem
The Trait Shop grid renders the full catalog (521 items across 9 slots on
testnet) as one flat list — finding a specific trait or browsing a slot is
needlessly hard.

## Decision (approved: option A)
Client-side-only filtering. The catalog already arrives whole from the single
cached `GET /api/shop/catalog` response, so no server change is needed or
wanted (filters must never fan out the server cache).

## UI (webapp/client/index.html, market-shop section, above shop-grid)
- **Slot chips** (`shop-slot-chips`, `lb-chips` styling): "All" plus one chip
  per slot present in the catalog, labeled with its count ("Head · 121").
  Built dynamically from the fetched items; active-chip handling mirrors the
  existing `highlightTabs` pattern.
- **Search** (`shop-search`): text input, live case-insensitive substring
  match on the trait value on `input` — no Apply button, everything is local.
- **Sort** (`shop-sort`): Price low→high (default) · Price high→low · A→Z.

## Logic
- `filterShopItems(items, {slot, query, sort})` — pure function in
  `webapp/client/market_pure.js` (Node-testable like the other helpers).
  Slot `'all'` passes everything; query trims + lowercases; sorts are stable
  with `value` as tie-breaker.
- `shopState = { items, slot, query, sort }` module state in app.js;
  `loadShopCatalog` stores the raw items once, control changes re-render via
  `renderShopGrid(filterShopItems(...))`.
- Empty filtered result: existing `shop-empty` copy; chips/search stay usable
  to clear.

## Testing
- Node tests for `filterShopItems` (slot narrowing, case-insensitivity,
  all three sorts, combined filters, empty result).
- Source-assertion DOM guards in `tests/test_market_panel_dom.py`.
- Buy flow untouched.
