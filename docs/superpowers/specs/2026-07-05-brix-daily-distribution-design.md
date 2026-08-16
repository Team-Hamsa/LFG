# BRIX Daily Distribution — Design

**Issue:** #48 — feat: BRIX daily distribution to holders (1/day per unlisted NFT; claim in app)
**Date:** 2026-07-05
**Status:** Draft for review

## 1. Problem

Reward LFG NFT holders with a daily BRIX drip: 1 BRIX per **unlisted** NFT per
24h epoch, accrued in a DB and paid on-chain only when the user explicitly
claims (Discord `/claim` or Activity button). Listed NFTs (active sell offer)
earn 0 for that epoch. Double-claims must be structurally impossible, and the
totals must be auditable against the chain.

Everything needed already exists as infrastructure — this design only adds two
tables, one daily script, one XRPL payment helper, two service endpoints, and
thin surface hooks:

- **Ownership per token:** `onchain_nfts` (nft_id → owner, is_burned), built by
  `lfg_core/nft_index.py` (schema at nft_index.py:66-82, `live_nfts()` at
  nft_index.py:171) and kept fresh by the pm2 listeners.
- **History + daily-job home:** `history_<net>.db` via
  `lfg_core/history_store.py` (`init_history_db` history_store.py:78,
  idempotent `executescript` schema — new tables slot in with zero migration
  tooling). `scripts/snapshot_balances.py` + its pm2 cron
  (`lfg-snapshot`, `--cron "10 0 * * *"`, CLAUDE.md) is the exact daily-job
  pattern to copy.
- **BRIX event derivation:** `derive_brix_events` (history_events.py:273)
  already classifies distributor payments as `airdrop`
  (history_events.py:292), and the `brix_events.kind` column already reserves
  a **`claim`** kind (history_store.py:47) that nothing emits yet.
- **Payment plumbing:** issuer-signed `Payment` with
  `source_tag=config.SOURCE_TAG` (2606160021, config.py:193) exists in
  `buy_and_burn` (xrpl_ops.py:301-340); trustline check exists as
  `get_trustline_balance` (xrpl_ops.py:255).
- **Service + auth:** `require_wallet` decorator (app.py:308-322), route table
  (app.py:1250+), leaderboard endpoint as the read-endpoint style template
  (app.py:504, executor-thread sqlite at app.py:544-579).

## 2. Accrual model

### 2.1 Epoch definition

An epoch is a **UTC calendar day** (`YYYY-MM-DD`), identical to the
`balance_snapshots.snap_date` convention (`datetime.now(timezone.utc)
.strftime("%Y-%m-%d")`, snapshot_balances.py:107). An NFT accrues 1 BRIX for
epoch *D* if, **at the accrual evaluation for D** (run shortly after D closes,
00:20 UTC), it is:

1. live (`onchain_nfts.is_burned = 0`),
2. held by a non-system wallet (exclude issuer, `SWAP_OFFER_ISSUER`,
   `BRIX_DISTRIBUTOR_ADDRESS`, `BRIX_AMM_ACCOUNT` — same set as
   `_lb_system_accounts()`, app.py:484-494),
3. **unlisted** (§3).

**Mid-epoch transfers:** the owner recorded is the owner at evaluation time
(effectively "owner at epoch close" as seen by the listener-fresh index). One
deterministic attribution point; no proration. A wash-transfer gains nothing —
the token still accrues exactly once per day (PK, §4).

### 2.2 Idempotency & backfill

The accruals table is keyed `PRIMARY KEY (epoch_date, nft_id)` and written
with `INSERT OR IGNORE` — re-running the job for the same date is a no-op, and
one NFT can never accrue twice for one epoch **by constraint**, not app logic.

