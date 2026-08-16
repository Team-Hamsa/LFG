# BRIX Daily Distribution Implementation Plan

**Spec:** docs/superpowers/specs/2026-07-05-brix-daily-distribution-design.md
**Issue:** #48

## Global Constraints

- **TDD:** every task starts with a failing test, then the implementation,
  then a verification command whose output is confirmed before moving on.
- **Env-guard preamble:** every NEW test file that imports `lfg_core` at
  module top MUST copy the env-guard preamble **verbatim from
  tests/test_seasons.py lines 1-18** (the `os.environ.setdefault` block for
  XUMM/SEED/TOKEN/BUNNY/LAYER_SOURCE/BUNNY_PULL_ZONE) before any `lfg_core`
  import, or full-suite collection order strands frozen config constants and
  breaks `webapp/test_smoke`.
- **SourceTag:** every XRPL transaction built here sets
  `source_tag=config.SOURCE_TAG` (2606160021). A test asserts it.
- **PRs:** open as **draft** (`gh pr create --draft`); flip ready
  (`gh pr ready`) only when settled — that triggers CodeRabbit; resolve or
  explicitly address its findings before merge; ≤4 ready-flips/hour.
- **Branching:** two stacked draft PRs off `main`:
  - PR-1 `feat/brix-accrual` — store + accrual engine + scripts + audit.
  - PR-2 `feat/brix-claim` — payment helper + service endpoints + surfaces.
- Verification base command: `.venv/bin/python -m pytest <file> -q`
  (pre-push runs `ruff format`; run `.venv/bin/ruff check .` before pushing).
- Pure logic lives in `lfg_core/` (unit-testable, no I/O at import);
  scripts stay thin CLIs — house style per the trait-rules plan.

## File Structure

```
lfg_core/brix_drip.py            # NEW: schema, accrual + claim state machine (pure/sqlite)
lfg_core/xrpl_ops.py             # + send_brix_claim(), find_claim_payment()
lfg_core/history_events.py       # derive_brix_events: kind='claim' for memo'd distributor payments
lfg_core/config.py               # + BRIX_DISTRIBUTOR_SEED, BRIX_DRIP_AMOUNT (default "1")
lfg_service/app.py               # + GET /api/brix, POST /api/brix/claim, GET /api/brix/claim/{id}
scripts/accrue_brix.py           # NEW: daily accrual CLI (pm2 cron)
scripts/recover_brix_claims.py   # NEW: stale open-claim reconciliation
scripts/brix_admin_report.py     # NEW: admin totals
scripts/audit_brix_distribution.py  # NEW: conservation audit (exit non-zero on drift)
surfaces/discord_bot/commands.py # + /claim
surfaces/_client/client.py       # + brix_status()/brix_claim() methods
webapp/                          # BRIX card + claim button
tests/test_brix_drip.py          # NEW
tests/test_brix_claim_flow.py    # NEW
tests/test_brix_endpoints.py     # NEW
tests/test_brix_derive_claim.py  # NEW
```

---

# PR-1 — Accrual store + engine (`feat/brix-accrual`)

### Task 1: Schema + accrual store (`lfg_core/brix_drip.py`)

- [ ] **Step 1: failing test** — `tests/test_brix_drip.py` (env-guard
  preamble verbatim from tests/test_seasons.py lines 1-18). Cases against a
  tmp sqlite file initialized with `brix_drip.ensure_schema(conn)`:
  - tables `brix_accruals`, `brix_claims`, `brix_meta` exist; partial unique
    index `idx_one_open_claim` rejects a second `pending` claim for one
    wallet (`sqlite3.IntegrityError`).
  - `record_accruals(conn, epoch, rows)` twice with the same rows →
    row count unchanged (INSERT OR IGNORE on PK `(epoch_date, nft_id)`).
  - `claimable(conn, wallet)` sums only `claim_id IS NULL` rows.
  - amounts are **INTEGER whole BRIX** (spec §4): assert `PRAGMA
    table_info` declares INTEGER for `brix_accruals.amount` and
    `brix_claims.amount`, and that `claimable` returns an `int` (exact
    integer arithmetic — the conservation audit must never need epsilons).
  - `get_meta/set_meta` round-trip `last_accrued_epoch`.
