# Marketplace follow-ups (network param + settlement + browse polish) — design

**Date:** 2026-07-24
**Status:** live — re-reviewed 2026-08-19 against main@27dc301
**Issue:** #130
**Last review:** 2026-08-19

> Scope shrank on re-review. The headline architecture item (per-network
> trait on-ledger ops) is **obsolete**, not merely descoped, and two more
> items are recommended won't-fix against explicit in-code rationale. What is
> left is three small, independent changes — one of which flipped from
> "descope" to "actionable" because its precondition finally happened.

## 0. What changed since this was drafted

- **The split topology the whole item-1 design existed for cannot run.**
  `config.validate_economy_config` is called at import in `lfg_core/config.py`
  and raises `ValueError` whenever `ECONOMY_ENABLED` is on and
  `ECONOMY_NETWORK != XRPL_NETWORK`, so *any* economy-enabled process
  configured on a split fails to start. That half is verifiable from this
  tree. The deployment half is attributed, not verified (the deployed `.env`
  files are untracked): issue #185's closing comment (2026-08-16) states the
  trait economy has been live on mainnet since 2026-07-21 via an
  `ECONOMY_ENABLED=1` + `ECONOMY_NETWORK=mainnet` flip. Together: prod runs
  matched networks, and no stack can run the economy on a split at all.
  - Do **not** reuse the draft's staging argument.
    `docs/ops/env.staging.example` never mentions `ECONOMY_NETWORK`, and its
    header instructs the operator to copy prod's `.env` and apply only the
    listed overrides — so staging inherits prod's `ECONOMY_NETWORK`, it does
    not fall back to `lfg_core/config.py`'s `testnet` default. Since that
    same file sets `ECONOMY_ENABLED=1`, a literal reading of the procedure
    yields a stack that cannot boot; the staging box must therefore set
    `ECONOMY_NETWORK=testnet` by hand (undocumented) or the deltas file has a
    gap. Out of scope for #130 — but do not cite that file as evidence that
    staging is matched, and consider filing the deltas-file gap separately.