`scripts/accrue_brix.py --network <net> [--date YYYY-MM-DD]` accrues one
epoch (default: yesterday UTC). A small `brix_meta` row records the last
accrued epoch; on startup the script loops from `last_accrued + 1` through
yesterday, so a missed cron day self-heals on the next run. Caveat
(documented, accepted): a late catch-up run evaluates ownership/listing at
run time, not at the historical epoch close — for a 1-BRIX/day drip this
drift is negligible and not worth reconstructing per-day historical state
from `nft_events`. (Rejected alternative in §8.)

Clock: everything is UTC; the only clock consumer is "which date is
yesterday". No timezone configurability.

## 3. "Unlisted" definition

**Authoritative source: on-ledger active sell offers via clio
`nft_sell_offers`, queried per live token at accrual time.**

Why not the history DB: `nft_events` records `offer_create` /
`offer_cancel` / `sale` rows (history_events.py:204-232), but an
`offer_create` row does not carry the **offer index**, so a later
`offer_cancel` (which reports the deleted offer's NFTokenID+Owner from meta,
history_events.py:221-232) cannot be matched to a *specific* earlier create
when an owner made several. Also offers created before our backfill window,
or expired offers, make event-replay a heuristic. The ledger's live
`NFTokenOffer` objects are exact.

Rules applied to the `nft_sell_offers` response:

- **Listed** ⇔ at least one sell offer whose `owner == current holder` exists.
  Offers with a `Destination` **still count as listed** — brokered
  marketplaces (xrp.cafe style) create destination-locked sell offers to the
  broker, and those are precisely the listings we must exclude. Stale offers
  by *previous* owners (invalid after transfer) do not count.
- `objectNotFound` from clio ⇒ zero sell offers ⇒ **unlisted**.
- **Fail-closed:** any other error / timeout for a token ⇒ treat as listed
  (accrue 0 that epoch, log a warning with a count). Rationale: an accrual is
  a monetary grant; when offer state is unknown we must not pay. A rare
  missed BRIX is recoverable goodwill; systematically paying listed NFTs
  during a clio outage is not.
- Endpoint: `nft_sell_offers` is a **standard rippled API method** (xrpl-py
  ships `xrpl.models.requests.NFTSellOffers`) — unlike `nft_info` /
  `nft_exists`, which are genuinely clio-only (xrpl_ops.py:194-198). The
  daily sweep still uses `config.CLIO_WS_URL` for consistency with the rest
  of the index tooling and clio's full-history posture — a preference, not a
  capability requirement.

Scale check: ~3,535 live tokens × 1 request/day over one websocket
connection — a few minutes, fine for a 00:20 UTC cron.

## 4. Storage & data model

Both tables live in **`history_<net>.db`** (per-network, alongside
`brix_events` / `balance_snapshots`) — accruals/claims are BRIX-economy
history, the conservation audit joins them against `brix_events`, and
`init_history_db`'s `CREATE TABLE IF NOT EXISTS` script (history_store.py:84)
gives us schema rollout for free. The app DB (`lfg_nfts.db`) is
edition-keyed and wrong for this; a new DB file would orphan the audit joins.

```sql
CREATE TABLE IF NOT EXISTS brix_accruals (
    epoch_date TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
    nft_id     TEXT NOT NULL,
    owner      TEXT NOT NULL,          -- holder at evaluation time
    amount     INTEGER NOT NULL DEFAULT 1,  -- whole BRIX (unit = 1 BRIX)
    claim_id   INTEGER,                -- NULL = unclaimed
    PRIMARY KEY (epoch_date, nft_id)
);
CREATE INDEX IF NOT EXISTS idx_accrual_owner ON brix_accruals(owner, claim_id);

CREATE TABLE IF NOT EXISTS brix_claims (
    claim_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet     TEXT NOT NULL,
    amount     INTEGER NOT NULL,       -- whole BRIX (Σ of bound accruals)
    state      TEXT NOT NULL,          -- pending|submitted|confirmed|failed
    tx_hash    TEXT,
    last_ledger_seq INTEGER,           -- Payment LastLedgerSequence (§5.3)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- one in-flight claim per wallet, enforced by the engine not the app:
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_claim
    ON brix_claims(wallet) WHERE state IN ('pending','submitted');

CREATE TABLE IF NOT EXISTS brix_meta (   -- last accrued epoch, per key
    key TEXT PRIMARY KEY, value TEXT
);
```

