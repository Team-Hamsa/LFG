# XRPL Transaction Hygiene — Pre-Submit Simulation (#58)

**Issues:** #58 (OPEN — the only live slice) · #61 · #75 · #57 · #54 (all
CLOSED, kept below as the historical record) · **Date:** 2026-07-05 ·
**Last review:** 2026-08-19 ·
**Status:** live — re-reviewed 2026-08-19 against main@27dc301

Originally one spec for the whole "tx hygiene" family. Four of its five issues
shipped in July 2026. **Only §3 (#58, pre-submit simulation) is still
buildable**; §1–§2 are preserved as the record of what landed and what did not.

---

## 0. What changed since this was drafted

- **The premise of the old §4 is gone.** Its "5 duplicated blind-retry loops"
  in `lfg_core/xrpl_ops.py` overcounted even on the day it was written: at
  `eea5085` (2026-07-05) the file had 5 `submit_and_wait` call sites but only
  **3** copy-pasted `retries = 5` loops (`mint_nft`, `burn_nft`, `modify_nft`);
  `create_nft_offer` and `buy_and_burn` already submitted once with no retry.
  All three loops are gone: PR #188 (`fix(economy): indeterminate on-chain
  outcome taxonomy, no blind resubmit (#179)`, merged 2026-07-12, commit
  `1b0923e`) replaced them with a single `xrpl_ops._submit_and_confirm` that
  **signs once, submits once, and never resubmits** (`git log -S"retries = 5"
  -- lfg_core/xrpl_ops.py` names exactly that commit). There is nothing left to
  consolidate, and §4's "classified retry" table is now the opposite of the
  invariant the code holds (see §3.5).
- **A choke point already exists** — `_submit_and_confirm` — so the spec's
  proposed `submit_checked()` is a rename of something real, not a new
  abstraction. Six ops route through it: `mint_nft`, `create_nft_offer`,
  `cancel_nft_offer`, `buy_and_burn`, `burn_nft`, `modify_nft`.
- **Two submit paths bypass it by design** (added after the draft, PR #328,
  sponsored free mint): `prepare_sponsored_mint`/`submit_sponsored_mint` and
  `prepare_sponsored_burn`/`submit_sponsored_burn` split sign-then-forward
  across process restarts, so they call `autofill_and_sign` and
  `submit_and_wait` themselves.
- **#61/#75/#57/#54 all closed** (2026-07-09/10). The provenance memo schema
  shipped as `lfg_core/memos.py` — not as `lfg_core/tx_memo.py`, and not in
  the shape §3 of the old draft proposed (see §1.2).
- **`config.RETRY_MAX_ATTEMPTS` / `config.RETRY_BASE_DELAY` do not exist.**
  `lfg_core/config.py` contains no `RETRY` symbol at all; those env vars are
  read by the *surface* configs (`surfaces/discord_bot/config.py`,
  `surfaces/telegram_bot/config.py`, `surfaces/x_bot/config.py`,
  `surfaces/_client/_retry.py`) for Discord reconnect and service-client
  retries. The old §4.1 "reuse existing RETRY_* env vars" instruction cannot be
  followed as written.
- **`simulate` is real and verified against our endpoints.** The draft flagged
  two unverified assumptions; both are now settled (§3.6):
  - `requirements.txt` pins nothing (line 8 is a bare `xrpl-py`). The ambient
    interpreter on this box resolves 4.5.0; PyPI's current latest is 5.0.0.
    Both expose `xrpl.transaction.simulate`,
    `xrpl.asyncio.transaction.simulate`, and `xrpl.models.requests.Simulate`
    (checked in the installed 4.5.0 and in the downloaded 5.0.0 wheel).
  - Both `config.JSON_RPC_URL` defaults answer the `simulate` method today
    (probed 2026-08-19, §3.6). The `PRESUBMIT_SIMULATE=0` fallback default the
    draft demanded is **not** needed.
- **`simulate` refuses a signed transaction** (`transactionSigned` error), so
  the pre-flight must run on the unsigned model *before*
  `_autofill_and_sign_with_retry`, not on the signed blob. This constrains
  placement in both the `_submit_and_confirm` path and the sponsored
  prepare/submit split.
- **Part of the reserve check is already server-side.** `simulate` returns the
  reserve-aware engine result directly (verified: an over-balance Payment on
  mainnet simulates `tecUNFUNDED_PAYMENT`). The hand-rolled integer-drops
  reserve calculator in the old §4.1 step 3 is redundant and is cut (§3.4).
- **`xrpl_ops.account_exists` already ships one pre-flight** — a three-way
  funded/absent/unknown check whose docstring calls out `tecNO_DST` on
  `NFTokenCreateOffer`. The simulate design must not duplicate or contradict
  it.
- **Test isolation moved** (#323): the root `conftest.py` is now the only place
  a suite-wide env pin works, and it already sets `LFG_SKIP_DOTENV=1` plus the
  mandatory vars. Any new flag must be pinned there, and any new network call
  inside `_submit_and_confirm` (or the sponsored prepare paths) escapes the
  stubs of ten existing test modules — they patch `autofill_and_sign` /
  `submit_and_wait`, sometimes `JsonRpcClient.request`, none of which intercept
  a simulate (§3.7).

---

## 1. Historical record — the four closed slices

Kept short. These describe shipped code; do not re-implement them.

### 1.1 SourceTag closure (#61, #75, #57's tag half) — closed 2026-07-09/10

The audit table in the original draft was correct in substance; its line
numbers have all rotted. What is true today:

- Backend builders in `lfg_core/xrpl_ops.py` set `source_tag=config.SOURCE_TAG`
  on every transaction they construct (`mint_nft`,
  `_sponsored_mint_transaction`, `create_nft_offer`, `cancel_nft_offer`,
  `buy_and_burn`, `burn_nft`, `modify_nft`, `prepare_sponsored_burn`).
- The user-signed choke point holds: `xumm_ops._create_xumm_payload` does
  `txjson.setdefault("SourceTag", config.SOURCE_TAG)` for every non-`SignIn`
  txjson.
- `surfaces/discord_bot/trustline.py` and `surfaces/discord_bot/admin.py`
  (`burn_nft`) stamp it inline.
- Guard tests exist: `tests/test_xrpl_source_tag.py`,
  `tests/test_xumm_source_tag.py`, `tests/test_discord_sourcetag_invariant.py`,
  `tests/test_telegram_sourcetag_invariant.py`, `tests/test_signing_account.py`,
  plus `tests/test_discord_trustline_sourcetag.py` and
  `tests/test_market_payloads.py` (the pair #61's closing comment also cites).
- #75's original target (an inline `NFTokenMint` in `main.py`) was already
  deleted by the spine refactor; `main.py` is a launch shim. Closed as
  obsolete, correctly.

The proposed AST "static sweep" test was **not** built. The per-builder tests
above are what guards the invariant.

### 1.2 Provenance memos (#54, #57's memo half) — closed 2026-07-09

Shipped in PR #144 (`feat(memos): provenance Memos on every XRPL transaction`,
merged 2026-07-09) as **`lfg_core/memos.py`**. The implementation deliberately
took the *issue's* schema, not this spec's counter-proposal:

| Old draft (§3 of the 2026-07-05 text) | Shipped in `lfg_core/memos.py` |
|---|---|
| one Memo, compact JSON `{"v","flow","surface","actor"}` | separate key/value memo entries |
| `actor` / `surface` / `flow` | `initiator` / `platform` / `action` (+ optional `campaign`) |
| `lfg_core/tx_memo.py` | `lfg_core/memos.py` |
| `build_memo` / `build_memo_json` / `parse_provenance` | `build_memo_models` / `build_memos_json` / `decode_memos` / `platform_for_surface` |

Closed enums, a size-budget assertion (`_assert_within_budget`) and the "omit
off-chain user IDs" stance all survived. `memos.platform_for_surface` maps a
service surface name onto the platform enum. Treat `lfg_core/memos.py` as the
source of truth; the old §3 table is superseded.

### 1.3 Residual gaps the closure did not cover

Two sites enumerated in the original audit are **still untagged today**
(verified 2026-08-19). Neither is part of #58; file separately if they matter.

- `lfg_core/xumm_ops.generate_static_payment_link` builds a
  `https://xaman.app/detect/<hex>` Payment txjson with only
  `TransactionType`/`Destination`/`Amount` — no `SourceTag`, no `Memos`. It is
  no longer a payment route: `mint_flow.MintSession.prepare_payment` sets
  `payment_link` from it and then overwrites it with the XUMM payload URL when
  the payload succeeds, and when the payload *fails* both callers gate on
  `payment_uuid is None` and fail the session terminally rather than entering
  the 300s payment wait behind an unparseable detect link (#262,
  `lfg_service/app.py::handle_mint_start` and `mint_flow.run_mint_session`).
  `MintSession.ensure_payment_fallback` still populates the field, so the
  untagged link can still be surfaced on a failed session, but nothing
  advertises it as payable. `lfg_core/swap_flow.py` does not call it at all and
  bulk mint never did; the only remaining callers in-tree are two
  `webapp/test_smoke.py` assertions. Untagged, but no longer a live volume leak.
- `scripts/testnet_amm_setup.py` still constructs `AccountSet` and `AMMCreate`
  with no `source_tag`. Testnet ops tool only; no mainnet volume.

---

## 2. Scope of the live work

Everything below concerns **#58 only**: a pre-submit `simulate` at the backend
submit paths, so a transaction that cannot succeed is refused before it is
signed — no burned fee, no wasted ledger round-trip, and a specific
`engine_result` in the log instead of a generic failure.

Out of scope: retry policy (§3.5), user-signed XUMM payloads (the user's wallet
runs its own pre-flight and we never pay their fee), and the two residual
SourceTag gaps in §1.3.

---

## 3. Design — pre-submit simulation (#58, rebased)

### 3.1 The submit surface today

| Path | Signs at | Submits at | Notes |
|---|---|---|---|
| `xrpl_ops._submit_and_confirm` | `_autofill_and_sign_with_retry` | `submit_and_wait(signed, client, None, autofill=False)` | choke point for `mint_nft`, `create_nft_offer`, `cancel_nft_offer`, `buy_and_burn`, `burn_nft`, `modify_nft` |
| `xrpl_ops.prepare_sponsored_mint` → `submit_sponsored_mint` | `autofill_and_sign` in *prepare* | `submit_and_wait` in *submit* | sign and forward are separated by a durable journal (PR #328); the blob may outlive a restart |
| `xrpl_ops.prepare_sponsored_burn` → `submit_sponsored_burn` | same split | same | LFGO debt discharge |
| `surfaces/discord_bot/admin.py::burn_nft` | implicit (`submit_and_wait(burn_tx, client, wallet)`) | same call | the only tx builder+submitter in a running surface; wrapped in `xrpl_ops.submission_coordinator` |
| `scripts/testnet_amm_setup.py` | implicit | `xrpl.asyncio.transaction.submit_and_wait` ×2 | testnet ops tool |

Every submission in `lfg_core`, plus the Discord admin burn, is serialized per
`Account` by `xrpl_ops.submission_coordinator` (asyncio lock + `flock`'d file),
because autofill reads the account sequence; `scripts/testnet_amm_setup.py`
takes no such lock (hand-run testnet tool). `_submit_and_confirm` takes the lock
itself via `_submission_scope`; no caller of it passes `coordinator_held=True`
today (only the sponsored prepare/submit pairs do, from
`lfg_core/mint_flow.py::mint_one_unit` and
`lfg_core/sponsored_burn.py::process_one`).

### 3.2 What #58 buys on this codebase

The issue's stated motivation ("burns fees or retry budget") is half-stale —
the retry budget is already gone. What remains, and is real:

- **`buy_and_burn` is the strongest case — on mainnet.** With `max_xrp` set it
  is a cross-currency `Payment` carrying `send_max` in drops; it is fired
  best-effort after mint-fee collection (`lfg_core/mint_flow.py`), after swap
  fees (`lfg_core/swap_flow.py` — which passes `max_xrp=None` when the fee was
  paid in BRIX, spending the wallet's existing balance instead), and from the
  shop's XRP buyback (`lfg_service/app.py`). A dry AMM path returns
  `tecPATH_DRY` — fee burned, nothing delivered. Simulation turns that into a
  free, logged no-op. Caveat: `buy_and_burn` short-circuits to the
  `"self-issuer-noop"` sentinel without submitting anything when
  `config.SIGNING_ACCOUNT == issuer`, which is the *default testnet* posture for
  the BRIX pair (`config.SWAP_OFFER_ISSUER` defaults to the SEED address on
  testnet). So this win is a mainnet win, and a staging rehearsal has to pick a
  non-self issuer (see the plan, Task 7).
- **`modify_nft` / `burn_nft` against a token whose owner just changed** — a
  free pre-flight refusal instead of a burned fee. (Which `tec*` rippled
  returns here was not probed; `tecNO_PERMISSION` is the expected one and the
  design does not depend on the specific code.)
- **Sponsored mint/burn** sign a blob that is journaled and may be forwarded
  much later. A pre-sign simulate keeps a doomed identity out of the journal
  entirely, which is worth more here than anywhere else — an unforwardable
  prepared mint has to be reconciled by hand.
- **Diagnosis.** Today `_validated_result` logs `f"{label} result: {tx_result}"`
  only after the fee is spent, and a malformed model surfaces as an opaque
  exception. A simulate result names the engine code before anything is signed.

### 3.3 Placement

Add one helper to `lfg_core/xrpl_ops.py`:

```python
async def _presubmit_simulate(tx: Transaction, client: JsonRpcClient, label: str) -> str | None:
    """Return a deterministic rejection code, or None to proceed."""
```

Contract:

1. **Gated** on `config.env_flag("PRESUBMIT_SIMULATE", "1")` read *at call
   time*, never a frozen module constant (#323 rule: a frozen constant tests
   whatever the ambient env froze at import).
2. Runs on the **unsigned** `Transaction` model. `simulate` rejects a signed
   transaction outright (`transactionSigned`), so it must precede
   `_autofill_and_sign_with_retry` / `autofill_and_sign`. rippled autofills
   `Fee`/`Sequence` for the simulation itself — the model needs no
   pre-population (verified, §3.6).
3. **Runs outside the submission lock.** In `_submit_and_confirm` the call goes
   above `async with _submission_scope(...)`. Simulation does not depend on the
   account sequence, and the per-`Account` critical section is shared with
   fire-and-forget harvests (PR #307) — extending it by the pre-flight's two
   round-trips (§3.6) would serialize real throughput for no benefit.
4. **Fail-closed only on deterministic engine results.** Read `engine_result`
   from `response.result`:
   - `tes*`, `ter*` (which means "retry later", not "wrong") and `tel*` (a
     node-local/queue verdict — `telINSUF_FEE_P`, `telCAN_NOT_QUEUE*`, the
     network-id family — that says nothing about the transaction's validity on
     a different server) → proceed.
   - `tem*` / `tef*` / `tec*` → return the code; the caller aborts. No
     carve-outs — in simulation nothing was burned, so submitting anyway can
     only convert a free warning into a paid failure.
   - Any prefix outside those five → proceed, and log it. An unclassified code
     must never abort real work.
5. **Degrade open on everything else.** `XRPLRequestFailureException` (which
   xrpl-py's `simulate` raises when the *request* fails — unknown method,
   `transactionSigned`, node error), transport errors, timeouts and any
   unexpected response shape → log a warning and proceed to submit. The ledger
   stays the authority; a flaky node must never brick minting.

Call sites, in priority order:

| # | Site | Insert before |
|---|---|---|
| 1 | `xrpl_ops._submit_and_confirm` | `async with _submission_scope(...)` |
| 2 | `xrpl_ops.prepare_sponsored_mint` | `autofill_and_sign` (inside the scope it already holds — acceptable; preparation is not on the hot path) |
| 3 | `xrpl_ops.prepare_sponsored_burn` | same |
| 4 | `surfaces/discord_bot/admin.py::burn_nft` | optional; an admin burn is human-paced. Reuse the helper, do not reimplement. |

`scripts/testnet_amm_setup.py` is out of scope (testnet ops tool, run by hand).

**Caller-visible outcome.** A simulate rejection in `_submit_and_confirm`
returns `None` — the existing "definitive, validated failure" signal every
caller already handles — and never `IndeterminateResultError`, because nothing
was signed, so the outcome is *known*. In the sponsored paths it maps to the
existing `"failed"` preparation state with the engine code as the reason. No
flow-level compensation semantics change.

One second-order effect worth naming: `create_nft_offer` deliberately collapses
*every* failure — including `IndeterminateResultError` — to `None`, and its
callers read that as "the offer may still have landed, go look on-ledger"
(#211, `swap_flow._create_offer_and_accept`). A simulate rejection there
therefore costs one wasted recovery scan, which finds nothing and lets the flow
fail cleanly. Correct, just not free.

### 3.4 Reserve check — cut, not deferred

The draft's §4.1 step 3 (fetch `ServerState` + `AccountInfo`, model an
owner-count delta per tx type, compare integer drops against
`fee + xrp_outflow`) is **removed from the design**:

- rippled performs exactly this arithmetic during simulated apply, against the
  live ledger and with the real per-type owner-object delta. Verified: an
  over-balance mainnet Payment simulates `tecUNFUNDED_PAYMENT`.
  `tecINSUFFICIENT_RESERVE` comes out of the same apply path (expected, not
  directly probed — confirm with an underfunded testnet account; see the plan).
- A hand-rolled owner-delta table is a permanent maintenance liability that
  drifts from amendments, and being wrong in the *pessimistic* direction blocks
  legitimate work — worse than the fee it saves.
- Two extra reads per transaction, on top of the simulate pre-flight, for a
  strictly weaker answer.

What #58 legitimately wants that simulate does not give: an operator-level
alarm when the signing account's XRP trends toward the reserve floor. That is a
monitoring concern, not a per-transaction one — the nearest existing home is
`scripts/audit_sponsored_mint_readiness.py`, which already computes a
required-balance check (for LFGO debt, not XRP). Track it separately if wanted;
it is not a prerequisite for #58.

### 3.5 Retry — explicitly not reinstated

The old §4.1 step 5 table (retry on transport / `ter*` / `telINSUF_FEE_P`,
re-autofill on `tefPAST_SEQ` / `tefMAX_LEDGER`) **must not be built**. It
contradicts the invariant PR #188 established for #179: a raised
`submit_and_wait` does not mean the transaction failed — it polls to
`LastLedgerSequence`, so resubmitting risks a duplicate mint. The current code
instead looks the signed hash up (`_confirm_by_hash`) and raises
`IndeterminateResultError` when the outcome is unknowable, which the trait
economy's phase-aware taxonomy (#107) depends on.

Every retry left in `xrpl_ops.py` is *pure-read* and bounded.
`_tx_lookup_with_retry` and `_autofill_and_sign_with_retry` retry only the
rippled "HTTP 200 with no `result` key" shape (#385/#386, detected by
`_is_malformed_result_error`); `_confirm_by_hash` polls an already-signed hash
for a bounded `attempts` (default 3) and never submits. A simulate
implementation may reuse `_is_malformed_result_error` for that same body shape;
it must add no other retry.

### 3.6 Verification performed (2026-08-19)

Run against the shipped `config.JSON_RPC_URL` defaults
(`https://s1.ripple.com:51234/`, `https://s.altnet.rippletest.net:51234/`),
raw JSON-RPC, no signing, nothing submitted:

| Probe | Result |
|---|---|
| `simulate` of an unsigned XRP Payment, mainnet | `status: success`, `engine_result: tesSUCCESS`, `applied: false` |
| same, testnet | `status: success`, `engine_result: tesSUCCESS`, `applied: false` |
| over-balance Payment (99,999,999 XRP), mainnet | `engine_result: tecUNFUNDED_PAYMENT`, "Insufficient XRP balance to send." |
| self-destination Payment, mainnet | `engine_result: temREDUNDANT` |
| tx carrying `SigningPubKey`/`TxnSignature` | request error `transactionSigned` — "Transaction should not be signed." |
| keys on a success response | `applied`, `engine_result`, `engine_result_code`, `engine_result_message`, `ledger_index`, `meta`, `status`, `tx_json` (with `Fee`/`Sequence` autofilled, empty `SigningPubKey`) |

Every row above was reproduced independently on 2026-08-19, plus one end-to-end
library call — `xrpl.transaction.simulate(Payment(...), client)` against
testnet returned `engine_result=tesSUCCESS`, `applied=False` on an unsigned
model built by xrpl-py, so the empty `SigningPubKey` a `Transaction` serializes
by default is not read as "signed".

One dependency the probes exposed: the mainnet default
(`https://s1.ripple.com:51234/`) answers as a **clio** server and *forwards*
`simulate` to rippled — every mainnet response carries `"forwarded": true` and
the "This is a clio server" warning. The pre-flight therefore rides on that
forwarding staying enabled; if it stops, `simulate` starts erroring and the
degrade-open rule (§3.3 step 5) is what keeps minting alive.

Library: `xrpl.asyncio.transaction.simulate(transaction, client, *, binary=False)`
and its sync twin `xrpl.transaction.simulate` — present with the identical
signature, and raising the same `XRPLRequestFailureException`, in the locally
installed 4.5.0 and in the 5.0.0 wheel (PyPI's current latest, checked
2026-08-19). `requirements.txt` does not pin a version, so the implementation
must not depend on a 5.x-only symbol.

**Hidden cost:** xrpl-py's `simulate` first calls
`get_network_id_and_build_version`, which issues a `ServerInfo` request unless
the client object has already cached `network_id`/`build_version` — and
`xrpl_ops` constructs a fresh `JsonRpcClient(config.JSON_RPC_URL)` per
operation. So each pre-flight is **two** round-trips, not one, unless a client
is reused. Budget for that in §3.8.

The sync/async split matters: `_submit_and_confirm` holds a sync
`JsonRpcClient` and calls it through `asyncio.to_thread`. Use
`xrpl.transaction.simulate` in a thread, matching the surrounding style (the
sync wrapper is `asyncio.run` around the async one, so it needs a thread with
no running loop), and import it as a module-level name in `xrpl_ops` so tests
can monkeypatch it the way they already patch `submit_and_wait`.

### 3.7 Configuration and test isolation

- New env var `PRESUBMIT_SIMULATE` (default `1`), read through
  `config.env_flag` at call time. Add it to the CLAUDE.md env block.
- **The suite must default it off.** Seven modules drive `_submit_and_confirm`
  with `autofill_and_sign` and `submit_and_wait` stubbed —
  `tests/test_xrpl_source_tag.py`, `test_xrpl_indeterminate.py`,
  `test_xrpl_malformed_poll_retry.py`, `test_xrpl_submit_lock.py`,
  `test_memos_transactions.py`, `test_nft_flags.py`, `test_signing_account.py`
  — and three more stub `autofill_and_sign` for the sponsored prepare paths
  Task 4 wires (`test_sponsored_burn.py`, `test_sponsored_burn_review.py`,
  `test_sponsored_final_review.py`). Several of them also patch
  `xrpl_ops.JsonRpcClient.request`, which does **not** cover a simulate: xrpl-py
  issues it (and its `ServerInfo` preflight) through `client._request_impl`. So
  a live simulate from those tests would be a real network request. Pin
  `os.environ.setdefault("PRESUBMIT_SIMULATE", "0")` in the root `conftest.py`
  — per #323 that is the only place an import-time-frozen value can be pinned
  suite-wide, and `setdefault` keeps the explicit-export escape hatch for tests
  that *do* exercise the feature.
  (`tests/test_shop_offer_builder.py` is **not** in this set — it stubs
  `_submit_and_confirm` itself and is unaffected either way.)
- With the flag off simulate never runs, so no existing test changes behavior;
  and because the helper degrades open, a test that turns it on without network
  access still proceeds to submit rather than failing.

### 3.8 Risks

- **False negative.** A simulate reporting `tec*` where the real submit would
  have succeeded aborts real work. Mitigated by simulating against the same
  endpoint we submit to (`config.JSON_RPC_URL`), by the flag, and by the `None`
  return being a signal every caller already handles.
- **Latency.** Two extra requests per backend transaction, not one: the
  `ServerInfo` preflight plus the `simulate` itself, because `xrpl_ops` builds a
  fresh `JsonRpcClient` per op (§3.6). Bulk mint pays that twice per unit (mint
  + offer) — four extra requests per NFT. Measure before enabling on prod; the
  flag is the rollback. Reusing one client across a unit, or priming
  `network_id`/`build_version` once, halves it if the measurement warrants.
- **Placement drift.** A future builder that signs before the helper runs
  silently loses the pre-flight and gets a `transactionSigned` error swallowed
  by degrade-open. The plan's "simulate is called with an unsigned model" test
  is the guard.

---

## 4. Issue disposition

| Issue | State |
|---|---|
| #61 | CLOSED 2026-07-09. Two residual untagged sites remain (§1.3) — not reopened here. |
| #75 | CLOSED 2026-07-10 as obsolete. |
| #57 | CLOSED 2026-07-09 as a duplicate of #54 (its SourceTag half was already covered by #61). |
| #54 | CLOSED 2026-07-09 by PR #144 as `lfg_core/memos.py`. Schema differs from this doc's original §3 — see §1.2. |
| #58 | OPEN. §3 above is the live design. Closes when `_presubmit_simulate` is wired at the paths in §3.3 with the flag defaulted on in prod. |
