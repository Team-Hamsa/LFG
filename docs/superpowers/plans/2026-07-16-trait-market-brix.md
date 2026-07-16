# BRIX Trait Listings + XRP On-Ramp — Implementation Plan (#239)

**Spec:** `docs/superpowers/specs/2026-07-16-trait-market-brix-design.md`

TDD throughout. Suggested order (Task 1 of #238 — the shared `detect_payment_path`
helper — lands first if both efforts run; otherwise include it here).

## Task 1 — `market_ops` per-kind amounts
- Parameterize `extract_created_sell_offer` / `verify_sell_offer` on expected currency
  (`"xrp"` | `"brix"`); BRIX branch validates currency+issuer match and Decimal value
  bounds (positive, 6dp, cap). Character callers pass `"xrp"` — behavior unchanged.
- Tests: BRIX dict accepted/normalized; wrong issuer/currency rejected; XRP offer
  rejected for `expect="brix"`; all existing character tests green untouched.

## Task 2 — `market_store` migration + API shape
- Add `amount_brix` (self-migrating ALTER); upsert asserts exactly-one-of
  drops/brix; browse/mine/history joins return the right field per kind.
- `min_brix`/`max_brix` filters for trait browse (post-cache, like the XRP filters).
- Tests: migration on existing DB; upsert invariant; filter math; cache not invalidated
  by filters.

## Task 3 — List/Cancel in BRIX
- `ListSession(kind=trait)` carries `amount_brix`; sell-offer txjson Amount = BRIX dict;
  `advance_list_session` extracts via `expect="brix"`. TraitSellSession inherits.
- Tests: payload shape; finalize writes `amount_brix` row; cancel unchanged.

## Task 4 — Buy holder path
- BRIX-aware `verify_sell_offer` wiring in `POST /api/market/buy`; settlement untouched.
- Tests: holder buy end-to-end with fakes (verify → accept → sold → settled).

## Task 5 — Buy on-ramp path
- `detect_payment_path` at buy start; `AWAITING_ONRAMP` state in `BuySession`;
  `xumm_ops.create_onramp_payment_payload` (self-Payment, Amount=BRIX dict,
  SendMax=quoted XRP, SourceTag+memos, optional user_token push).
- Poll: signed + validated `tesSUCCESS` → re-verify offer → build accept payload →
  normal path. Trustline-missing → 409 `trustline_required` (Activity drives the
  existing TrustSet flow first).
- Tests: path detection; on-ramp payload shape (SendMax, self-destination, SourceTag,
  memo action); abandon-after-onramp leaves listing live and session cancellable;
  signer==buyer still enforced on the accept; quote-unavailable → clean 4xx before any
  payload.

## Task 6 — Listener + backfill
- `apply_market_tx` offer_create: per-kind currency acceptance → correct amount column;
  `backfill_market.py` same rule + one-shot stale-close of legacy XRP trait listings.
- Tests: listener upserts BRIX trait offer / rejects XRP trait offer; backfill re-run
  idempotent + timestamp-preserving (existing property) with BRIX rows.

## Task 7 — Activity UI + docs
- Trait prices/badges in BRIX; list wizard BRIX input; buy modal renders the two-step
  on-ramp ("Get BRIX (~N XRP)" → "Confirm purchase"); `pay_with`/`price_xrp_quote` from
  status polls. Webapp smoke tests.
- CLAUDE.md marketplace section: per-kind denomination + on-ramp.

## Gate
Full pre-push gate green; PR (non-draft) referencing #239; Greptile + CodeRabbit
findings triaged before merge. Post-merge testnet smoke: list a trait in BRIX, buy it
from a zero-BRIX wallet via the on-ramp, confirm settlement + listener rows.