**Double-claim prevention is constraint-level, twice over:**

1. Binding accruals to a claim is one atomic statement inside the claim
   transaction: `UPDATE brix_accruals SET claim_id=? WHERE owner=? AND
   claim_id IS NULL`. Claimable balance is `SUM(amount) WHERE owner=? AND
   claim_id IS NULL` — once bound, rows can never be counted again, and two
   racing claims cannot bind the same row (sqlite write serialization + the
   NULL predicate).
2. The partial unique index rejects a second open claim for the same wallet
   at INSERT time, even across processes.

**Amounts are INTEGER, not REAL:** this is a currency ledger whose
conservation audit compares SUMs across three sources (§6); float
accumulation drift would force epsilon tolerances or produce false FAILs.
Drips are whole BRIX (1/NFT/day). If fractional rates ever arrive, that is
an explicit schema migration then (§9).

## 5. Claim flow

### 5.1 Payment source: the distributor wallet, not the issuer

Claims are paid by **`BRIX_DISTRIBUTOR_ADDRESS`** (config.py:174), signed
with a new env var **`BRIX_DISTRIBUTOR_SEED`**. Reasons:

- Paying BRIX *from the BRIX issuer* silently mints new supply on every
  claim; paying from a pre-funded distributor keeps issuance an explicit,
  visible funding operation.
- The derivation pipeline already special-cases the distributor
  (history_events.py:292) and the leaderboards already exclude it as a
  system account (app.py:490) — claims won't pollute `brix_earned` /
  `brix_rich` rankings.
- The issuer's mainnet regular-key setup (SIGNING_ACCOUNT/SEED) stays
  untouched.

New helper `lfg_core/xrpl_ops.send_brix_claim(destination, value, claim_id)`
modeled on `buy_and_burn` (xrpl_ops.py:301): `Payment` with
`amount=IssuedCurrencyAmount(config.SWAP_OFFER_CURRENCY_HEX,
config.SWAP_OFFER_ISSUER, value)` (the BRIX currency constants,
config.py:162-165), **`source_tag=config.SOURCE_TAG`** (hackathon-mandatory,
config.py:193), and a `Memo` `lfg:brix_claim:<claim_id>` so every claim tx is
self-identifying on-chain. The helper **always sets `LastLedgerSequence`**
(current validated ledger + a margin, e.g. +40 — `submit_and_wait`'s
autofill default is acceptable as long as the value is captured) and returns
it to the caller, who records it on the claim row (`last_ledger_seq`, §4) —
this is what makes "definitively failed" decidable during recovery (§5.3
step 5). Returns tx hash on `tesSUCCESS`, a typed failure otherwise
(distinguishing "definitively failed", e.g. `tec*`/no trustline, from
"unknown", e.g. timeout after submit).

### 5.2 Trustline requirement

Accrual **never** requires a trustline (balance just sits in the DB).
Claiming does: before creating the claim row, check
`get_trustline_balance(wallet, SWAP_OFFER_CURRENCY_HEX, SWAP_OFFER_ISSUER)`
(xrpl_ops.py:255) — `None` ⇒ HTTP 409 `{"code": "trustline_required"}`, no
state change. The Activity/Discord surface shows the existing BRIX trustline
button (MintView already has one, per issue notes). This is advisory (the
race where a user removes the line mid-claim is caught by the payment
returning `tecPATH_DRY`/`tecNO_LINE` → claim `failed` → accruals unbound).

### 5.3 Order of operations (partial-failure safety)

DB-journal-first, chain second, exactly one ambiguous window, never
blind-retried — same posture as `swap_flow`/`economy_flow` journaling:

