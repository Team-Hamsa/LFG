# XLS-56 Batch: single-signature accept of multiple NFT offers — design

**Date:** 2026-07-24
**Status:** live (blocked) — re-reviewed 2026-08-19 against main@27dc301;
blocked on the XLS-56 amendment itself (`Batch` was marked unsupported in
rippled 3.1.1 and replaced by `BatchV1_1`, still in mainnet voting — see the
2026-08-18 dependency-verification comment on issue #219)
**Issue:** #219
**Last review:** 2026-08-19

> **Do not start building.** The gating dependency is not "unverified" any
> more — it is negative. Per the 2026-08-18 dependency check on #219 (the only
> source; nothing about live amendment state is checkable from this tree), a
> `Batch` transaction cannot be enabled on any production network, so every
> line of code below would ship dead and the
> wire shape it encodes is for an amendment that has been withdrawn. The
> design is kept because it is still the right shape once `BatchV1_1`
> activates; treat §2 (Blocked-on) as the entry point and §4 as reference.

## 0. What changed since this was drafted

- **The central assumption reversed.** The draft treated XLS-56 activation as
  merely unverified. Per the 2026-08-18 comment on #219: a signer-validation
  bug found 2026-02-19 led rippled 3.1.1 (2026-02-23) to mark `Batch` and
  `fixBatchInnerSigs` **unsupported** — they can never be enabled on a
  production network. The replacement `BatchV1_1` is in mainnet voting, not
  enabled. (Amendment status is not offline-checkable from this tree; that is
  the comment's finding, with its date, not an independent verification.)
- **xrpl-py support re-verified locally.** `requirements.txt` still pins
  `xrpl-py` unversioned (line 8 is the bare name). The version checked here is
  the host's user-site install, 4.5.0, NOT the deployed `.venv`: there
  `xrpl.models.transactions.Batch` imports, `BatchFlag` exposes
  `TF_ALL_OR_NOTHING`/`TF_ONLY_ONE`/`TF_UNTIL_FAILURE`/`TF_INDEPENDENT`
  (`TF_INDEPENDENT == 0x80000`), and
  `TransactionFlag.TF_INNER_BATCH_TXN == 0x40000000`. The 2026-08-18 comment
  reports the deployed `.venv` at 5.0.0 with the same import plus
  `combine_batch_signers()`. **Both model the original `Batch`, not
  `BatchV1_1`** — the builder below may need rework when that lands.
- **The inner wire shape is now partly answered offline** (it was fully open
  in the draft). `Batch(...).to_xrpl()` emits
  `RawTransactions: [{"RawTransaction": {...}}]` with the inner `Account`
  repeated, inner `Flags = 0x40000000` and inner `SigningPubKey: ""` (no inner
  `Fee` unless set). `xrpl.asyncio.transaction.main._autofill_batch` shows what
  the ledger wants: it *fills* a missing inner `Fee: "0"` / `SigningPubKey: ""`
  (and rejects either if present with any other value), rejects an inner
  `TxnSignature` / `Signers` / `LastLedgerSequence`, and — for a single-account
  batch — fills each missing inner `Sequence` as `outer.Sequence + 1, +2, …`.
  It asserts the **outer** `Sequence` is already known before doing so, which
  is exactly the field XUMM fills at sign time, after we hand it a txjson. That
  is a new, concrete problem for a XUMM-signed payload (§3, first constraint).
- **The 8-inner cap is ours to enforce.** `Batch._get_errors` rejects only two
  things: fewer than 2 inner transactions, and an inner tx missing the
  `tfInnerBatchTxn` flag. The library imposes no upper bound. The cap of 8 is a
  protocol rule (per the 2026-08-18 comment), so `BATCH_ACCEPT_MAX_INNER` is
  load-bearing, not belt-and-braces.
- **More producers now feed the tray.** Burn-to-mint (#397,
  `lfg_core/burn2mint_flow.py`, whose mints ride `bulk_mint_flow` and land as
  gift offers) drops N free destination-locked offers like bulk mint does. The
  sponsored free-mint campaign (#328) adds at most **one** — `free_mint_claims`
  is `UNIQUE (network, wallet)` (`lfg_core/sponsored_mint.py`), so it deepens
  the tray across users rather than giving one user N to claim.
- **The tray is per-kind since #327.** `lfg_service/app.py::_pending_offer_row`
  resolves characters from `onchain_nfts` on `config.XRPL_NETWORK` and
  Extract-minted trait tokens from `trait_tokens` on `config.ECONOMY_NETWORK`.
  A batch endpoint must not assume character-only rows.
- **Sign delivery moved on, shrinking the UX gap.** Accepts render through
  `applySignDelivery` (defined in `webapp/client/app.js`, deciding via the pure
  `webapp/client/signdelivery_pure.js::signDelivery`, #142/#380). On a
  coarse-pointer device the deep link is primary and the QR collapses; on
  desktop the QR also collapses once push (#135/#212) reports `sent`, but stays
  primary otherwise. So "ten QR scans" is now closer to "ten taps" on mobile,
  and unchanged on a desktop with no push. One signature is still better, but
  this is a polish win, not a rescue.
- **A JS unit harness exists now.** `tests/test_*_pure_js.py` executes the pure
  ES modules under Node (e.g. `tests/test_harvest_pure_js.py` over
  `webapp/client/harvest_pure.js`). The plan's "no JS unit harness exists" is
  stale.
- **Multi-select has a shipped precedent**: batch harvest (#356, PR #379) —
  `harvest_pure.js` (`toggleSelected`, `batchSummary`, `pruneSelection`,
  `splitBatchResults`) plus `BATCH_HARVEST_MAX = 20` in `lfg_service/app.py`.
  Copy that shape rather than inventing one.
- **Test env changed (#323).** The root `conftest.py` pins the suite
  environment centrally; new test files need no env-guard preamble.
- **Client asset pins moved.** The stylesheet is `webapp/client/style.v24.css`
  (the plan said `style.v22.css`) and `webapp/client/index.html` loads
  `app.js?v=59`, with per-module `?v=` pins on the ES imports inside `app.js`.
- **Nothing shipped for this issue.** No `BATCH_ACCEPT_*` symbol exists under
  `lfg_core/` or `lfg_service/`; `gh pr list --search "XLS-56"` returns only
  unrelated batch-named PRs (#225, #300, #379, #397).

## 1. Problem

Bulk mint (#215) and burn-to-mint (#397) each deliver N free
`NFTokenCreateOffer`s destination-locked to the recipient (the sponsored
campaign, #328, adds one per wallet). Each offer still needs its own
`NFTokenAcceptOffer` signature:

- `lfg_service/app.py::handle_pending_offer_accept` (`POST /api/offers/accept`)
  builds ONE XUMM accept payload per row, on click, via
  `xumm_ops.create_accept_offer_payload`; the client is
  `webapp/client/app.js::offerAccept`.
- Every accept is a separate XUMM payload, so a full claim pushes against the
  open-payload cap (#260 — `xumm_ops.DEFAULT_EXPIRE_MINUTES = 15` mitigates)
  and the per-minute quota (#254).

XLS-56 would let one signature carry up to 8 inner transactions, collapsing a
claim into one Xaman confirmation.

## 2. Blocked-on

| Dependency | State (source) | Unblocks |
| --- | --- | --- |
| XLS-56 `Batch` amendment | **Dead** — unsupported since rippled 3.1.1 (#219 comment, 2026-08-18) | nothing; superseded |
| `BatchV1_1` amendment | In mainnet voting, not enabled (same source) | everything below |
| xrpl-py models `BatchV1_1` | Unknown — 4.5.0 (verified here) and 5.0.0 (per that comment) model the original `Batch` | builder correctness |
| Xaman signs a single-account Batch payload | Unverified; no Xaman release note or payload doc found (same source) | flipping the gate anywhere |
| Xaman autofills inner `Sequence`/`Fee` | Unverified — new question, see §3 | whether a XUMM-built Batch is viable at all |

**Revisit trigger:** `BatchV1_1` reaching high validator support / enabling on
mainnet. Then re-verify in this order: (1) amendment enabled on testnet and
mainnet; (2) xrpl-py's model matches `BatchV1_1` wire semantics; (3) Xaman
signs a single-account Batch end-to-end on testnet. Only then implement.

## 3. Constraints (verified against this tree unless noted)

- **Inner-transaction sequencing is the sharpest unknown.** Per
  `_autofill_batch` in the installed xrpl-py, inner txns carry `Fee: "0"`,
  `SigningPubKey: ""` and a `Sequence` chained off the outer Batch's own
  sequence (`outer.Sequence + 1, +2, …`); that helper can only compute the
  chain because it is handed an outer `Sequence` that is already set. Our
  payload builders never set `Sequence`/`Fee` at all (see
  `create_accept_offer_payload`'s txjson) — XUMM fills them at sign time, i.e.
  after our txjson is built, and there is no evidence it descends into
  `RawTransactions`. So either Xaman implements Batch-aware autofill, or the
  backend must pin the outer `Sequence` **and** every inner one from a
  just-read account sequence — which is a TOCTOU: any other transaction the
  wallet signs in the meantime invalidates the whole batch (`tefPAST_SEQ`).
  Resolve this before writing the builder.
- **SourceTag + memos (#54).** `xumm_ops._create_xumm_payload` `setdefault`s
  `SourceTag` and `Memos` on the txjson it is handed, i.e. on the **outer**
  Batch — the transaction that is signed. Whether hackathon volume credit
  counts inner transactions that carry no tag is an open decision flagged in
  the 2026-08-18 comment; if inner tags are needed the builder must stamp them
  itself, since `_create_xumm_payload` never descends into `RawTransactions`.
  Action/initiator constants are `memos.ACTION_ACCEPT_OFFER`,
  `memos.INITIATOR_USER`, platform via `memos.platform_for_surface(...)`.
- **`Batch` requires ≥2 inner txns** (`Batch._get_errors`), so a one-offer
  selection must fall back to the existing single-offer path. The ≤8 cap has
  no library enforcement (`_get_errors` checks only the ≥2 floor and the
  per-inner `tfInnerBatchTxn` flag) — we enforce it.
- **Inner `tfInnerBatchTxn` (0x40000000)** is set automatically by the model's
  `__post_init__`; a hand-built txjson must set it explicitly.
- **Only free gifts are in scope.** `xrpl_ops.filter_claimable_offers` keeps
  only sell-flagged, unexpired offers whose `destination` is the caller and
  whose `amount == "0"` — priced Trait Shop offers are deliberately excluded so
  a user can never unknowingly sign a charging transaction. A batch of frees is
  therefore low-blast-radius.
- **Signer pinning** (2026-07-21 wrong-wallet incident): the outer `Account`
  and every inner `Account` pin to the caller's wallet, matching
  `create_accept_offer_payload(account=...)`.
- **Fail-closed re-verification**, as in `handle_pending_offer_accept`: every
  offer index is re-checked on-ledger immediately before the payload is built,
  via `xrpl_ops.get_account_nft_offers(xrpl_ops.bot_wallet_address())` →
  `filter_claimable_offers(offers, wallet, time.time())`. Note the lookup is
  against the OFFER CREATOR (the signing account), not the caller's wallet;
  the caller is the `destination` filter. An offer that has gone away is
  dropped from the batch, not failed as a whole request.
- **No custody.** The Batch is user-signed; abandoning it leaves every offer
  live and claimable, exactly like abandoning a single accept.
- **Network seam.** Offers themselves come from the signing account on
  `config.XRPL_NETWORK`; only the *display* join is per-kind
  (`_pending_offer_row`). Batch accept inherits that unchanged.

## 4. Design (reference — do not implement yet)

Four seams behind one feasibility gate.

### 4.0 Feasibility gate (`lfg_core/config.py`)

```python
BATCH_ACCEPT_ENABLED_DEFAULT = "0"  # named so a test can lock the shipped default
BATCH_ACCEPT_ENABLED = env_flag("BATCH_ACCEPT_ENABLED", BATCH_ACCEPT_ENABLED_DEFAULT)
BATCH_ACCEPT_MAX_INNER = int(os.getenv("BATCH_ACCEPT_MAX_INNER", "8"))  # XLS-56 cap
```

Same shape as `BULK_MINT_UI_ENABLED_DEFAULT` / `BURN_TO_MINT_ENABLED_DEFAULT`.
Gate OFF ⇒ the endpoint answers `409 batch_disabled` and the client never
renders the control; behavior is byte-for-byte today's per-offer tray.

### 4.1 Payload builder (`lfg_core/xumm_ops.py`)

`create_batch_accept_payload(account, offer_ids, *, return_url=None,
user_token=None, platform=memos.PLATFORM_BACKEND, campaign=None)` builds:

```
{"TransactionType": "Batch",
 "Account": <caller>,
 "Flags": 0x00080000,                       # BatchFlag.TF_INDEPENDENT
 "RawTransactions": [
   {"RawTransaction": {"TransactionType": "NFTokenAcceptOffer",
                       "Account": <caller>,
                       "NFTokenSellOffer": <offer_index>,
                       "Flags": 0x40000000, # TF_INNER_BATCH_TXN
                       "Fee": "0",
                       "SigningPubKey": ""}},
   ...]}
```

and hands it to `_create_xumm_payload(txjson,
options=_with_return_url({}, return_url), user_token=user_token,
memos_json=memos.build_memos_json(memos.INITIATOR_USER, platform,
memos.ACTION_ACCEPT_OFFER, campaign))` — `_with_return_url` sets the
`DEFAULT_EXPIRE_MINUTES` (15) `expire`, `_create_xumm_payload` stamps
`SourceTag`/`Memos` and returns the `qr_url`/`xumm_url`/`push` dict. Caller
guarantees `2 <= len(offer_ids) <= BATCH_ACCEPT_MAX_INNER`. Shape mirrors the
existing `create_accept_offer_payload`.

`TF_INDEPENDENT` over `TF_ALL_OR_NOTHING`: one inner accept failing (an offer
claimed seconds earlier on another device) must not void the rest — matching
the tray's "claim what is still there" behavior.

**Build the txjson from the model, not by hand, if possible.** Constructing
`Batch(...)` and taking `.to_xrpl()` (dropping the empty outer `SigningPubKey`
XUMM will fill) makes wire-shape drift arrive as a library upgrade rather than
a silent mismatch — relevant precisely because `BatchV1_1` will change it. The
inner `Sequence` question in §3 must be settled either way.

### 4.2 Service endpoint (`lfg_service/app.py`)

`@require_wallet handle_pending_offers_accept_batch` on
`POST /api/offers/accept-batch`, registered beside the existing
`app.router.add_get("/api/offers/pending", …)` /
`add_post("/api/offers/accept", …)` pair:

1. `not config.BATCH_ACCEPT_ENABLED` → `409 {"code": "batch_disabled"}`.
2. `config.WEBAPP_DEV_MODE` → `501` (mirrors the single-offer handler).
3. Body `offer_indices: list[str]`; reject empty/non-string → `400`.
4. Re-verify on-ledger; lookup failure → `503 pending_unavailable`. Intersect
   with the request, preserving request order.
5. `0` survivors → `410 offer_gone`; exactly `1` → `{"single": true,
   "offer_index": …}` so the client falls back to `offerAccept`.
6. Chunk survivors at `config.BATCH_ACCEPT_MAX_INNER`; one
   `create_batch_accept_payload` per chunk with `account=wallet`,
   `user_token=await _push_token(request["user"])`,
   `platform=memos.platform_for_surface(_platform(request["user"]))`,
   `return_url=await _request_return_url(request)`. Return
   `{"batches": [{"qr", "link", "push", "count"}, …]}` — a 10-offer claim is 2
   signatures, still far better than 10.

`GET /api/offers/pending` gains `"batch": config.BATCH_ACCEPT_ENABLED` so the
client never assumes support. `POST /api/offers/accept` is untouched.

### 4.3 Client (`webapp/client/app.js`, `index.html`, `style.v24.css`)

Selection logic goes in a new pure module (`offers_pure.js`) tested under Node
like `harvest_pure.js`, not inline in `app.js`. In `openOffers()`, when the
server advertises `batch` and `offers.length >= 2`, `offerRow` gains a checkbox
and the panel gains a sticky "Accept selected (1 signature)" button that POSTs
the checked `offer_index` list and renders one `.u-accept` block per returned
chunk through the existing `applySignDelivery` path (`signText`,
`makeQrToggle`). `{single: true}` routes back through `offerAccept`. Gate off
⇒ today's per-row list, unchanged.

Any `app.js`/`index.html` change bumps the `?v=` cache-buster (currently
`app.js?v=59`) in the same commit, and a new ES module import gets its own
`?v=` pin — per repo convention.

## 5. Open questions

1. **Inner `Sequence`/`Fee` autofill in Xaman** (§3) — the make-or-break wire
   question; supersedes the draft's vaguer "exact inner shape" question, which
   xrpl-py has now answered for the original amendment.
2. **`BatchV1_1` wire deltas** vs the `Batch` the library models today.
3. **Volume attribution**: does an inner transaction with no `SourceTag` count
   for hackathon volume, and if not, do we stamp every inner tx (bigger
   payload, more memo budget) or accept outer-only credit?
4. **Flag choice** `TF_INDEPENDENT` vs `TF_ALL_OR_NOTHING` — re-confirm the
   semantics survive into `BatchV1_1`.
5. **Rollout order**: testnet/staging gate flip after a real Xaman testnet
   sign; prod only after mainnet activation plus a mainnet sign.
6. **Is it still worth it** given push/deep-link delivery (#142/#380/#212) cut
   the per-accept friction? Re-judge at unblock time; the honest answer today
   is "nice, not urgent".

## 6. Testing (when unblocked)

- **Unit, builder** (`tests/test_batch_accept_payload.py`, fake
  `_post_xumm_payload`): outer is `TransactionType: "Batch"` with `Account`
  pinned and `len(RawTransactions) == len(offer_ids)`; each inner is an
  `NFTokenAcceptOffer` with the caller's `Account`, `Flags & 0x40000000`,
  `Fee == "0"`, `SigningPubKey == ""`; outer carries `SourceTag ==
  config.SOURCE_TAG` and a `Memos` block.
- **Unit, chunking**: 1 → single fallback, 2 → one batch, 8 → one batch,
  10 → 8+2, 16 → 8+8.
- **Unit, endpoint**: gate off → 409 with no XUMM call; gate on → payload per
  chunk; a no-longer-claimable index is dropped; 0 survivors → 410; 1 → single.
  Reuse `tests/test_pending_offers.py`'s helpers (`_offer(**kw)`, the `WALLET`
  constant) — that file has no pytest fixtures.
- **Route smoke**: add `"/api/offers/accept-batch"` to the expected-path list
  in `webapp/test_smoke.py::test_routes_registered`. That list holds canonical
  path strings only (no methods), and neither `/api/offers/pending` nor
  `/api/offers/accept` is in it yet, so this is an addition, not an edit.
- **JS**: `tests/test_offers_pure_js.py` over the new selection module, same
  harness as `tests/test_harvest_pure_js.py`.
- **Manual (testnet, post-Xaman verification)**: mint 3 to a test wallet,
  select all 3 in the tray, sign once, confirm all 3 land and the on-ledger
  Batch carries `SourceTag 2606160021` + the accept-offer memo.

## 7. Out of scope

- **Marketplace multi-buy** (priced sell offers/bids): money moves, per-offer
  amount re-verification and per-kind denomination (#239) apply, and partial
  fills have settlement consequences the gift path does not.
- **Batching the burn side of burn-to-mint** (#397 signs each `NFTokenBurn`
  separately via `burn2mint_flow.start_next_burn`; that module's own header
  already names XLS-56 as the thing that would collapse the loop) — a natural
  second consumer of the same builder, but a separate flow with its own
  fail-closed ordering. Note it at unblock time; do not fold it in here.
- **Batch of `NFTokenCreateOffer`** (the delivery side) — #219 is the accept
  side only.
- **Discord-bot / Telegram batch UI** — the tray is Activity/web only.
