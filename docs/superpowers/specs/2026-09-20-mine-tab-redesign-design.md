# Mine tab redesign — five intent groups, two display units

**Issue:** [#583](https://github.com/Team-Hamsa/LFG/issues/583)
**Date:** 2026-09-20
**Status:** approved, ready for plan

## Problem

The Marketplace **Mine** tab renders nine flat sections, each a wrapping strip
of variable-width `.trait-chip` pills with 36px thumbnails. Measured in
headless Chrome against the real mainnet holder distribution (586 holders in
`onchain_mainnet.db`), it fails at both ends — for opposite reasons:

| Case | Holders | Renders as |
|---|---|---|
| 1 NFT, no Closet | **365 (62%)** | 1 chip + **8 italic "nothing here" lines** |
| ~24 characters, 18 Closet traits | ~7% | 1,532px desktop / **2,936px mobile** |
| 349 characters, 475 Closet rows | 8 wallets (42% of supply) | **15,582px**, with only 200 of 475 Closet rows loaded |

Four root causes:

1. **Wrong unit.** `.trait-chip` is a variable-width pill with a 36px thumb.
   Width follows label length, so every wrapped row ends somewhere different —
   that ragged edge is most of the visual complaint. Browse already solved
   this with `.nft-grid` (`repeat(auto-fill, minmax(120px,1fr))`, full-bleed
   square art, `max-height` + `overflow-y` containment); Mine uses none of it.
2. **Sections named after tables, not intentions.** Three trait buckets
   ("Unlisted traits" = wallet trait NFTokens, "Loose Closet traits" = Closet
   credits, "My Closet market orders") and three bid buckets, all at identical
   heading weight, so an incoming offer ranks the same as trade history.
3. **No scale strategy.** `_compute_mine_data` has no `LIMIT`; the client
   renders one chip per row with no cap, filter or containment.
4. **Money invisible, taps too small.** `#1035 — 12.0 XRP` is one uniform text
   run (Browse styles price via `.market-card-price`); action buttons are
   0.72rem text on 3px padding, under touch targets on the Telegram Mini App
   and Discord Activity surfaces.

## Goals

- A 1-NFT holder sees their NFT and one honest sentence — not eight apologies.
- A 349-NFT holder sees a page of bounded height with their actionable items
  at the top.
- Anything needing the user's signature or decision is the first thing on
  screen.
- Art renders at a size where you can tell two characters apart.

## Non-goals

- Server-side pagination. The largest wallet's payload is **269 KB raw /
  17 KB gzipped**; the payload is not the problem. No change to
  `/api/market/mine`, `/api/market/bids/mine` or `/api/closet/orders/mine`.
- Trimming the unused `attributes` field (~400 of 609 bytes per character) —
  real, but not worth the scope at 17 KB gzipped.
- Moving Closet inventory into the Dressing Room, or touching Browse, Shop or
  Wanted.

## Design

### 1. Five intent groups

Nine table-shaped sections become five groups named for what the user is
trying to do. Two of today's sections **split**, which is where the three
indistinguishable trait buckets and three bid buckets resolve:

| Today | Goes to | Action |
|---|---|---|
| Bids on my NFTs | **Needs you** | Accept |
| Bids on my traits | **Needs you** | Fill / Deposit & fill |
| Closet orders, `state = 'pending_escrow'` | **Needs you** | Finish signing |
| Closet fills, `state = 'funds_pending'` and `buyer == wallet` | **Needs you** | Finish paying |
| Closet fills, `state = 'failed'` | **Needs you** | View |
| My listings (character XRP + trait BRIX) | **Selling** | Cancel |
| Closet orders, `side = 'ask'` | **Selling** | Cancel |
| My bids | **Buying** | Cancel |
| Closet orders, `side = 'bid'` (not `pending_escrow`) | **Buying** | Cancel |
| Unlisted characters | **Your stuff → Characters** | List |
| Unlisted trait tokens **+** loose Closet traits | **Your stuff → Traits** (merged) | Sell |
| Closet fills, everything else | **History** | View |

Group order on screen: **Needs you, Selling, Buying, Your stuff, History.**

`closet_market_store.orders_for_owner` already returns exactly the four live
states (`pending_escrow`, `open`, `matched`, `cancelling`), so the split is a
pure client-side partition on `side` and `state` — no new query, no new
endpoint.

`matched` ("filling") and `cancelling` admit no user action, so they stay in
Selling/Buying carrying a state chip rather than appearing in Needs you.

**Needs you ordering:** incoming offers first (money waiting), then the
user's own stuck actions oldest-first. Incoming offers group by kind before
sorting — character bids (XRP) then trait bids (BRIX), each descending by its
own price. Sorting one list across two currencies would be meaningless.

### 2. Merged Traits, custody as a badge

A trait you own is one fact; whether it is a Closet credit or an NFToken in
your wallet is plumbing. They merge into one Traits grid.

Tiles are keyed by **(slot, value, custody)** — at most two tiles per trait
key, so a tile's badge describes the whole tile unambiguously and its action
is never ambiguous about which copy it acts on. Wallet-custody tiles carry an
"in your wallet" badge and disclose the signature cost at action time. This
follows the precedent already in the codebase: `holdingFillAction` picks
"Deposit & fill" vs "Fill" from one merged list of incoming bids.

Data backs the merge: only the house wallet holds many trait tokens — every
other holder has ≤3 — so a separate wallet-token bucket is empty for
essentially every user, which is the same disease as the eight italic
apologies.

Quantities: Closet tiles show `×count`; several wallet tokens of one key
group into one tile showing `×n`. A partially encumbered tile reads
`×3 · 2 listed`, so a refused Equip is explicable.

### 3. Two display units, chosen by what matters

`.trait-chip` is one pill doing two unrelated jobs. Split it by whether the
art identifies the thing:

- **Items you hold or are selling → cards.** Reuse `.nft-grid` / `.nft-card`
  exactly as Browse and the swap picker do. Art goes **36px → full-bleed
  square** (~120–160px), columns align. Price gets its own line via
  `.market-card-price` (yellow, bold), currency-correct: XRP for characters,
  BRIX for traits. The action becomes a full-width footer button,
  `min-height: 40px`.
- **Offers and trade events → rows.** Bids, orders and fills are about
  counterparty, price and state, not art. A **grid** row, not a chip:
  `grid-template-columns: 44px 1fr auto auto` (thumb · label · price ·
  action), so the ragged edge is gone. Below 420px the price and action move
  to a second grid row via `grid-template-areas`, keeping the action
  full-width and ≥40px.

Applied per group: **Selling** and **Your stuff** are cards; **Needs you**,
**Buying** and **History** are rows.

Selling is deliberately **one grid holding both currencies** — character
listings priced in XRP beside trait listings and Closet asks priced in BRIX.
Each card's price label carries its own unit, which is enough; splitting
Selling by currency would reintroduce the near-empty-bucket problem this
redesign exists to remove. Your stuff splits Characters from Traits because
those differ in *kind and action*, not merely in denomination.

### 4. Collapsible group headers

Each group is a `<details class="mine-group">` reusing the pattern shipped in
[#579](https://github.com/Team-Hamsa/LFG/pull/579) for the Mint odds panel —
`<summary>` as the whole hit target, carrying the group name, a count, and a
`▾` chevron that rotates when open. Open by default for Needs you, Selling,
Buying and Your stuff; **History starts collapsed.**

Open state is kept in a module-level `Set` keyed by group id, mirroring
`oddsOpenSlots`, so the poll-driven re-render after an action does not
collapse what the user opened.

### 5. Caps and the "Show all" drill-in

Each group caps what it renders inline:

| Group | Unit | Inline cap |
|---|---|---|
| Needs you | rows | 20 (flood guard only) |
| Selling | cards | 12 |
| Buying | rows | 5 |
| Your stuff → Characters | cards | 12 |
| Your stuff → Traits | cards | 12 |
| History | rows | 5 |

Over the cap, the group ends with `Show all N →`, which opens `#mine-all`: a
focused view replacing the group list with a back control, the group title and
count, a search box, and the group's **full, uncapped** list in its own unit.
Search filters client-side, case-insensitively, over each item's title and
subtitle.

That view reuses `.nft-grid`'s existing scroll containment, and it is the only
thing on screen — so **exactly one scroller is ever live** and the nested-scroll
trap that stacked containers would create on mobile never arises.

The result is a page whose height is bounded by the caps rather than by
holdings: roughly **2,000px at 900px wide** for the 349-character wallet,
against 15,582px today, and — the real point — no longer growing with
collection size. Narrow viewports fit fewer cards per row, so the same caps
yield a taller page there; it stays bounded and constant regardless of how
much the wallet holds.

### 6. Emptiness

- **A group with nothing in it renders nothing** — no header, no italic line.
- If Needs you, Selling and Buying are all empty, one muted line renders under
  Your stuff: *"Nothing listed or bid on yet."*
- If the caller owns nothing at all: *"You don't own anything yet."* plus a
  control that switches to Browse.
- With `ECONOMY_ENABLED=0` the trait groups arrive empty and therefore vanish,
  instead of printing "Nothing here." — a strict improvement, no special case.

### 7. Edge cases preserved

- `value === 'None'` Closet rows stay visible, dimmed and actionless, sorted
  last — an empty slot is a holding, not a trait. This preserves the
  deliberate choice the code marks `(#516 D5)` in `closet_market_store.py`,
  `house_closet.py` and `app.js`. Note that tag is an internal
  review-finding ID, **not** GitHub issue 516, which is unrelated.
- Null `image_url` (a value with no art) falls back to `BLANK_IMG`.
- `renderChipList` is **not** deleted — the Dressing Room trait strip and the
  Wanted book (`closet-book-bids`) still use it. Only Mine stops calling it.

## Implementation shape

### `webapp/client/mine_pure.js` (new, pure)

A DOM-free module, following the `market_pure` / `closet_market_pure`
convention:

```js
buildMine({ mine, bids, closet, wallet, closetMarketEnabled })
// ->
// {
//   needsYou: Item[],
//   selling:  Item[],
//   buying:   Item[],
//   stuff: { characters: Item[], traits: Item[] },
//   history:  Item[],
//   ownsNothing:   boolean,  // no items in any group
//   nothingActive: boolean,  // needsYou + selling + buying all empty
// }
```

`Item` carries the **raw** image fields (`image` for a CDN character URL,
`image_url` for a server `/api/layer` trait URL) and lets `app.js` pick the
resolver — the same seam `marketPure.mapListingRow` + `marketRowImgSrc` draw
today, because `imgUrl`/`traitLayerSrc` are surface-dependent.

```js
// unit: 'card' | 'row'
// Exactly one of `image` / `imageUrl` is set, or neither (-> BLANK_IMG).
{ unit, key, image, imageUrl, title, subtitle, price: {amount, currency} | null,
  badges: string[], state: {label} | null, dim: boolean,
  action: { label, kind, payload } | null }
```

`action.kind` is a string name — `'cancelListing' | 'cancelBid' | 'acceptBid'
| 'list' | 'sell' | 'fillClosetBid' | 'cancelClosetOrder' | 'viewClosetFill' |
'resumeClosetOrder' | 'resumeFill'` — dispatched through a handler table in
`app.js`. All state→label, side-split, merge, sort and cap logic lives here,
where it is testable under Node.

### `webapp/client/app.js`

`renderMineGroups`, `renderBidGroups` and `loadClosetMine`'s three
`renderChipList` calls collapse into one `renderMine()` consuming
`buildMine()`'s output, plus `renderMineCard` / `renderMineRow` /
`showMineAll(groupKey)`. `renderChipList` stays for its other two callers.

### `webapp/client/index.html`

The nine `.trait-strip-section` blocks under `#market-mine` become five
`<details class="mine-group">` containers plus the `#mine-all` drill-in
container.

### CSS and cache busters

`style.v40.css` → `style.v41.css` with the new `.mine-*` rules; `app.js?v=109`
→ `?v=110`; the new `./mine_pure.js?v=1` import. All three bump **in
lockstep** — an ES-module import's `?v=` that lags is the known cache trap in
this client.

### Server

Unchanged.

## Testing

- **`tests/test_mine_pure_js.py`** — Node-run unit tests of `buildMine()`,
  matching how `market_pure` / `closet_market_pure` are already tested:
  - the partition is **total** — every input row lands in exactly one group,
    and no row lands in two
  - both splits: Closet orders by `side` and by `pending_escrow`; fills by
    Needs-you state vs History
  - trait merge keyed `(slot, value, custody)`, quantity roll-up, `listed`
    encumbrance text
  - `value === 'None'` → dimmed, actionless, sorted last
  - `ownsNothing` / `nothingActive` flags
  - caps and the over-cap remainder count
  - `closetMarketEnabled = false` and economy-off payloads
- **Visual** — re-run the headless-Chrome harness at all three holder shapes
  (1 / 24+18 / 349+475) × 900px and 390px, before and after.
- **Gate** — the existing pre-push suite (ruff, mypy, gitleaks, pytest).

## Risks

- **Cache busters.** Three version pins must move together; a stale
  `mine_pure.js?v=` silently serves old logic.
- **Discord client caching.** The Discord client can keep running a stale
  `app.js` despite no-store headers — fully relaunch the Activity and confirm
  the served version before debugging "impossible" behavior.
- **`renderChipList` has other callers.** Deleting it breaks the Dressing Room
  strip and the Wanted book.