- [ ] **Step 2: implement** `ensure_schema` (executescript, mirroring
  history_store.py:13-66 style), `record_accruals`, `claimable`, meta
  helpers. Schema exactly as spec §4.
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_drip.py -q`

### Task 2: Accrual evaluation (pure) + listing check

- [ ] **Step 1: failing test** (same file) — `evaluate_accruals(live_tokens,
  listed_fn, system_accounts, epoch)` is pure: given fake `OnchainNft`-shaped
  rows and a `listed_fn(nft_id) -> bool | None`:
  - burned tokens, system-account owners, ownerless tokens → skipped;
  - `listed_fn` True → skipped; **None (unknown) → skipped (fail-closed)**
    and counted in a returned `unknown` tally;
  - False → accrual row `{epoch_date, nft_id, owner, amount}`.
- [ ] **Step 2: implement** in `lfg_core/brix_drip.py`. Also
  `async fetch_sell_offer_state(nft_ids, clio_url)` → `dict[nft_id,
  bool|None]` using clio `nft_sell_offers` over one
  `AsyncWebsocketClient(config.CLIO_WS_URL)` connection: listed iff any
  offer with `owner == current holder`; `objectNotFound` → False; other
  errors after 3 retries → None. Unit-test the response-classification
  helper with canned dicts (no network).
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_drip.py -q`

### Task 3: CLI `scripts/accrue_brix.py`

- [ ] **Step 1: failing test** — subprocess-free: import the script's
  `run_accrual(conn, oconn, listed_fn, today)` orchestration function and
  assert: catches up from `last_accrued_epoch+1` through yesterday;
  re-run is a no-op; meta cursor advances.
- [ ] **Step 2: implement** — argparse `--network/--date`, reads
  `onchain_<net>.db` via `nft_index.live_nfts` (nft_index.py:171), writes
  `history_<net>.db`, prints per-epoch `accrued/skipped_listed/unknown`
  counts + distributor-balance headroom warning. Docstring documents the pm2
  cron (copy the `lfg-snapshot` pattern from CLAUDE.md, suggested
  `--cron "20 0 * * *"`).
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_drip.py -q`
  and a manual smoke: `.venv/bin/python scripts/accrue_brix.py --network
  testnet --date 2026-07-04` twice → identical totals, second run inserts 0.

### Task 4: Admin report + conservation audit scripts

- [ ] **Step 1: failing test** — seed a tmp history DB with accruals, claims
  (confirmed/failed), and `brix_events` `kind='claim'` rows; assert the
  audit's check functions return the four PASS/FAIL results from spec §6 and
  a seeded drift (confirmed claim without matching on-chain debit) FAILs.
  Accrual/claim totals compare as exact integers (no epsilon); only the
  on-chain `brix_events.delta` side is REAL and is compared after `round()`
  to whole BRIX.
- [ ] **Step 2: implement** `scripts/brix_admin_report.py` and
  `scripts/audit_brix_distribution.py` (audit_history.py style: prints
  PASS/FAIL per check, exits non-zero on any FAIL; check functions live in
  `lfg_core/brix_drip.py` for testability).
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_drip.py -q`
- [ ] **PR-1:** `gh pr create --draft` → self-review → `gh pr ready` →
  CodeRabbit findings addressed → merge.

---

# PR-2 — Claim flow + surfaces (`feat/brix-claim`)

### Task 5: Payment helper (`xrpl_ops.send_brix_claim`) + config

- [ ] **Step 1: failing test** — `tests/test_brix_claim_flow.py` (env-guard
  preamble verbatim from tests/test_seasons.py lines 1-18). With
  `submit_and_wait` monkeypatched, assert the built `Payment` has:
  `account == config.BRIX_DISTRIBUTOR_ADDRESS`, BRIX
  `IssuedCurrencyAmount` (currency `config.SWAP_OFFER_CURRENCY_HEX`, issuer
  `config.SWAP_OFFER_ISSUER`), **`source_tag == config.SOURCE_TAG`**, and a
  memo decoding to `lfg:brix_claim:<id>`. The helper must set and **return
  the Payment's `LastLedgerSequence`** (spec §5.3 — it makes recovery
  decidable); assert it is present in every result. Result mapping:
  `tesSUCCESS` → `("confirmed", hash, lls)`; `tec*` → `("failed", None,
  lls)`; exception after submit → `("unknown", None, lls)`.
- [ ] **Step 2: implement** — `config.BRIX_DISTRIBUTOR_SEED` (no default),
  `config.BRIX_DRIP_AMOUNT`; `send_brix_claim` modeled on `buy_and_burn`
  (xrpl_ops.py:301) but returning the tri-state; `find_claim_payment(claim_id)`
  scanning distributor `account_tx` for the memo (for recovery).
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_claim_flow.py -q`

### Task 6: Claim state machine (`brix_drip.open_claim / settle_claim`)

- [ ] **Step 1: failing test** — on a seeded DB:
  - `open_claim(conn, wallet)` binds exactly the unclaimed rows atomically
    and returns `(claim_id, amount)`; zero claimable → `NothingToClaim`;
    existing open claim → `ClaimInFlight` (IntegrityError mapped).
  - `settle_claim(conn, claim_id, "confirmed", tx_hash)` sets state+hash;
    `"failed"` **unbinds** accruals (their `claim_id` back to NULL);
    `"unknown"` leaves everything bound.
  - `record_submission(conn, claim_id, tx_hash, last_ledger_seq)` persists
    the `last_ledger_seq` column (spec §4/§5.3 step 2).
  - Two threads racing `open_claim` for one wallet → exactly one succeeds.
- [ ] **Step 2: implement** in `lfg_core/brix_drip.py` (single transaction,
  spec §5.3 step 1 and step 3-4 transitions).
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_claim_flow.py -q`

