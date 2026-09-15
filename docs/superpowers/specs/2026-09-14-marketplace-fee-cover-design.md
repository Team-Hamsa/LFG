# Marketplace fee cover — LFG refunds the external broker's fee on in-app buys

**Status:** proposed
**Date:** 2026-09-14
**Supersedes (for now):** #428's seller-side royalty refund — deferred, see
"Why not the seller refund"
**Depends on:** #426 external Buy-now (implemented), #283 native bids
(implemented), `lfg_core/brokers.py` measured `broker_rate`
**Companion:** `2026-09-14-marketplace-cross-list-design.md` (supply side,
blocked on Batch #219)

## Problem

The roadmap item: *"Incentivize marketplace activity by refunding a % out of
royalties. Set broker fee to 0; make adjustable in admin panel."* The proposal
was 1.589% of the sale back to the buyer and 1.589% to the seller, out of the 7%
`TransferFee` royalty.

Before building it we measured whether it would work. It mostly would not, but
one narrow version does. This spec records the evidence and then specifies that
version.

## Efficacy probe (2026-09-14)

Source: `~/LFG/history_mainnet.db` + `onchain_mainnet.db`, opened read-only.
The window is the 90 days to 2026-09-13. Scope is character NFTs.

| | |
|---|---|
| Third-party XRP sales | 197 sales, 1,110 XRP. **All 197 settled by xrp.cafe's bot** |
| Royalty received by the issuer | ~76 XRP |
| Median sale | ~5 XRP |
| Participants | 59 sellers, 24 buyers. One buyer made 94 of the sales |
| Live character listings | 370 on cafe (21.8k XRP of asks), 9 on bidds / Art Dept, **1** plain LFG listing |
| Cafe sellers who are LFG wallets | 76 of 108, holding 283 of the 370 listings |
| Buy-now (#426) fills from the app | 9 fills, 61 XRP. 7 of them from `rHaMsA…` (team) |
| Trait (BRIX) sales | 89, all with the issuer as seller (primary sales, no royalty). **Zero trades between users** |

### Who pays the broker fee

Measured over all 1,197 cafe-brokered XRP sales of our characters, from
balance deltas in the validated tx metadata:

- The seller's XRP delta was **at least 93.0% of the ask in every sale**
  (median exactly 93.0%).
- The buyer paid a median **101.589%** of the ask.
- Royalty was exactly 7% of `(buy amount - broker fee)` at the median.

So cafe's fee is paid on the **buyer's** side. Sellers net 93% on cafe and 93%
through LFG. #428's premise, "cafe sellers net ~91.4%", was wrong and has been
corrected in that spec and issue.

### What that means for a refund

1. **The royalty is venue-agnostic.** The issuer collects 7% on cafe sales
   too. Moving a sale that would have happened anyway from cafe to LFG earns
   zero additional royalty, so a refund on it is pure cost.
   - Let *k* be the total refund rate: 3.178% for the proposal, 7% for #428.
   - To pay for itself, the refund must create brand-new volume *N* with
     `0.07·N ≥ k·(E + N)`, where *E* is the refunded volume that would have
     happened anyway.
   - At *k* = 3.178% that requires **N ≥ 0.83·E**. At *k* = 7% it never pays
     back.
2. **The per-sale amount is imperceptible.** 1.589% of a 5 XRP sale is about
   0.08 XRP per side, which will not move anyone off the venue where all the
   buyers are.
3. **The binding constraint is in-app supply, not price.** LFG already charges
   buyers ~1.6% less than cafe and pays sellers the same. It has 1 listing
   against cafe's 370.
4. **The absolute cost is small.** If all 197 sales had gone through the app
   with both refunds, the total would have been ~35 XRP for the quarter.
   Downside risk is low; the case against it is that it would not work, not
   that it is expensive.

### What *does* have leverage

The demand side is small and concentrated: 24 buyers, one of them making nearly
half the purchases. Those buyers can already buy cafe listings from inside LFG
via Buy-now (#426), at a price 1.589% over the ask. If LFG refunds that fee,
**the app becomes the cheapest way to buy a cafe listing**:

- The buyer effectively pays the ask.
- The royalty still arrives, because cafe settles the sale either way.
- The cost is bounded by the broker fee on sales we route, ≤ ~18 XRP/quarter
  even if every sale moved.
- No supply migration is needed.

That is this spec. Supply import (cross-listing) is the companion spec.

### Why not the seller refund (#428 / the 1.589% seller half)

The seller already nets the same on both venues. A seller refund only matters
once sellers list here, and the cross-listing that would bring them is blocked
on Batch. Refunding an in-app sale that doesn't exist yet is a no-op. Deferred
until cross-listing ships.

### Mapping to the roadmap wording

- **"Set broker fee to 0."** In-app sales are direct accepts: the buyer accepts
  the seller's offer with no intermediary, so there is no broker and no fee.
  LFG's broker fee is already structurally 0 and there is nothing to set.
  - A real LFG broker fee would require LFG to run its own brokered matching
    bot (settling plain listings against qualifying bids with its own
    `NFTokenBrokerFee`).
  - That is out of scope here: one match of that kind happened in 90 days.
- **"Make adjustable in admin panel."** The adjustable number is the **coverage
  %** of the external broker's fee that LFG refunds, together with the
  campaign's budget and caps, all in Discord `/admin`.

## Design

### Summary

When a buyer places a bid **through LFG** on a character that is **listed on
an allowlisted broker with a measured `broker_rate`**, and the bid is at or
above that listing's clearing price, LFG records a **fee-cover promise** and
reserves budget for it. When the broker's bot settles the sale, LFG pays the
buyer an **automatic XRP refund** from the issuer.

- The refund is bounded by the promise, the broker fee actually observed in the
  validated accept, and half the royalty actually observed in that accept.
- Stopping the campaign blocks new promises and honors open ones.

### Lifecycle

```
POST /api/market/bid ──► quote (read-only: campaign active? budget + wallet cap headroom?)
        │                      returned as session.fee_cover = {state:"quoted", drops}
        ▼
bid validates on-ledger (advance_bid_session → _write_bid_row; status poll or sweep)
        │
        ▼
promise: INSERT fee_cover_promises (PK offer_index), reserve drops   ── or declined(reason)
        │
        ├── bid cancelled / expires unfilled ──► promise released (budget freed)
        ▼
broker's bot brokers the accept (listener closes buy_offers row 'accepted')
        │   detected by: bid status poll (primary) · settlement sweep (backstop)
        ▼
resolve accept tx (history archive) ─► re-fetch validated tx (xrpl_ops.get_tx)
        │
        ▼
refund row: INSERT fee_cover_refunds (PK accept_tx_hash, UNIQUE offer_index)
        state owed ──► submitted ──► confirmed | failed        (or declined(reason))
```

### Promise eligibility (evaluated at quote and again at promise)

A promise is made only when **all** of these hold:

1. **A campaign is active.** Status is `active` and `now < ends_at` when set.
2. **At quote time, the NFT has a live `market_listings` row whose
   `destination` resolves via `brokers.resolve`** to an allowlisted broker
   with `broker_rate is not None`, and whose `seller` is the NFT's current
   owner-of-record.
   - The quote saves that listing on the session
     (`BidSession.fee_cover_listing`, server-side only).
   - **At promise time the same row must still be live, or closed `sold`
     with `buyer == bidder`.** The broker's bot can fill a clearing bid within
     seconds, and the listener can close the listing `sold` before the
     session's first `done` poll; re-reading only live listings would erase
     the promise the buyer was just shown. The listing is rebuilt from that
     row, so its broker must still resolve with a measured rate and its seller
     must still be the bid-time owner.
   - Sold to anyone else, cancelled, stale or missing → no promise (an
     ordinary bid).
   - A bid with no saved listing (no quote, e.g. no campaign at quote time)
     falls back to the live lookup.
3. **`bid_drops >= brokers.clearing_drops(ask_drops, rate)`.** This is the bid
   the broker's bot will fill now. A below-clearing bid is an ordinary bid: if
   it fills later, the buyer paid only what they bid, so there is no "extra" to
   cover.
4. **`bid_drops >= min_bid_drops`.**
5. **The bidder is not a system wallet.** The system set is
   `sponsored_mint.excluded_wallets()` ∪ `system_wallets.with_durable(...)` ∪
   `{BRIX_DISTRIBUTOR_ADDRESS}`.
6. **Budget headroom:** `committed + promised_drops ≤ budget_drops`.
   - `committed` = Σ open promises + Σ refunds in
     `owed | submitted | confirmed`, for this campaign.
7. **Wallet headroom:** Σ (open promises + refunds) for the bidder across all
   campaigns in the trailing `wallet_window_seconds` stays
   `≤ wallet_cap_drops`.

`promised_drops = floor(ceil(bid_drops × rate) × coverage_bps / 10000)`.
`coverage_bps` and `rate` are **snapshotted into the promise**, so a later
admin edit never changes a promise already made.

**Quote vs promise.** The quote at `POST /api/market/bid` is advisory and
reserves nothing. The promise is written only once the bid is on-ledger, when
`offer_index` exists. If headroom disappears between the two (a small window,
at most the payload lifetime), the promise is declined `budget_exhausted` or
`wallet_cap` and the status response says so. The client never shows
"covered" once a promise is declined.

### Refund computation (from the validated accept, never from the price)

Given an accept tx `T` resolved for promise `P`:

1. **Tx validity.** `T` is validated, `tesSUCCESS`, `NFTokenAcceptOffer`, and
   in **brokered mode**: both `NFTokenBuyOffer` and `NFTokenSellOffer` are set.
   - `NFTokenBuyOffer == P.offer_index`.
   - The deleted buy offer's `Owner == P.bidder`.
   - `T.Account` (the broker) is allowlisted with a measured rate.
   - Otherwise the refund is declined `not_broker_settled`.
2. **Observed fee.** `fee = int(T.NFTokenBrokerFee)` in XRP drops. A missing or
   IOU fee is declined `fee_unobserved`.
3. **Observed royalty.** `royalty` is the issuer `AccountRoot` balance delta in
   `T.meta`. The issuer is the NFT issuer decoded from `NFTokenID`, and must
   equal `config.SIGNING_ACCOUNT`.
   - If the seller or buyer *is* the issuer, the refund is declined
     `issuer_party`.
   - If the delta is absent or ≤ 0, the refund is declined
     `royalty_unobserved` and parked for a human.
4. **Linked counterparty.** The seller is the deleted sell offer's `Owner`.
   The refund is declined `linked_counterparty` when either holds:
   - `identity.bucket_for_wallet(seller)` contains the bidder.
   - Both wallets have the same non-exchange funder in `wallet_funders`
     (#461 rule; `funding.EXCHANGES`-funded wallets never match).
   - A missing funder row counts as **not linked** (fail-open). The payout
     ceiling below makes a missed link cost at most a sub-royalty refund.
5. **Amount.**
   `refund = min(P.promised_drops, floor(fee × P.coverage_bps / 10000),
   floor(royalty × ROYALTY_CEILING_BPS / 10000))`.
   - `ROYALTY_CEILING_BPS = 5000` is a **code constant, not an admin knob.**
     It keeps every refund strictly below the royalty received on the same
     sale, so no pattern of trades can make LFG a net payer. That includes
     self-dealing through a fake broker account, where `fee` can be set to the
     whole spread.
   - A result below 1 drop is declined `zero_refund`.

The promise closes `filled` with `accept_tx_hash` in the same transaction
that inserts the refund row. Any unspent remainder (`promised_drops - refund`)
is released implicitly, because `committed` counts the refund row, not the
promise.

### Durability, ordering, recovery (copied from `brix_drip` claims)

- **Money state lives in the app DB** (`db_path.app_db_path(network)`), never
  in the droppable `onchain_<net>.db` / `history_<net>.db`.
- **`fee_cover_refunds.accept_tx_hash` is the PRIMARY KEY** and
  `offer_index` is `UNIQUE`. Sqlite makes a second payout for one sale
  impossible, whether from a replay, a double poll, or sweep + poll racing.
- **Order: record `owed` → claim → submit → record outcome.** The claim path
  is the template (`app.py::_claim_one_wallet`):
  - **The claim is one conditional write.** `UPDATE … SET
    state='submitted', last_ledger_seq=<provisional> WHERE accept_tx_hash=?
    AND state='owed'`. Only the caller whose update changes a row submits, so
    the poll path and the sweep can race without paying twice.
  - The provisional deadline (`current + FEE_COVER_LEDGER_MARGIN × 10`) is
    recorded in that same write, so no `submitted` row ever lacks a deadline.
  - Submission goes through `xrpl_ops.send_fee_cover_refund(dest, drops,
    refund_key, max_last_ledger_seq)`, modelled on `send_brix_claim` and
    returning its `ClaimPayment` tri-state.
- **Outcomes:**
  - `confirmed` on validated `tesSUCCESS`.
  - `failed` (reason `payout_failed`) on a **definitive** failure: a validated
    non-success result, or a presubmit `simulate` rejection.
    `_submit_and_confirm` logs the result code but does not return it.
  - `ClaimNotSubmitted` → back to `owed`, and the pay attempt reports
    `deferred`. `send_fee_cover_refund` raises it for **every** pre-flight
    step before `_submit_and_confirm` (drops check, signing wallet, the
    validated-ledger read raising or returning nothing, `Payment`
    construction), chaining the original error: nothing reached the ledger,
    so it must never park a user-visible `failed`. An unreadable validated
    ledger in `pay_refund` itself (None or raising) also defers without
    claiming.
  - An unknown outcome stays `submitted` with the payment's own
    `last_ledger_seq`.
- **An indeterminate outcome is never retried blind.** Recovery resolves it.
- **Recovery** is `lfg_core/fee_cover.recover(...)`, run at service startup
  (like `_start_brix_claim_recovery`) and by
  `scripts/recover_fee_cover_refunds.py`.
  - It pages `config.SIGNING_ACCOUNT`'s `account_tx` from
    `last_ledger_seq - 5000`.
  - It matches the refund memo tag `lfg:fee_cover:<accept_tx_hash>` and
    requires a genuine payout: validated, `tesSUCCESS`, `Payment`,
    `Account == SIGNING_ACCOUNT`, `Destination == bidder`, XRP
    `delivered_amount == refund_drops`.
  - Found → `confirmed`. Absent **and** validated ledger past
    `last_ledger_seq` → `failed` with reason **`payout_expired`**. Anything
    else stays untouched.
- **A `failed` row is parked, never retried automatically**, whether its
  reason is `payout_failed` or `payout_expired`. Typical causes:
  - a destination property (`tecDST_TAG_NEEDED`, DepositAuth `tecNO_PERMISSION`)
  - an issuer short of spendable XRP (`tecUNFUNDED_PAYMENT`)

  A failed payment can never validate later, so resubmitting it is safe. An
  operator fixes the cause (tops up the issuer, or accepts that a destination
  cannot receive), then runs `scripts/recover_fee_cover_refunds.py --requeue
  <accept_tx_hash>`. That moves `failed → owed` only while the campaign budget
  still has headroom for it. The result code is in the service log line from
  `_submit_and_confirm`.

### Payout transaction

- `Payment`, XRP drops, `Account = config.SIGNING_ACCOUNT` set **explicitly**
  (mainnet: the issuer via regular key), `Destination = bidder`.
- `SourceTag = config.SOURCE_TAG`.
- `LastLedgerSequence = current + FEE_COVER_LEDGER_MARGIN` (new env, default
  40), clamped to the provisional deadline.
- Submitted through `_submit_and_confirm`, so presubmit `simulate` and the
  per-account submission lock apply. The issuer also signs mints; the lock
  keeps sequences serialized.
- Memos:
  - `build_memo_models(BACKEND, BACKEND, ACTION_FEE_COVER,
    campaign=f"fee-cover-{campaign_id}")`, adding **`fee-cover`** to the closed
    `memos._ACTIONS` enum.
  - Plus a `Memo(memo_data=hex("lfg:fee_cover:<accept_tx_hash>"))` recovery
    tag, like `claim_memo_tag`.

### Fill detection and promise release

- **Promise write.** The promise is written when the bid session finalizes:
  on the client's status poll, or — when the client stopped polling before
  the bid validated — by `sweep_fee_cover()`, which first advances every
  in-memory bid session that is not terminal and whose `fee_cover.state` is
  `quoted`. Sessions live only in memory, so a bid whose session is lost to a
  service restart before validation stays uncovered.
- **Primary trigger.** The bid status poll. In `_advance_market_session`'s
  `bid` branch, after computing `session.fill`: when `fill == "accepted"` and a
  promise exists, call `fee_cover.settle_promise(network, offer_index)`. This
  is idempotent via the PKs. The status response gains
  `fee_cover: {state, drops, reason, payout_tx_hash}`.
- **Backstop.** `sweep_fee_cover()` runs inside `_settlement_sweep_loop`, in its
  own try/except like the trait/shop sweeps.
  - `_start_settlement_sweep` currently starts the loop only when
    `ECONOMY_ENABLED or wc_enabled()`. It becomes **unconditional**.
    - The economy sweeps keep their `ECONOMY_ENABLED` guard inside the loop.
    - `sweep_sign_requests()` stays as it is today: already unguarded inside
      the loop, and a no-op with no open `sign_requests` rows. The plan
      confirms that no-op before flipping the gate.
    - A campaign started after boot on a stack with both flags off still
      settles.
  - `sweep_fee_cover()` is a single indexed query when nothing is open.

  For **every open promise**. The `buy_offers` index is not a usable
  trigger: an expired offer stays on-ledger, and in the index as live, until
  someone cancels it.
  - **Accept found** in the history archive (`nft_events` sale for
    `P.nft_id` at `ts ≥ P.created_at - 3600` whose `xrpl_txs.raw_json`
    `NFTokenBuyOffer == P.offer_index`) → `settle_promise`.
  - **Cancel found** (an `offer_cancel` event for `P.nft_id` whose
    comma-joined `offer_index` contains `P.offer_index`) → release
    `cancelled`.
  - **Neither, and the bid has expired**, and the archive covers the expiry
    (baseline complete, no continuity gap, `validated_close_time` past
    `bid_expiration + 946684800`, the same test as `epoch_state.certify_epoch`)
    → release `expired`. An expired offer can never be accepted, so an archive
    that has seen past the expiry without an accept proves none happened.
  - **Otherwise** leave open. It fails closed: never released early, never
    paid without a tx. On an archive without a certified baseline (staging,
    typically), expired promises stay open, holding their budget, until it is
    certified.
- **Retry of `owed` rows.** A row with retryable state is retried each sweep
  with an in-memory attempt counter. A `deferred` pay attempt (nothing reached
  the ledger) does not count as an attempt. Each promise, each owed row and
  the recovery call run in their own `try/except`, so one bad row never stops
  the sweep — it is the only payer. After `_SWEEP_MAX_ATTEMPTS` it is
  journaled to `ECONOMY_RECORDS_DIR/fee-cover-giveup-<accept_tx_hash>.json`,
  following the `settle_pending_trait_sales` pattern. The row stays `owed` for
  a human.
- Why the archive and not `buy_offers`: the listener's `close_bid` stores no
  accept hash, and a listener outage makes `backfill_market.py` close the row
  `stale`, not `accepted`. The history archive self-heals via the #402 auto
  catch-up and holds the raw accept.

### Schema (app DB, per network; self-creating like `sponsored_mint`)

```sql
CREATE TABLE IF NOT EXISTS fee_cover_campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  network TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','stopped')),
  coverage_bps INTEGER NOT NULL CHECK (coverage_bps BETWEEN 0 AND 10000),
  budget_drops INTEGER NOT NULL CHECK (budget_drops > 0),
  wallet_cap_drops INTEGER NOT NULL,
  wallet_window_seconds INTEGER NOT NULL DEFAULT 2592000,
  min_bid_drops INTEGER NOT NULL DEFAULT 1000000,
  started_at INTEGER NOT NULL, started_by TEXT NOT NULL,
  ends_at INTEGER, stopped_at INTEGER, stopped_by TEXT
);
-- at most one active campaign per network
CREATE UNIQUE INDEX IF NOT EXISTS idx_fee_cover_one_active
  ON fee_cover_campaigns(network) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS fee_cover_promises (
  offer_index TEXT PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES fee_cover_campaigns(id),
  network TEXT NOT NULL,
  nft_id TEXT NOT NULL, bidder TEXT NOT NULL,
  bid_drops INTEGER NOT NULL, ask_drops INTEGER NOT NULL,
  broker TEXT NOT NULL, broker_rate REAL NOT NULL,
  coverage_bps INTEGER NOT NULL, promised_drops INTEGER NOT NULL,
  bid_expiration INTEGER,              -- Ripple epoch, from the bid
  state TEXT NOT NULL CHECK (state IN ('open','filled','released','declined')),
  reason TEXT,                          -- declined/released reason
  accept_tx_hash TEXT,
  created_at INTEGER NOT NULL, closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS fee_cover_refunds (
  accept_tx_hash TEXT PRIMARY KEY,
  offer_index TEXT NOT NULL UNIQUE REFERENCES fee_cover_promises(offer_index),
  campaign_id INTEGER NOT NULL,
  network TEXT NOT NULL,
  bidder TEXT NOT NULL,
  seller TEXT, broker TEXT,             -- NULL when a declined accept had no parsable offer/broker
  observed_fee_drops INTEGER, observed_royalty_drops INTEGER,
  refund_drops INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK (state IN ('owed','submitted','confirmed','failed','declined')),
  reason TEXT,
  payout_tx_hash TEXT, last_ledger_seq INTEGER,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fee_cover_audit (       -- same shape as free_mint_audit
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  network TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  at INTEGER NOT NULL, campaign_id INTEGER, result TEXT NOT NULL, details TEXT
);
```

**Declined promises are recorded** (`state='declined'`, reason) whenever the
buyer could have seen "LFG covers the fee": a campaign was live **and** the NFT
had a qualifying external listing. So `below_clearing`, `below_min_bid`,
`system_wallet`, `budget_exhausted` and `wallet_cap` are recorded, and every
"not covered" the UI shows can be explained later. `campaign_inactive` and
`not_external_listing` write nothing; those are ordinary bids.

**Decline and release reasons** (closed set):

- Promise-level: `campaign_inactive`, `not_external_listing`,
  `below_clearing`, `below_min_bid`, `system_wallet`, `budget_exhausted`,
  `wallet_cap`.
- Refund-level: `not_broker_settled`, `fee_unobserved`, `issuer_party`,
  `royalty_unobserved`, `linked_counterparty`, `zero_refund`.
- Release: `cancelled`, `expired`.

### Admin panel (Discord `/admin`, the sponsored-mint pattern)

- **Service endpoints**, all `@require_service_token` +
  `_require_discord_surface`, actor `discord:<user id>`:
  - `GET /api/admin/fee-cover/status`
  - `POST /api/admin/fee-cover/start` with body `{coverage_pct,
    budget_xrp, wallet_cap_xrp, min_bid_xrp, duration_hours?}`
  - `POST /api/admin/fee-cover/stop`
  - `POST /api/admin/fee-cover/update` with the same fields minus duration,
    applying only to *new* promises
- **Storage writes** use `BEGIN IMMEDIATE`. Every call writes a
  `fee_cover_audit` row, including no-ops such as `already_active`, as
  `sponsored_mint.start_campaign` does.
- **`surfaces/_client/client.py`** gains `fee_cover_status/start/stop/update`.
- **`AdminView`** gains **Fee cover: Start / Update / Stop / Refresh**.
  - Start and Update open a modal. Defaults: coverage 100%, budget 50 XRP,
    wallet cap 5 XRP per 30 days, min bid 1 XRP, duration blank (no end).
  - A status embed shows active/stopped, knobs, budget
    committed/paid/remaining, open promises, refunds by state, the top 5
    wallets by refunded drops, and declines by reason.
  - `log_admin_action` posts each change to the admin log channel.
- **Reads are live on every request** (no cache), so a Stop takes effect on
  the next quote.
- **Validation.** `coverage_pct ∈ [0, 100]`, `budget_xrp > 0`. The royalty
  ceiling is not exposed.

### Client (vanilla-JS Activity)

- **`/api/market/listings` serialization.** External rows that already carry
  `clearing_*` gain `fee_cover_drops` / `fee_cover_xrp` when a campaign is
  active and the campaign has budget headroom for that estimate. This is a
  public, unauthenticated estimate: it ignores wallet caps, so the copy says
  "limits apply".
  - **The campaign read happens post-cache.** It is not folded into the 60 s
    `_MARKET_CACHE` key, so Stop is reflected immediately.
- **`market_pure.js`:**
  - `buyNowLabel` is unchanged.
  - New `feeCoverNote(vm)` renders "LFG refunds xrp.cafe's 0.08 XRP fee after
    it settles, so you pay the 5.08 XRP ask." It replaces `externalFeeNote`
    when `vm.feeCoverXrp` is set.
  - `externalFillCopy` gains refund lines, keyed on the status response's
    `fee_cover.state`. That state is one field folding promise and refund:
    the refund's state once a refund row exists, the promise's state before
    that, and `quoted` before the bid is on-ledger.
    - `quoted`: "LFG covers the fee (limits apply)."
    - `open`: "Fee refund reserved."
    - `released`: nothing extra. The bid closed unfilled and the existing
      "not filled — nothing was charged" line covers it.
    - `owed` / `submitted`: "Your 0.08 XRP fee refund is on its way."
    - `confirmed`: "Refunded 0.08 XRP (tx link)."
    - `declined`: "This purchase isn't covered: \<reason copy\>."
    - `failed`: "We couldn't send your fee refund. Contact support."
- **`app.js::buyExternalNow`.** The confirm dialog shows the fee-cover note.
  `watchExternalFill` keeps polling after `fill == accepted` until
  `fee_cover.state` is terminal or 2 minutes pass. After that, "on its way" is
  the final copy, since the sweep will pay.
- **"My bids"** (`/api/market/bids/mine` → `my_bids`): each row gains
  `fee_cover` from the promise/refund tables.
- **Cache-buster bumps** for `market_pure.js` / `app.js` follow the lockstep
  rule.

### Ops

- `scripts/fee_cover_report.py --network <net> [--campaign N] [--audit]`:
  - Budget burn-down: committed, paid, remaining.
  - Promises and refunds by state and reason.
  - Refunds by wallet: where abuse shows up first.
  - Open `submitted` rows older than an hour.
- **`--audit` exits non-zero** when any `confirmed` refund exceeds its
  promise, its observed fee × coverage, or 50% of its observed royalty, or
  when a campaign's confirmed total exceeds its budget.
- **Optional cron** `lfg-fee-cover-audit` / `stg-fee-cover-audit` at 03:40
  UTC, after `lfg-market-sweep` at 03:30. Registering it is an ops step, like
  every other cron here.
- **Ships with no campaign active on both stacks.** The code is inert until an
  admin presses Start.
- **Issuer XRP headroom is the number to watch.** Refunds are paid from the
  account the royalties land in, but the issuer also funds mint reserves. The
  status embed shows `issuer spendable XRP` next to `budget remaining`.

### Honesty and metrics

- **Campaign copy states plainly** that LFG refunds the other marketplace's
  fee on purchases made through LFG, funded from collection royalties.
- **Refund payments are tagged XRP `Payment`s** from the issuer, so they land in
  `sourcetag_metrics.py`'s `xrp_payment_volume.out_drops`. They are **not**
  marketplace volume and must not be presented as such. The report tallies
  them separately via the `fee-cover` memo action.

## What this does not do

- It does not refund sellers (deferred with #428).
- It does not cover in-app (non-brokered) sales: there is no fee to cover.
- It does not cover bids below clearing that fill later, nor unmeasured
  brokers (bidds, Art Dept until measured).
- It does not cover trait / BRIX sales: no external venue, and no user-to-user
  volume.
- It does not change `TransferFee`, which is immutable on the NFToken anyway,
  or the #426 clearing-price math.
- It does not introduce an LFG broker or broker fee.

## Risks

- **A broker rate changes.**
  - *Rate rises:* Buy-now bids stop filling (existing #426 risk). Promises
    expire and release.
  - *Rate falls:* the refund follows the observed fee, never the promise.
- **Fake-broker self-dealing.** An attacker brokers their own bid with an
  inflated `NFTokenBrokerFee` via an account they control. Three rules stop it:
  refunds require `T.Account` to be allowlisted, refunds cap at 50% of the
  observed royalty, and the promise is bounded by the rate at bid time.
- **Budget squatting.** Many bids placed and left open to lock budget. This is
  bounded by the per-wallet cap, which counts open promises, and the 7-day bid
  TTL.
- **Issuer runs short of spendable XRP.** The payout fails definitively and
  parks `failed`. The status embed shows the issuer's XRP balance next to
  budget remaining. After a top-up, `--requeue` pays it.
- **Archive gap delays a refund.** Promises fail closed and settle once #402
  catch-up heals the archive. The buyer sees "on its way", never a false
  "declined".

## Testing

- **Pure computation** (`fee_cover.compute_refund`), from real `meta`
  fixtures: the #426 verified fill (`FED6256D…`), a direct sell-accept
  (declined `not_broker_settled`), an issuer-seller sale (`issuer_party`), a
  fake-broker inflated fee (capped at 50% royalty; declined when the broker is
  not allowlisted), an IOU fee (`fee_unobserved`), and a zero-royalty sale.
- **Promise eligibility.** Every rule gets a tripping case and a passing case:
  clearing boundary (±1 drop), min bid, system wallet, budget boundary, wallet
  cap window edges, and coverage/rate snapshotting across an admin update.
- **Double-pay impossibility.** The same accept hash settles twice (poll +
  sweep) → one refund row, one submit.
- **Durability.** Covered cases:
  - An indeterminate submit stays `submitted`.
  - Recovery finds the memo → `confirmed`.
  - Absent before `last_ledger_seq` → untouched; absent after → `failed`.
  - A definitive failure parks `failed`; `--requeue` moves it back to `owed`
    only with budget headroom; `ClaimNotSubmitted` → back to `owed`.
  - Two concurrent `pay_refund` calls on one `owed` row → exactly one
    submission.
- **Release.**
  - A cancel event releases.
  - Expiry releases only once the archive is certified past expiration.
  - A continuity gap blocks release.
- **Stop semantics.** Stop → quote returns `campaign_inactive`, and an open
  promise still settles and pays.
- **Admin endpoints.** Service-token and Discord-surface gating, audit rows on
  no-ops, and validation bounds.
- **Serialization.** `fee_cover_*` fields only when a campaign is active with
  headroom; read post-cache; the 409 `external_listing` guard is unchanged.
- **JS pure tests** (`tests/test_market_pure_js.py`): `feeCoverNote` and the
  refund states in `externalFillCopy`.
- **Manual (staging, testnet).** No cafe on testnet, so allowlist a
  test broker account via `BROKER_ALLOWLIST_PATH` with a measured-style rate.
  Broker a test bid from a script and confirm the refund lands with the memo.
