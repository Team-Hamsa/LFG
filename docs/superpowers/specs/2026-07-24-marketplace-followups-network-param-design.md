# Marketplace follow-ups (network param + settlement + browse polish) — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #130

## Problem

#130 is the follow-up bucket left over from the marketplace PR #129 whole-branch
review — a checklist of non-merge-blocking items. The **Hardening / polish**
sub-list was fully cleared in PR #150 (all six boxes checked). What remains are
three **Architecture** items, one conditional hardening cleanup, and four
**Product** items. Grounding each against the code as it stands today:

1. **Trait on-ledger network parameterization** (`verify`/`get_tx`/`run_deposit`
   per network instead of the interim `ECONOMY_ENABLED` gate). The gate exists
   because every on-ledger op flows through the single-network `xrpl_ops`
   module-level globals (`config.JSON_RPC_URL` / `config.WS_URL` /
   `config.CLIO_WS_URL` / `config.SIGNING_ACCOUNT`, all derived from one
   `XRPL_NETWORK`) plus a single `SEED`. `config.validate_economy_config` hard-
   fails boot when `ECONOMY_ENABLED` is on and `ECONOMY_NETWORK != XRPL_NETWORK`.
   The motivating scenario — running the trait market on testnet while
   characters run on mainnet — was **retired by product decision**: the economy
   was flipped live on mainnet 2026-07-21 (#185), so `ECONOMY_NETWORK ==
   XRPL_NETWORK == mainnet` in prod today and the gate is no longer a
   limitation. This item is therefore **recommend-descope** (see Open questions).

2. **Detach settlement from the confirming buy poll.** In
   `lfg_service/app.py` the buy-status handler (`prefix == "buy"`, `outcome ==
   "sold"`) `await`s `_settle_trait_sale(...)` inline (≈ line 2058), which runs
   the full `economy_flow.run_deposit` (on-ledger issuer burn + Closet credit)
   before the HTTP poll response returns — a seconds-long response for the
   caller. The 2-minute sweep (`settle_pending_trait_sales`) already backstops
   any failure, so the inline await buys nothing but latency.

3. **Durable settlement-sweep attempt counter.** `_sweep_attempts` (trait) and
   `_shop_settle_attempts` (shop) are in-memory `dict`s in `lfg_service/app.py`.
   A service restart resets the count, so a genuinely stuck settlement is retried
   `_SWEEP_MAX_ATTEMPTS` times *again* after every restart — noisy, though safe
   because `run_deposit` is fail-closed and idempotent.

4. **`app.py`: promote `market_store` private helpers / drop the
   `cast(sqlite3.Row)` type-lie** — explicitly conditional in the issue ("if a
   second caller appears"). No second caller has appeared; `_attributes_match`
   remains single-caller inside `market_store.browse`. Recommend-descope.

5. **0-drop listings.** `market_store.browse` surfaces character rows verbatim;
   five stray third-party `amount_drops=0` sell offers exist on testnet. Product
   question: hide `amount_drops<=0` character rows, or accept free-transfer
   listings as a feature.

6. **Trait browse value filter is free-text exact-match.** In
   `webapp/client/app.js::loadMarketBrowse` the value comes from
   `el('market-trait-value').value.trim()` — a bare text input. A typo yields a
   silent empty result set with no hint.

7. **Trait listing thumbnail representative-body heuristic** — already improved:
   `lfg_service/app.py::_trait_image_url` now disk-verifies the body via
   `LocalLayerStore.find_display_body` (affinity-allowed → shared → any body with
   the art) rather than the old alphabetically-first guess. Needs only a product
   eyeball, no code. Recommend-descope.

8. **Brokered accept path** — the mixed sell+buy-node accept path shipped and has
   been exercised by the #131/#283 marketplace smoke passes. Recommend-descope.

This spec covers the genuinely-actionable, code-worthy remainder: **items 2, 3,
5, 6.** Items 1, 4, 7, 8 are called out as descope/no-op in Open questions so the
issue can be closed cleanly rather than left as a stale bucket.

## Constraints discovered

- **SourceTag + memos.** None of the four in-scope items builds a *new*
  transaction. Settlement re-uses the shipped `economy_flow.run_deposit`, whose
  issuer-burn already stamps `SourceTag = 2606160021` and provenance memos via
  `xrpl_ops` / `memos.build_memo_models` (`action=deposit`). Detaching the call
  (item 2) must not alter the tx it builds — only *when/how* it is awaited.
- **Fail-closed settlement taxonomy.** `run_deposit` verifies ownership
  on-ledger before the irreversible burn and journals `deposited_pending_closet`
  on a post-burn Closet-credit failure. Any detach must preserve the invariant
  that `settled=0` (set by `close_listing(..., 'sold')`) stays the durable "still
  owed" flag and the sweep (`unsettled_trait_sales`) remains the source of truth
  — never a fire-and-forget task whose failure is invisible.
- **Per-kind network seam.** `_market_network("trait")` → `ECONOMY_NETWORK`;
  `_market_network("character")` → `XRPL_NETWORK`. 0-drop is a *character* browse
  concern → resolve on `XRPL_NETWORK`. Do not conflate.
- **60 s browse cache.** `_MARKET_CACHE` holds the unfiltered per-`(network,
  kind)` join; every user filter is applied post-cache in Python. A 0-drop hide
  must run in the same post-cache path (or in `browse` itself) so it never keys
  the cache.
- **`market_listings` self-migrating schema.** New columns land via the
  forward-only `ALTER TABLE ... ADD COLUMN` block in `market_store.init_schema`
  (`buyer`, `amount_brix` precedent). A durable sweep counter would follow the
  same pattern. Same for `shop_orders` in `shop_store`.
- **No-build client + cache-buster.** `webapp/client/app.js` is loaded as
  `app.js?v=32` in `webapp/client/index.html`; any `app.js` edit bumps that in
  the same commit or the change never ships.

## Design

### Item 2 — Detach settlement from the buy poll

In `lfg_service/app.py`, the `outcome == "sold"` branch, after
`_close_listing_sync(..., "sold", wallet)` writes the durable buyer + `settled=0`:
replace the inline `await _settle_trait_sale(...)` for `listing_kind == "trait"`
with a **fire-and-forget task** that does not block the poll response:

```python
if session.listing_kind == "trait":
    # Kick settlement off the poll's critical path — the sweep
    # (settle_pending_trait_sales) is the durable backstop, and the row is
    # already settled=0 so nothing is lost if this task never runs.
    _spawn_bg(_settle_trait_sale(
        session.wallet_address, session.nft_id, session.offer_index, session.network))
```

`_spawn_bg` is a tiny helper (`asyncio.ensure_future` + a done-callback that logs
any exception — never swallow silently) added near the sweep helpers. The buy
poll returns immediately; the row's `settled=0` + the existing 2-minute sweep
guarantee eventual settlement. No tx shape changes.

### Item 3 — Durable sweep attempt counter

Add a self-migrating `settle_attempts INTEGER NOT NULL DEFAULT 0` column to
`market_listings` (via the `init_schema` ALTER block) and mirror the pattern in
`shop_store` for `shop_orders`. New `market_store` helpers:

- `bump_settle_attempts(conn, offer_index) -> int` — `UPDATE ... SET
  settle_attempts = settle_attempts + 1 ... RETURNING settle_attempts` (or
  read-back), returns the new count.
- `reset_settle_attempts(conn, offer_index)` — set to 0 on success (belt-and-
  suspenders; the row is deleted/settled anyway).

`settle_pending_trait_sales` reads/writes the count from the row instead of the
in-memory `_sweep_attempts` dict; the give-up threshold check
(`>= _SWEEP_MAX_ATTEMPTS`) reads the column. The in-memory dict is deleted.
`sweep_shop_orders` gets the analogous `shop_store` treatment. A restart now
preserves the count, so a stuck settlement gives up after a *global*
`_SWEEP_MAX_ATTEMPTS`, not per-boot.

### Item 5 — Hide 0-drop character listings

In `market_store.browse`, immediately after `_browse_character_rows(...)` (or as a
character-only guard in that helper), drop rows with `amount_drops` `None` or
`<= 0`. Character rows are always XRP-denominated, so a non-positive
`amount_drops` is a stray free-transfer / directed offer, not a real price. This
runs inside `browse` → automatically post-cache (the cache stores the raw join;
`_compute_market_rows` calls `browse` with `_MARKET_ROW_CAP`). Traits are
BRIX-denominated (`amount_brix`) and unaffected.

### Item 6 — Trait value filter datalist

Attach an HTML `<datalist>` to the existing `market-trait-value` input in
`webapp/client/index.html`, populated client-side from the distinct values for
the currently-selected slot. Source: the already-shipped public
`GET /api/shop/catalog` (`handle_shop_catalog`), whose rows carry `(slot,
value)` for every mintable trait — the client fetches it once (it already may,
for the shop), builds a `slot -> sorted[values]` map, and repopulates the
datalist when `market-trait-slot` changes. No new endpoint. Exact-match query
semantics are unchanged; the datalist just prevents typos and shows the valid
set. Bump the `app.js?v=` cache-buster.

## Out of scope

- **Item 1 (network-parameterized trait on-ledger ops).** A real fix means
  threading a per-network XRPL client *and a per-network signing wallet* through
  `xrpl_ops`/`market_ops`/`economy_flow` — a large refactor whose only payoff is
  the split topology the mainnet economy flip (#185) retired. Recommend-descope.
- **Items 4, 7, 8** — conditional/already-addressed/smoke-covered (see Problem).
- Any change to `run_deposit`'s tx shape, the on-ramp flow, or bids (#283).

## Open questions / decisions for maintainer

1. **Item 1** — confirm descope. Given `ECONOMY_NETWORK == XRPL_NETWORK` in prod
   and `validate_economy_config` enforcing it, is there any near-term plan to run
   a split topology? If not, close this line item as won't-fix and keep the gate.
2. **Item 5** — hide `amount_drops<=0`, or *accept* free-transfer listings as a
   deliberate feature? This spec assumes hide. If accept, the item is a no-op +
   doc note instead.
3. **Item 3** — is a per-boot reset actually a problem worth a schema migration,
   or is a bounded log-noise nuisance acceptable? (The retries are safe either
   way.) If cosmetic-only, this could be descoped too.
4. **Item 6** — datalist (this design) vs. a full `<select>`? A `<select>` forces
   valid values but loses free typing; a datalist is additive. Confirm the
   preferred UX.
5. **Item 4** — leave the `market_store` privates private (no second caller) and
   drop the checkbox as won't-fix?

## Testing

- **Item 2 (unit):** in a buy-poll test, patch `_settle_trait_sale` and assert
  the `sold` branch returns without awaiting it (e.g. the mock records it was
  *scheduled* not awaited; assert the poll response is produced before
  settlement completes). Assert `close_listing(..., 'sold')` still ran and
  `settled=0`.
- **Item 3 (unit):** `market_store` — schema has `settle_attempts` after
  `init_schema` on a pre-existing DB (migration); `bump_settle_attempts`
  increments and returns; sweep gives up after the threshold using the durable
  count across a simulated "restart" (new connection, count persists).
- **Item 5 (unit):** `browse(kind='character')` excludes a row with
  `amount_drops=0` and one with `None`; a positive row survives; trait BRIX rows
  unaffected.
- **Item 6 (unit):** a `market_pure.js` pure helper that turns catalog rows into
  a `slot -> values` map (tested with the existing JS test harness if present;
  otherwise a small pure-function test). Client wiring verified in smoke.
- **Integration/manual smoke:** buy a trait on testnet → poll returns fast →
  settlement lands within one sweep window; a live 0-drop testnet offer no longer
  appears in character browse; the value input shows a datalist of valid values
  and a typo yields the (still) empty result without a datalist match.
- Full `pytest` + `ruff`/`ruff-format`/`mypy`/`gitleaks`/`validate-trait-config`
  pre-push gate green; `app.js` cache-buster bumped in the item-6 commit.