- **Five places still assert the retired split as present-tense fact** —
  `CLAUDE.md`'s "Per-kind network seam" paragraph ("the deployed topology
  runs characters on mainnet while the trait economy stays testnet-gated …
  until the economy reaches mainnet"), two docstrings in `lfg_service/app.py`
  (`_market_network` and `_compute_mine_data`, the latter: "the networks can
  differ in the deployed topology"), and comments in
  `tests/test_market_api.py` / `tests/test_config_economy_validate.py`. This
  stale prose is the only remaining deliverable from item 1.
- **Item 4 flipped from descope to actionable.** The issue conditions it on
  "if a second caller appears". Two have: `lfg_service/app.py`
  (`handle_market_listings`' post-cache trait filter) and
  `webapp/mock_market.py::browse` both call `market_store._attributes_match`,
  and app.py additionally calls `market_store._row_attrs(cast(sqlite3.Row,
  r), kind)` on rows that are plain dicts — the exact type-lie the checkbox
  names.
- **Item 7 is done, by different code than the draft cited.** The resolver
  moved out of `lfg_service/app.py` into `lfg_core/trait_images.py::
  trait_image_url` (shared with `webapp/mock_market.py`); `app.py::
  _trait_image_url` is now a one-line delegate kept so tests can monkeypatch
  it. Under a `LocalLayerStore` (`LAYER_SOURCE=local`, the deployed setting)
  it disk-verifies the body via `LocalLayerStore.find_display_body` and
  returns `None` on a miss rather than a known-404 URL; under a CDN store it
  still falls back to the affinity-only guess. PR #357 (merged 2026-08-15)
  further fixed trait image URLs in Mine.
- **Item 8 is done and now has a unit test**, not just "implicit smoke
  coverage": `lfg_core/nft_listener.py::_accept_new_owner` resolves the buyer
  from the deleted buy offer's `Owner` on a brokered accept, covered by
  `tests/test_market_listener.py::
  test_accept_brokered_persists_buy_offer_owner_not_broker`.
- **Both remaining Architecture items are argued against in the code
  itself**, by comments that predate this spec (both introduced in PR #129,
  confirmed with `git log -S`). The draft did not engage with either. See §3.
- **Item 2 has a user-facing cost the draft missed.** `webapp/client/app.js::
  marketBuyRender` renders a trait `done` state as *"Sold — added to your
  Closet."* That sentence is true today only because settlement is awaited
  before the poll returns. A naive detach makes it a lie for the duration of
  the burn+credit, and permanently wrong if settlement then fails.
- **Citations that rotted:** the schema/migration entry point is
  `market_store.init_db`, not `init_schema`; the browse cache-buster is
  `app.js?v=59` (was 32) and `market_pure.js` is imported as
  `./market_pure.js?v=23` from `app.js` — a `market_pure.js` edit must bump
  *both*.
- **Item 6 got cheaper.** PR #292 (merged 2026-07-20) shipped client-side
  shop-catalog filtering:
  `webapp/client/app.js` already fetches `/api/shop/catalog` into
  `shopState.items`, and `webapp/client/market_pure.js` already exports
  `filterShopItems` / `shopSlotCounts` with a real Node execution harness at
  `tests/test_market_pure_js.py`. The datalist helper slots straight in.

## 1. Where every #130 checkbox stands

| # | Item | Verdict |
|---|------|---------|
| 1 | Parameterize trait on-ledger ops per network | **Obsolete** — close won't-fix; doc hygiene only |
| 2 | Detach settlement from the confirming buy poll | **Descope** unless shipped with an honest two-phase UI |
| 3 | Durable settlement-sweep attempt counter | **Drop** — durable is worse under crash-loop |
| H | Six Hardening/polish boxes | Already done in PR #150 (merged 2026-07-10) |
| 4 | Promote `market_store` privates / drop `cast` | **Actionable** — precondition now met |
| 5 | 0-drop listings: hide or accept | **Open** — needs a product call, then ~5 lines |
| 6 | Trait browse value filter is free text | **Actionable** — datalist |
| 7 | Trait listing thumbnail heuristic | **Done** (`lfg_core/trait_images.py`) |
| 8 | Brokered accept path untested | **Done** (`_accept_new_owner` + listener test) |

Only 4, 5, 6 carry code, plus the item-1 doc fix. Everything else should be
ticked or struck on the issue so #130 can close.

## 2. Design — the parts still worth building

### 2.1 Item 4 — publish the two attribute helpers

`lfg_core/market_store.py` keeps `_attributes_match(attrs, filters)` (AND
across slots, OR within a slot; exact, case-sensitive value match) and
`_row_attrs(row, kind)` private. Three call sites exist now: `browse` itself,
`lfg_service/app.py`'s post-cache trait filter, and
`webapp/mock_market.py::browse`.

- Rename to `attributes_match` / `row_attrs`; the module has no `__all__`, so
  publishing is a rename plus call-site updates.
- Retype `row_attrs`' first parameter away from `sqlite3.Row` so both a
  `sqlite3.Row` (what `browse` passes internally, before its trailing
  `dict(r)` conversion) and a plain `dict` (what the two external callers
  pass) are accepted. That is what lets the `cast(sqlite3.Row, r)` in
  `lfg_service/app.py` — and the four-line comment apologising for it — be
  deleted.
  - **Do not use `Mapping[str, Any]`.** `sqlite3.Row` is not a `Mapping`
    subclass (`issubclass(sqlite3.Row, collections.abc.Mapping)` is `False`
    at runtime, and typeshed does not register it either), so `browse`'s own
    internal call would fail the repo's `strict = true` mypy gate with
    `Argument 1 … has incompatible type "Row"; expected "Mapping[str, Any]"
    [arg-type]`. Verified 2026-08-19 with mypy on a reduced case.
  - Use `row: Any`, matching the existing convention for exactly this shape
    in the same module — `market_store.listing_price(row: Any)` already takes
    a Row-or-dict, and `Any` is already imported there. A local `Protocol`
    with
    `def __getitem__(self, key: str) -> Any: ...` also typechecks for both
    inputs if a tighter bound is wanted; both were checked under
    `mypy --strict`. Either way the cast, not the annotation, is the lie the
    checkbox is about.
- The `cast` and `sqlite3` imports in `lfg_service/app.py` **stay**: `cast` is
  still used by `cast(_Handler, wrapper)` at the end of the `require_market`
  decorator, and `sqlite3` is used throughout the module.
- Note `row_attrs` reads `row["attributes_json"]` for characters and
  `row["slot"]`/`row["value"]` for traits; `mock_market` builds its attrs
  list inline instead of calling it, so only `attributes_match` needs
  updating there.
- No behavior change, no schema, no client change. Keep both names exported
  from `market_store` — do not move them to a new module.

### 2.2 Item 5 — non-positive character prices in browse

Today nothing stops a 0-drop character listing from being indexed and
surfaced:

- `market_ops.extract_created_sell_offer` accepts the Amount when
  `amount.isdigit()`, and `"0".isdigit()` is true. That function is the
  indexing gate for third-party offers too: `nft_listener._apply_offer_create`
  routes every streamed `NFTokenCreateOffer` through it before
  `market_store.upsert_listing`. `scripts/backfill_market.py` applies the same
  `isdigit()` test on its own sweep path.
- `market_store.browse` filters only on the caller's `min_amount_drops` /
  `max_amount_drops`, never on the row's own price.
- `market_store.listing_price` returns `Decimal(row["amount_drops"] or 0)`,
  so a 0-drop (or NULL-drops) character row sorts **first** under the default
  `price_asc` — it lands at the top of page 1 of the public browse.
- `market_ops.verify_sell_offer` would pass such an offer (it checks amount
  equality and the absence of a foreign `Destination`, not positivity), so
  in-app buy actually works on it.

The issue's evidence ("5 stray third-party 0-drop offers exist on testnet")
cannot be re-verified from this tree — the per-network `onchain_*.db` files
are gitignored and live on the deploy box, and characters now browse on
mainnet. Treat the count as unknown; the code path is real regardless.

If the product answer is **hide** (this spec's assumption): drop rows where
`amount_drops is None or amount_drops <= 0` inside `market_store.browse`,
character branch only. Trait rows are BRIX-denominated (`amount_brix`) and
must not be touched. Note this is *pre*-cache, not post-cache as the draft
claimed: `lfg_service/app.py::_compute_market_rows` calls `browse` and caches
its output. That is correct for an unconditional rule (it never keys the
cache) but wrong for anything user-parameterized — do not move a user filter
here by analogy.

`webapp/mock_market.py::browse` is a separate implementation used only under
`WEBAPP_DEV_MODE`; its seed rows carry no non-positive `amount_drops` on a
character (the `amount_drops: None` seeds are all `kind="trait"` rows priced
in `amount_brix`), so parity there is optional — say so in the commit rather
than silently diverging.

If the answer is **accept free transfers as a feature**, the item is a
one-line note in the issue plus (optionally) a `price: free` badge in
`market_pure.js::priceLabel`. Do not half-do it.

### 2.3 Item 6 — datalist for the trait value filter

`webapp/client/index.html` has `<input id="market-trait-value" type="text">`;
`webapp/client/app.js::loadMarketBrowse` reads it with `.value.trim()` and
turns `(slot, value)` into a `trait=Slot:Value` param. The server match is
exact and case-sensitive, so a typo yields a silent empty grid. The slot
control is already a `<select>`, populated by `ensureMarketTraitSlotOptions`
from `/api/nfts`'s `swappable_traits`.

Design: keep the free-text input, attach a `<datalist>` populated per slot.

- New pure helper in `webapp/client/market_pure.js`, next to the existing
  shop helpers: `catalogSlotValues(items) -> {slot: [value, ...]}`, sorted
  and deduped, tolerant of a null/empty argument (same defensive shape as
  `shopSlotCounts`).
- Source the rows from `GET /api/shop/catalog`, whose response is
  `{"items": [{slot, value, price_brix, image_url}, ...]}`
  (`lfg_service/app.py::handle_shop_catalog` → `lfg_core/shop.py::catalog`).
  `app.js` already caches those rows in `shopState.items` when the Shop panel
  loads; reuse them when present and fetch once lazily otherwise.
- **Coverage caveat, and why datalist beats `<select>` (open question 4):**
  the catalog lists only *shop-purchasable* traits — enabled, non-excluded
  `trait_rarity` rows, with `Body Type` dropped. A user can legitimately list
  a trait token whose value is excluded from the shop. A `<select>` would
  make that listing unfilterable; a datalist is a hint that still allows free
  typing. Choose the datalist.
- `<datalist id="market-trait-values">` in `index.html` (there is no
  `<datalist>` anywhere in that file today), `list=` on the value input,
  repopulated on `market-trait-slot` change. Bump `app.js?v=59` → `?v=60`;
  if `market_pure.js` changes (it does), also bump its `?v=23` in the
  `import` at the top of `app.js`.
- **The `app.js?v=` bump is asserted by two tests** that must move in the
  same commit or the gate goes red:
  `tests/test_harvest_pure_js.py::test_index_html_cache_buster_bumped` and
  `tests/test_app_js_deeplink.py::test_cache_busters_bumped` both assert the
  literal string `"app.js?v=59"`. Nothing pins `market_pure.js?v=23`
  literally — `tests/test_market_panel_dom.py` asserts only
  `"from './market_pure.js"` — so that bump is free.

### 2.4 Item 1 residue — delete the stale split-topology prose

No code. Five sites still describe the split as the deployed topology:
`CLAUDE.md` ("Per-kind network seam"), the docstrings of
`lfg_service/app.py::_market_network` **and** `lfg_service/app.py::
_compute_mine_data`, and comments in `tests/test_market_api.py` (the
"Split-network topology" section header block) and
`tests/test_config_economy_validate.py`
(`test_boot_ok_when_disabled_and_networks_mismatch`).

Rewrite them to the truth: the seam is **defensive**, kept because
`ECONOMY_NETWORK` and `XRPL_NETWORK` are separate knobs and a trait read
resolved on the wrong one returns empty for every user — not because any
stack runs them apart. Keep the "do not simplify to one network" warning; it
is still the right instruction, just for a different reason. Do not delete
`_market_network`, and do not relax `validate_economy_config`.

The `tests/test_config_economy_validate.py` comment is the mildest of the
five — it describes what the *economy-disabled* posture looks like, and that
posture is still legal (`validate_economy_config` only fires when
`ECONOMY_ENABLED` is on). Drop the "still" and the present tense; leave the
test itself alone.

## 3. Descoped, with the counter-arguments spelled out

**Item 1 — per-network trait on-ledger ops.** A real fix threads a
per-network XRPL client *and* signing wallet through
`xrpl_ops`/`market_ops`/`economy_flow`. Every `xrpl_ops` entry point builds
its client and wallet per call straight from the module-level
`config.JSON_RPC_URL` / `config.SEED` constants — 19 literal
`JsonRpcClient(config.JSON_RPC_URL)` and 8 `Wallet.from_seed(config.SEED)`
sites in that one module — and those constants are frozen at import from one
`XRPL_NETWORK` and one
`SEED`. The payoff is a topology that no stack can run: with
`ECONOMY_ENABLED` on, `validate_economy_config` refuses the split at import.
Close won't-fix.

**Item 2 — detach settlement from the buy poll.** The inline `await
_settle_trait_sale(...)` lives in the `outcome == "sold"` branch of
`lfg_service/app.py::_advance_market_session` — the shared advance step that
`handle_market_buy_status` (one of five handlers produced by
`_make_market_status_handler`) drives, not in the handler body itself. It
carries a comment from PR #129 (merged 2026-07-06; `git log -S` traces it to
commit d298fc1) stating the await is deliberate: "run_deposit's own
fail-closed/journaling guarantees mean there is nothing to gain from
detaching it, and awaiting keeps the outcome deterministic for both callers
and tests". Re-reviewing that call:

- The latency is real (an on-ledger owner verify, an issuer burn and a
  Closet `NFTokenModify`), but nothing currently breaks under it. The client
  polls on a 3 s `setTimeout` chain (`pollMarketFlow`) and `api()` sets no
  `AbortController`/timeout, so a slow poll stretches a spinner rather than
  failing. `handler_cancellation` is not enabled anywhere in the tree, so a
  disconnecting client does not abort the settlement either.
- Detaching would make `marketBuyRender`'s trait `done` copy — "Sold — added
  to your Closet." — false at the moment it is shown. Doing it honestly means
  surfacing `settled` on the buy session/status response, a two-phase render
  ("Purchased — moving it into your Closet…" → "added to your Closet"), and a
  cache-buster bump. That is a different, larger change than the draft's
  one-line spawn.
- It would also rewrite `tests/test_market_api.py::
  test_buy_status_trait_purchase_triggers_settlement_seam`, which asserts on
  the settlement call synchronously after the handler returns.

Recommend **won't-fix as specified**. Revisit only with measured poll
durations from prod, and then ship the two-phase UI in the same PR.

**Item 3 — durable sweep attempt counter.** `_sweep_attempts` and
`_shop_settle_attempts` in `lfg_service/app.py` are in-memory by explicit
design; the `_sweep_attempts` comment (PR #129, commit d298fc1, confirmed
with `git log -S`) reads: "a durable counter buys nothing but a crash-loop
that could exhaust the budget in seconds instead of ~10 minutes."
`_shop_settle_attempts` carries "same rationale as `_sweep_attempts`". That
is correct and is the whole argument. Persisting the count makes the failure
mode strictly worse in the one scenario that matters (a wedged process
restarting repeatedly), in exchange for less log noise. Cost is not trivial
either: a self-migrating column in `market_store.init_db` (whose NOT-NULL
rebuild path copies an explicit `column_list`), a matching `_MIGRATIONS`
entry in `lfg_core/shop_store.py`, seven direct `_sweep_attempts` references
in `tests/test_market_trait_flow.py` (five assertions plus two fixture
`.clear()` calls), and the `_shop_settle_attempts` touchpoints in
`tests/test_shop_sweep.py` and `tests/test_shop_conservation.py`. Drop it.

If a maintainer still wants durability, the only version worth building
stores `(attempts, last_attempt_ts)` and rate-limits by elapsed time rather
than by count — which is a different design, not this checkbox.

## 4. Constraints

- **SourceTag + memos.** Nothing here builds or alters a transaction.
- **Per-kind network seam.** 0-drop is a *character* concern and resolves on
  `XRPL_NETWORK`; the datalist's catalog resolves on `ECONOMY_NETWORK`
  (`handle_shop_catalog` uses `config.ECONOMY_NETWORK`). They happen to be
  equal in both deployed stacks today; do not hard-code that.
- **`handle_shop_catalog` returns `{"items": []}` when `ECONOMY_ENABLED` is
  off.** The datalist must degrade to today's plain free-text input, not
  throw.
- **Browse cache.** `_MARKET_CACHE` stores `_compute_market_rows`' output,
  i.e. `market_store.browse`'s rows. Unconditional rules may live in
  `browse`; anything the caller varies must stay in the handler's post-cache
  filter chain.
- **No-build client.** Any `app.js` edit bumps `app.js?v=` in
  `webapp/client/index.html`; any `market_pure.js` edit bumps the `?v=` on
  its `import` inside `app.js` as well. The `app.js?v=` value is pinned as a
  literal in two tests (see §2.3) — update them in the same commit.
- **Test env isolation (#323).** New test files that import `lfg_core` need
  no env-guard preamble any more — the root `conftest.py` supplies the
  required vars. `tests/test_market_pure_js.py` imports no `lfg_core` at all
  and skips when `node` is absent.

## 5. Open questions for the maintainer

1. **Item 5 — hide or accept?** This spec assumes hide (non-positive
   `amount_drops` on character rows). Accepting free transfers as a feature
   is defensible; say which, because "leave it ambiguous" is what keeps the
   checkbox open.
2. **Item 2** — confirm won't-fix-as-specified, or fund the two-phase UI.
3. **Item 3** — confirm drop.
4. **Item 4** — confirm the rename lands in `market_store` (not a new
   `market_attrs` module) so the diff stays a rename.

## 6. Testing

- **Item 4:** no new behavior test. `tests/` currently references neither
  private name (grep for `_attributes_match` / `_row_attrs` hits only
  `lfg_core/market_store.py`, `lfg_service/app.py` and
  `webapp/mock_market.py`), so the rename is caught by the suite only through
  the call sites; add one assertion in `tests/test_market_store.py` that
  `market_store.row_attrs` accepts a plain `dict` for both kinds (the point
  of dropping the cast). mypy is the other half of the gate here — see the
  `Mapping[str, Any]` warning in §2.1; run it before assuming the retype is
  free.
- **Item 5:** extend `tests/test_market_store.py`'s browse cases (they live
  in the `TestBrowseCharacters` / `TestBrowseTraits` /
  `TestBrowseAmountAndSortAndPaging` / `TestBrowseBrix` classes; no
  individual test name contains "browse") — seed live
  character rows with `amount_drops` of `0`, `None` and `1_000_000` plus a
  live BRIX trait row; assert `browse(kind='character')` returns only the
  positive row and `browse(kind='trait')` is unaffected. Add one case pinning
  that the excluded row is gone from the *default* `price_asc` ordering (it
  used to sort first).
- **Item 6:** add `catalogSlotValues` cases to `tests/test_market_pure_js.py`
  (grouping, sort, dedupe, empty/null input) in the same style as the
  existing `filterShopItems` / `shopSlotCounts` cases. Client wiring is
  eyeballed in a `WEBAPP_DEV_MODE=1` load.
- **Item 1 residue:** none — prose only. `scripts/check_repo_layout.py` does
  not cover this text.
- Full pre-push gate (ruff, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config, check-repo-layout,
  audit-layer-dimensions) green; never `--no-verify`.