### Task 7: Recovery script `scripts/recover_brix_claims.py`

- [ ] **Step 1: failing test** — with `find_claim_payment` and the
  current-validated-ledger lookup faked: stale `submitted` claim whose memo
  tx is found+validated → `confirmed` (+hash); memo tx absent AND validated
  ledger index `> claim.last_ledger_seq` → `failed` + accruals unbound;
  **memo tx absent but validated ledger `<= last_ledger_seq` → left
  untouched** (tx could still validate — absence alone is not failure);
  `last_ledger_seq` NULL, non-fully-validated range, or lookup error → left
  untouched (never guess).
- [ ] **Step 2: implement** thin CLI over a `recover(conn, finder)` function
  in `brix_drip.py`; also invoked once at service startup (Task 8).
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_claim_flow.py -q`

### Task 8: Service endpoints (`lfg_service/app.py`)

- [ ] **Step 1: failing test** — `tests/test_brix_endpoints.py` (env-guard
  preamble verbatim from tests/test_seasons.py lines 1-18), aiohttp
  test-client against `create_app()` with `WEBAPP_DEV_MODE` + tmp
  `HISTORY_DB_PATH`, trustline + payment monkeypatched:
  - `GET /api/brix` → claimable/totals/open_claim shape (spec §5.4);
    `unlisted_last_epoch` is a pure DB count of the caller's accrual rows
    for the latest accrued epoch — assert no clio/network call is made;
  - `POST /api/brix/claim` happy path → `{claim_id, state:"confirmed",
    amount, tx_hash}`; no trustline → 409 `trustline_required`; nothing
    accrued → 400 `nothing_to_claim`; open claim → 409 `claim_in_flight`;
    tec failure → claim failed and balance restored on next GET;
  - `GET /api/brix/claim/{id}` → 404 for another wallet's claim.
- [ ] **Step 2: implement** handlers with `require_wallet` (app.py:308),
  sqlite work on an executor thread with connections opened in-thread (the
  `handle_leaderboard` pattern, app.py:544-579); register routes in
  `create_app()` (app.py:1250+); run claim recovery once at startup.
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_endpoints.py -q`

### Task 9: `kind='claim'` derivation

- [ ] **Step 1: failing test** — `tests/test_brix_derive_claim.py`
  (env-guard preamble verbatim from tests/test_seasons.py lines 1-18): a
  normalized distributor Payment tx with the `lfg:brix_claim:` memo derives
  `kind="claim"`; without the memo stays `"airdrop"`; non-distributor
  payment with the memo stays `"payment"`.
- [ ] **Step 2: implement** in `derive_brix_events`
  (history_events.py:289-298).
- [ ] **Verify:** `.venv/bin/python -m pytest tests/test_brix_derive_claim.py -q`
  then full suite `.venv/bin/python -m pytest -q`.

### Task 10: Surfaces — Discord `/claim` + Activity card

- [ ] **Step 1: failing test** — client-method tests for
  `LFGServiceClient.brix_status()/brix_claim()` (existing `_client` test
  pattern); webapp smoke keeps passing.
- [ ] **Step 2: implement** — `/claim` slash command in
  `surfaces/discord_bot/commands.py` (thin: call service, render
  claimable → confirm → result embed; `trustline_required` → point at the
  existing MintView trustline button). Activity: BRIX card (claimable +
  "Claim" button + status poll) in `webapp/` vanilla-JS, matching existing
  fetch/poll patterns; use in-app overlay for confirm (no `window.confirm` —
  silent no-op in Discord's sandboxed iframe).
- [ ] **Verify:** `.venv/bin/python -m pytest -q`; manual:
  `WEBAPP_DEV_MODE=1` harness → card renders, claim round-trips against the
  mocked flow.

### Task 11: Docs + ops

- [ ] CLAUDE.md: new env vars (`BRIX_DISTRIBUTOR_SEED`, `BRIX_DRIP_AMOUNT`),
  accrual cron, recovery/audit commands, distributor pre-funding note.
- [ ] Post spec+plan permalinks (blob URLs at commit SHA) to issue #48 via
  `gh issue comment 48 --repo Team-Hamsa/LFG` (required by the repo's
  brainstorming→issue-link rule).
- [ ] **PR-2:** draft → ready → CodeRabbit resolved → merge. Ops afterward:
  set envs, fund distributor with BRIX (testnet first), install
  `lfg-brix-accrue` pm2 cron (`--cron "20 0 * * *" --no-autorestart`), run
  `scripts/audit_brix_distribution.py` after first epoch.
