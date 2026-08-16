# Trait Shop XRP Fallback — Implementation Plan (#238)

**Spec:** `docs/superpowers/specs/2026-07-16-shop-xrp-fallback-design.md`

TDD throughout: each task writes failing tests first against the seams
(`ShopDeps` fakes, in-memory SQLite), then implements.

## Task 1 — Shared payment-path detection helper
- Extract `detect_swap_payment`'s balance-check + AMM-quote + buffer logic into a shared
  helper (`lfg_core/brix_payment.py::detect_payment_path(wallet, brix_amount) ->
  ("BRIX"|"XRP", amount)`), parameterized on currency/issuer (defaults from config).
- Re-point `swap_flow.detect_swap_payment` at it (thin wrapper; identical behavior).
- Tests: BRIX-sufficient → BRIX; insufficient/None balance → XRP with buffer + ROUND_UP
  6dp; AMM quote None → raises. Existing swap tests stay green.

## Task 2 — Store migration
- `shop_store`: add `pay_with`, `price_xrp`, `buyback_done` columns (self-migrating
  ALTER on open, pattern from `identities.user_token`). `create_order`/`update_order`
  accept the new fields.
- Tests: migration on a pre-existing DB file; defaults (NULL/0); round-trip.

## Task 3 — XRP-path offer in `start_shop_buy`
- Detect path before minting (fail fast pre-mint on unquotable AMM).
- XRP path: offer `amount = xrp_to_drops(price_xrp)`; session + order record
  `pay_with`/`price_xrp`. BRIX path byte-identical to today.
- Tests (ShopDeps fakes): offer amount shape per path; pre-mint failure on quote-None
  (no mint, no supply row, order `failed`); revert path unchanged.

## Task 4 — Post-settlement buyback
- On settle success in `advance_shop_buy` AND in the sweep's settlement retry: if
  `pay_with=="XRP"` and not `buyback_done`, call
  `buy_and_burn(price_brix, max_xrp=price_xrp)` best-effort, then set `buyback_done=1`
  (set it even on a logged failure — single attempt, matching swap's posture).
- Tests: buyback called with correct args on XRP path only; not on BRIX; exactly once
  across poll+sweep; buyback exception doesn't fail the order.

## Task 5 — Service + Activity surface
- `/api/shop/buy` + status responses include `pay_with`/`price_xrp`; Activity shop UI
  shows the XRP-equivalent price on the XRP path.
- Tests: webapp smoke asserts the fields; existing shop endpoint tests green.

## Task 6 — Sweep expiry on XRP path + docs
- Test: expired XRP-path `pending_accept` order cancels/burns/reverses supply exactly
  like BRIX (no buyback fired). Rescue branch settles then owes buyback.
- CLAUDE.md Trait Shop section: note the XRP fallback + buyback.

## Gate
Full pre-push gate (ruff/mypy/gitleaks/pytest/validate-trait-config) green; PR
(non-draft) referencing #238; both bots triaged before merge.