1. **TX A (sqlite transaction):** check no open claim (index enforces),
   `SUM` unclaimed accruals (0 ⇒ 400 `nothing_to_claim`), INSERT claim
   `state='pending'`, bind accruals (`UPDATE … SET claim_id`), COMMIT.
2. **Submit payment** (`send_brix_claim`); persist the returned
   `last_ledger_seq` on the claim row as soon as it is known (before or
   immediately after submit — it bounds the ambiguity window).
3. On submit-accepted: `state='submitted'`, `tx_hash=…`. On `tesSUCCESS`
   validation: `state='confirmed'`.
4. On **definitive failure** (tec-class, malformed): `state='failed'` and
   **unbind** (`UPDATE brix_accruals SET claim_id=NULL WHERE claim_id=?`) —
   balance returns, user may retry.
5. On **unknown outcome** (crash/timeout between 2 and 3): the claim stays
   `pending`/`submitted` with the accruals still bound — funds can never be
   double-paid because a new claim can't include those rows and the unique
   index blocks a new open claim. Recovery (`scripts/recover_brix_claims.py`,
   also run at service startup): for each stale open claim, search the
   distributor's `account_tx` for the `lfg:brix_claim:<claim_id>` memo.
   - found + validated ⇒ `confirmed` (+ tx_hash).
   - **`failed` + unbind ONLY when both hold:** (a) the memo tx is absent
     from distributor `account_tx` over a fully-validated range, AND (b) the
     current **validated** ledger index is `> last_ledger_seq` on the claim
     row — past that point the XRPL guarantees the tx can never validate.
   - anything else (lookup error, `last_ledger_seq` NULL or not yet passed,
     range not fully validated) ⇒ leave untouched; try again later.
   Absence from `account_tx` alone is NOT proof of failure while the tx
   could still validate; `LastLedgerSequence` is what makes the verdict
   decidable. The memo makes recovery exact, never a guess.

### 5.4 API

Following `handle_leaderboard` (sqlite on executor thread, app.py:544) and
`require_wallet` (app.py:308):

- `GET /api/brix` (require_wallet) →
  `{wallet, claimable, unlisted_last_epoch, accrued_total, claimed_total,
  open_claim: {claim_id, state, tx_hash} | null, last_epoch}`.
  `unlisted_last_epoch` = `COUNT(*)` of the caller's `brix_accruals` rows
  for `last_epoch` (the latest accrued epoch, from `brix_meta`) — a pure DB
  read, **never** a live per-request clio sweep.
- `POST /api/brix/claim` (require_wallet) → runs §5.3; returns
  `{claim_id, state, amount, tx_hash}`; 409 `trustline_required` /
  `claim_in_flight`, 400 `nothing_to_claim`.
- `GET /api/brix/claim/{claim_id}` (require_wallet, own claims only) →
  claim status polling, same ownership-check shape as `make_status_handler`
  (app.py:325).

Surfaces:
- **Discord** `/claim` in `surfaces/discord_bot/commands.py` (thin, calls the
  service via `LFGServiceClient` like `register`/`letsgo`).
- **Activity**: a BRIX balance card + Claim button in `webapp/` (vanilla-JS,
  no build step), polling the claim status endpoint; trustline-required
  response deep-links the existing trustline flow.

## 6. Derivation + admin + conservation audit

- **`kind='claim'`:** extend `derive_brix_events` — a `Payment` from the
  distributor carrying an `lfg:brix_claim:` memo derives `kind="claim"`
  instead of `"airdrop"` (slots into history_events.py:292; the schema
  comment already reserves the kind, history_store.py:47). Rebuildable
  retroactively via `scripts/derive_history_events.py`.
- **Admin report** `scripts/brix_admin_report.py --network <net>`: pending
  claimable total + top unclaimed wallets, claims by state, total
  distributed, top recipients. (CLI-first like every other ops surface here;
  a gated endpoint can come later if the Activity grows an admin page.)
- **Conservation audit** `scripts/audit_brix_distribution.py --network
  <net>`, modeled on `scripts/audit_history.py` (PASS/FAIL, non-zero exit):
  1. Σ `brix_accruals.amount` where `claim_id` ∈ confirmed claims ==
     Σ `brix_claims.amount` where `state='confirmed'`;
  2. that total == Σ on-chain distributor debits with `kind='claim'` in
     `brix_events`;
  3. no accrual bound to a `failed` claim; no `confirmed` claim without
     `tx_hash`;
  4. per-epoch accrual count ≤ live-token count from `onchain_nfts`.

## 7. Anti-abuse summary

| Vector | Defense |
|---|---|
| Same NFT counted twice in an epoch | `PRIMARY KEY (epoch_date, nft_id)` + `INSERT OR IGNORE` |
| Transfer mid-epoch (both parties accrue) | single evaluation point per epoch; one owner recorded |
| Re-running accrual job | idempotent by PK; `brix_meta` cursor |
| Double-claim / race | atomic `claim_id IS NULL` binding + partial unique open-claim index |
| Replay after crash | memo-based on-chain reconciliation before any retry; never blind-resubmit |
| List-then-claim within epoch | listing checked at evaluation; fail-closed on unknown offer state |
| No trustline | pre-check 409; tec failure path unbinds cleanly |
| System accounts farming | issuer/distributor/AMM excluded (same set as leaderboards) |

## 8. Alternatives considered

- **Historical state reconstruction for backfill** (owner+listing at each
  past epoch close from `nft_events`): exact but heavy, and listing replay is
  unreliable without offer indexes (§3). Rejected — catch-up evaluates at
  run time; drift ≤ the missed window on a 1-BRIX granularity.
- **Listing detection from `nft_events` only** (no ledger query): rejected,
  see §3 (offer-index ambiguity, pre-window offers, expirations).
- **Fail-open on unknown offer state:** rejected — never pay on unknown
  state (mirrors the fail-closed Deposit posture in the trait economy).
- **Pay from the issuer wallet:** rejected (§5.1 — invisible supply
  inflation, leaderboard pollution, regkey entanglement).
- **Auto-send daily (no claim):** rejected by the issue itself ("user must
  initiate claim") — and batch-sending to thousands of wallets daily is an
  ops/fee burden with no user touchpoint.
- **XUMM-signed claims:** unnecessary — the *user* signs nothing; the
  distributor pays out. XUMM stays in the trustline flow only.
- **New standalone DB:** rejected — audit joins live in `history_<net>.db`.

## 9. Non-goals

- Proration within an epoch; retroactive accrual before feature launch.
- Variable rates, rarity multipliers, streak bonuses. Fractional drip rates
  are explicitly out: amounts are INTEGER whole BRIX (§4); fractional rates
  would be a deliberate schema migration.
- Mainnet enablement decisions (ship testnet-first; mainnet is env + funding
  the distributor with BRIX, an ops step).
- Buy-offer or AMM "listing" semantics — only owner sell offers delist.
- Admin web UI.

## 10. Risks

- **Distributor runs dry** → claims fail with tec, accruals unbind, users
  retry after refunding. Mitigation: admin report shows
  `claimable_total vs distributor balance` headroom; alert threshold in the
  accrual job log.
- **clio flakiness at accrual time** → fail-closed under-accrual. Mitigation:
  per-token retry (3 attempts) before marking listed; warning count in
  output; idempotent re-run does NOT retroactively add missed rows for an
  already-accrued epoch date (accepted).
- **Listener staleness** (ownership index behind) → accrual attributes to a
  recent-but-stale owner. Bounded by listener lag (seconds); accepted.
- **sqlite contention** with listeners: history DBs are WAL with 30s busy
  timeout (history_store.py:82-83); claim transactions are short.
