# XRPL Transaction Hygiene — Consolidated Design

**Issues:** #61 (SourceTag everywhere) · #75 (inline /letsgo mint SourceTag) ·
#57 (memos + source_tag) · #54 (provenance Memo schema) · #58 (pre-submit
simulate / reserve check) · **Date:** 2026-07-05 · **Status:** Draft

One spec for the whole "tx hygiene" family: (1) close the last SourceTag gaps
and lock them shut with a choke-point invariant, (2) a provenance Memo schema,
(3) pre-submit simulation + reserve check at a single submit choke point.

---

## 0. Reality check — most of #61/#75 is already done

The issues were filed against a codebase that no longer exists in that shape.
Since the shared-services spine refactor (#76–#81), `main.py` is an **8-line
launch shim** (`main.py:1-8`) delegating to `surfaces/discord_bot/bot.py`; the
inline `NFTokenMint` at "~main.py:365/374" that #75 targets **was deleted**
when the mint pipeline moved into `lfg_core/mint_flow.py` → `xrpl_ops.mint_nft`
(which sets `source_tag`, `lfg_core/xrpl_ops.py:58`). The trustline module even
carries the fix marker: `surfaces/discord_bot/trustline.py:42` — `"SourceTag":
SOURCE_TAG,  # Make Waves invariant (#75)`.

Guard tests already exist and pass:
`tests/test_xrpl_source_tag.py` (mint/offer/burn/modify/buy_and_burn),
`tests/test_xumm_source_tag.py` (payment/accept-offer payloads; SignIn exempt),
`tests/test_discord_sourcetag_invariant.py` + `test_discord_trustline_sourcetag.py`
(the two surface-local inline paths: trustline TrustSet, admin burn).

**#75 disposition: obsolete — close with a comment**, no code change (§7).

## 1. Audit — every transaction-building / submitting site

`ts_helpers.py` no longer exists (legacy, removed with the spine refactor).
Exhaustive repo grep for tx constructors, `submit_and_wait`, and `txjson`
(excluding `.venv`, `backup/`, tests) yields exactly these sites:

| # | Site (file:line) | Tx type | Signer | SourceTag | Memo | Live? |
|---|---|---|---|---|---|---|
| 1 | `lfg_core/xrpl_ops.py:66` (`mint_nft`, tag at :58) | NFTokenMint | bot | **yes** | no | live — mint_flow:274, swap_flow:403, economy flows |
| 2 | `lfg_core/xrpl_ops.py:114` (`create_nft_offer`, :120) | NFTokenCreateOffer | bot | **yes** | no | live — mint_flow:332, swap_flow:257, economy |
| 3 | `lfg_core/xrpl_ops.py:331` (`buy_and_burn`, :327) | Payment (IOU / cross-currency) | bot | **yes** | no | live — mint_flow:231, swap_flow:326 |
| 4 | `lfg_core/xrpl_ops.py:357` (`burn_nft`, :353) | NFTokenBurn | bot | **yes** | no | live — swap_flow:211/449, harvest, deposit |
| 5 | `lfg_core/xrpl_ops.py:409` (`modify_nft`, :405) | NFTokenModify | bot | **yes** | no | live — swap_flow:238/428, equip |
| 6 | `lfg_core/xumm_ops.py:142-149` (`_create_xumm_payload`) | any txjson (Payment, NFTokenAcceptOffer; SignIn exempt) | user (Xaman) | **yes** — central `setdefault("SourceTag", …)` at :149 | no | live — all XUMM QR payloads |
| 7 | `lfg_core/xumm_ops.py:41-52` (`generate_static_payment_link`) | Payment (xaman.app/detect deep link) | user (Xaman) | **NO — GAP** | no | **LIVE** — mint_flow.py:104,133; swap_flow.py:309 |
| 8 | `surfaces/discord_bot/trustline.py:40-46` | TrustSet (XUMM payload, built inline) | user | **yes** (:42) | no | live |
| 9 | `surfaces/discord_bot/admin.py:44-52` | NFTokenBurn (admin burn) | bot | **yes** (:47) | no | live |
| 10 | `scripts/testnet_amm_setup.py:99-106` | AccountSet (DefaultRipple) | bot | **NO — gap** | no | ops tool, testnet only, rerun after resets |
| 11 | `scripts/testnet_amm_setup.py:130-142` | AMMCreate | bot | **NO — gap** | no | ops tool, testnet only |

Not tx sites (checked, read-only): `nft_info`/`nft_exists`/`get_amm_xrp_cost`/
`get_trustline_balance`/`wait_for_payment` (xrpl_ops), all listeners/backfill/
snapshot scripts, `history_events.py`/`history_store.py` (they *read*
`SourceTag`/`Memos` off archived txs — relevant to §4.5). The economy scripts
(`economy_*.py`, `migrate_bucket_to_closet.py`, `closet_token.py`,
`economy_flow.py`) build no txs directly — everything routes through
`xrpl_ops` / `xumm_ops` (verified by grep; e.g. economy_bootstrap_char.py:16
"All txns carry SourceTag via xrpl_ops"). `lfg_service`/`webapp` build no
txjson — they call `xumm_ops.create_*_payload`.

### Headline finding

**Site 7 is the real, live hackathon-credit leak today** — the exact failure
mode #75 described, relocated. `generate_static_payment_link` hex-encodes a
Payment txjson into an `https://xaman.app/detect/<hex>` deep link **without
`SourceTag`**, and it is the payment path for both the **mint fee**
(mint_flow.py:104,133 — including the WEBAPP/Activity path) and the
**swap fee** (swap_flow.py:309). Every user paying a mint/swap fee through
the deep link (rather than a `_create_xumm_payload` QR) produces an untagged
Payment. Sites 10-11 are testnet-only ops scripts (no mainnet volume) but
violate #61's "no exceptions" and should be fixed for completeness.

## 2. Design A — SourceTag closure + regression lock (#61, #75)

### 2.1 Fixes (small)

1. **`generate_static_payment_link`** (xumm_ops.py:47): add
   `"SourceTag": config.SOURCE_TAG` to `transaction_json`. **ASSUMPTION, not
   verified:** the detect-link format is plain txjson hex and `SourceTag` is a
   standard common field, so we *expect* Xaman's detect flow to carry it
   through signing — but this has not been tested. **PR-1's testnet
   verification MUST sign one deep-link payment in Xaman and confirm
   `SourceTag` on the validated tx before #61 is closed.** Fallback if Xaman
   strips it: route the mint-fee/swap-fee payment through
   `_create_xumm_payload` (which stamps server-side, xumm_ops.py:149) instead
   of the static detect link, at the cost of a payload API call per payment.
2. **`scripts/testnet_amm_setup.py`**: add `source_tag=config.SOURCE_TAG` to
   the `AccountSet(...)` (:100) and `AMMCreate(...)` (:131) constructors.

### 2.2 Enforcement mechanism — assert at the submit choke point

Rather than trusting N builder call sites, enforce the invariant where every
bot-signed tx already funnels: this spec introduces **`xrpl_ops.submit_checked()`**
(§4) as the single wrapper around `submit_and_wait`. Its first act, before any
network I/O, is:

```python
if tx.source_tag != config.SOURCE_TAG:
    raise TxHygieneError(f"{tx.transaction_type} missing Make Waves SourceTag")
```

Fail-closed: a builder that forgets the tag can no longer submit at all — the
regression becomes a loud unit-test/runtime failure instead of silent lost
hackathon credit. The user-signed side already has its choke point
(`_create_xumm_payload:149` stamps every non-SignIn txjson); we keep the
`setdefault` (stamp, don't reject — payload callers are user-facing) and add
the same stamp inside `generate_static_payment_link`.

### 2.3 Regression tests

- Extend `tests/test_xumm_source_tag.py`: decode the hex tail of
  `generate_static_payment_link(...)` and assert
  `json.loads(bytes.fromhex(tail))["SourceTag"] == 2606160021`.
- New `tests/test_tx_hygiene.py` (env-guard preamble verbatim from
  `tests/test_seasons.py:1-18`):
  - `submit_checked` raises on a tx with missing/wrong `source_tag`
    (never touches the network — pass a client stub that fails the test if
    called).
  - **Static sweep**: `ast`-parse `lfg_core/xrpl_ops.py` + `scripts/
    testnet_amm_setup.py` and assert every `Call` whose func name is in
    `{NFTokenMint, NFTokenCreateOffer, NFTokenBurn, NFTokenModify, Payment,
    AMMCreate, AccountSet, TrustSet}` has a `source_tag` keyword (or `**kwargs`
    from a dict literal containing the `"source_tag"` key). This is the
    practical "grep test" #61 asked for: it fails the moment someone adds a
    builder without the tag, before it's ever wired to a flow.
  - Assert `scripts/testnet_amm_setup.py`'s two constructors carry the tag
    (import-free: reuse the AST sweep).

Existing per-builder tests (test_xrpl_source_tag.py etc.) stay as-is.

## 3. Design B — provenance Memo schema (#54, absorbing #57's memo half)

No `Memos` exist anywhere in the codebase today — full greenfield.

### 3.1 Schema — one compact JSON memo, not four key/value memos

#54 sketched one `Memo` entry per field (`initiator`, `platform`, …). We
instead emit a **single** `Memo` whose `MemoData` is compact JSON — fewer
bytes on-chain (each Memo entry adds object overhead toward the 1 KB cap),
one decode for consumers, and one place to version:

```json
{"v":1,"flow":"mint","surface":"discord","actor":"user"}
```

| Field | Type | Values (closed enums in `lfg_core/tx_memo.py`) |
|---|---|---|
| `v` | int | schema version, `1` |
| `flow` | str | `mint` · `swap` · `swap-fee` · `mint-fee` · `offer` · `burn` · `harvest` · `assemble` · `equip` · `extract` · `deposit` · `closet` · `trustline` · `admin-burn` · `buyback` · `amm-setup` · `migrate` |
| `surface` | str | `discord` · `telegram` · `webapp` · `miniapp` · `cli` · `ops` |
| `actor` | str | `user` (user-signed via Xaman) · `bot` (issuer/regkey-signed) |
| `campaign` | str? | optional, only during campaigns (e.g. `make-waves`) |

**Wire encoding** (`build_memo(flow, surface, actor, campaign=None) -> Memo`):
- `MemoType` = hex(`"lfg/prov"`) — a stable, URL-safe namespace key. All app
  memos are identified by this MemoType; version lives *inside* the JSON.
- `MemoFormat` = hex(`"application/json"`).
- `MemoData` = hex(compact `json.dumps(..., separators=(",", ":"))`).
- Values are module constants (`Flow.MINT`, `Surface.DISCORD`, …) — free
  strings rejected: `build_memo` validates against the enums and raises.
- **Size bound**: the helper asserts the encoded Memo ≤ 256 bytes hex-decoded
  (worst-case current schema is ~90 bytes; hard cap leaves 3/4 of the 1 KB
  per-tx memo budget free for future entries). Unit test locks this.

**PII stance:** memo contents are public forever. Wallet addresses are already
on-chain in `Account`/`Destination` — but Discord/Telegram user IDs are
off-chain PII and are **omitted entirely** (not hashed: a hash of a public
Discord ID is trivially reversible by enumeration, so hashing is privacy
theater; and we need no on-chain user key — reconciliation joins on
signing account + tx hash via `history_<net>.db`, which already maps wallets
to users off-chain). `actor` is a role, not an identity. No usernames, no
internal DB ids, no edition numbers (already in the NFT URI).

Inbound/echoed memos (per #57) are untrusted input: parsers must length-cap,
`json.loads` inside try/except, and validate enum membership — never render
raw memo text to users.

### 3.2 Stamping point + plumbing the context

Same choke points as SourceTag:

- **Bot-signed:** `submit_checked(tx, *, memo_ctx)` attaches
  `memos=[build_memo(**memo_ctx)]` via `Transaction` model copy before
  submitting (xrpl-py models are immutable — rebuild with
  `tx.to_dict() | {"memos": …}`). Each `xrpl_ops` builder gains an optional
  `memo_ctx` param; the flow layer (`mint_flow`/`swap_flow`/`economy_flow`)
  passes `flow` + `surface` (it already knows the surface via the service
  request), defaulting `actor="bot"`.
  - `surface` plumbing: `lfg_service` handlers already know their surface
    (per-surface service tokens `SERVICE_TOKEN_DISCORD`/`_TELEGRAM`); flows
    receive it as a plain string arg with default `"cli"` so economy scripts
    work unchanged.
- **User-signed:** `_create_xumm_payload` gains optional `memo_ctx` and, when
  provided, injects `txjson.setdefault("Memos", [build_memo_json(...)])`
  (dict form, since txjson is raw JSON). `create_payment_payload` /
  `create_accept_offer_payload` / trustline pass their flow. SignIn stays
  memo-free. The static detect link also gets the memo (constant per
  flow — safe to bake into the URL).

**Backward compatibility:** absence-tolerant by construction — memos are
additive metadata. `history_events.py` derivation, the listeners, and the
auditors key off tx type/taxon/accounts and ignore `Memos` today; nothing
breaks for pre-memo txs, and the parser (§3.3) returns `None` for missing/
foreign/malformed memos.

### 3.3 Future consumption (history derivation)

`lfg_core/history_store.py` already archives verbatim `{tx, meta}` JSON and
records `source_tag` (history_store.py:20,97). Add (follow-up, not this PR)
`parse_provenance(tx) -> dict | None` in `tx_memo.py`; `history_events.py`
can then attach `flow`/`surface` to derived `nft_events`/`brix_events`
during `rederive()` — enabling per-surface leaderboards (`users_swaps` by
surface, etc.) with zero re-scraping, because raw txs are the source of truth.

## 4. Design C — pre-submit simulation + reserve check (#58)

### 4.1 The choke point: `submit_checked()`

There is no single submit point today — `submit_and_wait` is called at
5 scattered sites in `xrpl_ops.py` (:73, :123, :331, :363, :415), plus
`surfaces/discord_bot/admin.py:52` and `scripts/testnet_amm_setup.py:99,130`,
each with its own copy-pasted `retries = 5` loop that **retries blindly on any
exception** — including deterministic failures (`tem*`, `tec*` raised by
`submit_and_wait` as `XRPLReliableSubmissionException`), burning 25 s of
sleeps and, for `tec*` results, real fees, per #58's complaint.

Consolidate into one function in `xrpl_ops.py` (all sites migrate to it;
admin.py and testnet_amm_setup import it too):

```python
async def submit_checked(
    tx: Transaction,
    client: JsonRpcClient,
    wallet: Wallet,
    *,
    memo_ctx: MemoCtx | None = None,
    retries: int = config.RETRY_MAX_ATTEMPTS,   # env-driven, replaces the hardcoded 5s
) -> Response:
```

Order of operations (all fail-closed — any check that cannot complete
*for a deterministic reason* aborts; **network errors during pre-flight
degrade to submit-anyway with a warning**, so a flaky clio can't brick
minting — the pre-flight is an optimization, the ledger stays the authority):

1. **Invariant gate** (§2.2): `source_tag` present and correct — raise
   `TxHygieneError`, no retry, no network.
2. **Memo stamp** (§3.2) if `memo_ctx` given.
3. **Reserve check** (bot-signed txs only): fetch `ServerState`
   (`reserve_base`/`reserve_inc`, in **integer drops** — never floats) +
   `AccountInfo` (`Balance`, `OwnerCount`, both int drops/counts). Compute
   `spendable = balance - (reserve_base + owner_count_after * reserve_inc)`
   where `owner_count_after` adds the tx's worst-case owner-object delta
   (+1 for NFTokenMint that may create a page, NFTokenCreateOffer, TrustSet,
   AMMCreate; 0/−1 for burns/accepts — use a conservative per-type table,
   default +1). Require `spendable >= fee + xrp_outflow`, where
   **`xrp_outflow` is defined precisely as**: `Amount` when `Amount` is an
   XRP drops string; else `SendMax` when `SendMax` is an XRP drops string
   (the cross-currency case — `buy_and_burn` delivers a BRIX IOU but *spends*
   XRP via `send_max`, xrpl_ops.py:329-330, which a delivered-amount-only
   check would undercount to 0); else 0. All arithmetic in `int` drops
   (IOU values untouched — this is an XRP-reserve check only).
   Failure → `ReserveError` (deterministic, no retry, no submit).
4. **Simulation**: xrpl-py **5.0.0** (installed, `.venv`) ships
   `xrpl.asyncio.transaction.simulate` (verified importable; wraps the
   `simulate` API method, rippled 2.4+). **ASSUMPTION, not verified:** that
   our JSON-RPC endpoints (testnet + mainnet, `config.JSON_RPC_URL`) actually
   serve the `simulate` method — this is asserted from rippled version
   expectations, not tested. **PR-2's verification MUST include one live
   `simulate` call against BOTH networks' endpoints; if either does not
   support it, flip the `PRESUBMIT_SIMULATE` default to `0`** (degrade-open
   already handles per-call failure, but the default must reflect reality).
   `simulate(tx_without_signature, client)`; inspect `engine_result`:
   - `tes*` → proceed.
   - Any `tem*` / `tef*` / `tec*` → `SimulationError(engine_result)`,
     **no submit, no retry** — no carve-outs. In particular `tecPATH_DRY`
     (missing trustline / no liquidity) is deterministic at this instant: a
     2 s backoff fixes nothing, and in simulation nothing was burned, so
     submitting anyway would only convert a free warning into a burned fee.
     This short-circuit is exactly the retry-budget burn #58 targets.
   - request error / method unavailable → log warning, proceed to submit
     (degrade open per the pre-flight principle above).
5. **Submit with classified retry**: call `submit_and_wait`; classify every
   failure per this exhaustive table (retry = `config.RETRY_BASE_DELAY *
   2**attempt` backoff, up to `config.RETRY_MAX_ATTEMPTS`):

   | Failure class | Examples | Action |
   |---|---|---|
   | Transport | connection error, timeout, HTTP 5xx | **RETRY** |
   | `ter*` (retryable by protocol definition) | `terQUEUED`, `terRETRY`, `terPRE_SEQ` | **RETRY** |
   | Local fee pressure | `telINSUF_FEE_P` | **RETRY** |
   | Stale autofill | `tefPAST_SEQ`, `tefMAX_LEDGER` | **RETRY — after re-autofill** (fresh sequence / LastLedgerSequence) |
   | `tem*` (malformed) | `temMALFORMED`, `temBAD_FEE` | **NO RETRY** — deterministic, raise immediately |
   | All other `tef*` | `tefNO_PERMISSION`, `tefBAD_AUTH` | **NO RETRY** |
   | **ALL `tec*`** (no exceptions) | `tecPATH_DRY`, `tecINSUFFICIENT_RESERVE`, `tecUNFUNDED_PAYMENT` | **NO RETRY** — the fee is already burned; retrying re-burns it. Flow-level compensation (swap/economy journaling) decides what happens next, not the submit layer. |

Return the `Response`; callers keep their existing "hash + Tx re-check"
handling initially (mechanical migration, behavior-preserving for the success
path). The 5 duplicated retry loops in `xrpl_ops.py` collapse into this one.

`buy_and_burn` note: its self-issuer no-op guard (xrpl_ops.py:310-321) stays
*outside* `submit_checked` (flow decision, not submit hygiene).

### 4.2 Interaction with existing behavior

- Exceptions from `submit_checked` are caught by each op's existing
  `try/except → return None` envelope, so flow-level fail-safe ordering
  (swap_flow/economy_flow journaling) is untouched — ops still surface
  failure as `None`, just *faster* and *cheaper* on deterministic errors.
- Config: reuse existing `RETRY_MAX_ATTEMPTS` / `RETRY_BASE_DELAY` env vars
  (currently only honored by some paths; hardcoded `retries = 5` dies).
- New env `PRESUBMIT_SIMULATE=1` (default on) as a kill switch if a network's
  endpoint misbehaves; reserve check has no switch (pure math, two reads).

## 5. Module layout

- `lfg_core/tx_memo.py` — new: enums, `build_memo` (xrpl-py `Memo` model),
  `build_memo_json` (txjson dict form), `parse_provenance`, size assertion.
- `lfg_core/xrpl_ops.py` — `submit_checked`, `TxHygieneError`/`ReserveError`/
  `SimulationError`, migrate 5 call sites, tag fixes stay in builders.
- `lfg_core/xumm_ops.py` — memo_ctx in `_create_xumm_payload`; SourceTag +
  memo in `generate_static_payment_link`.
- `surfaces/discord_bot/admin.py`, `scripts/testnet_amm_setup.py` — migrate to
  `submit_checked`.
- Tests: `tests/test_tx_hygiene.py`, `tests/test_tx_memo.py`,
  `tests/test_submit_checked.py` (+ extend the two existing source-tag test
  files). All new lfg_core-importing test files carry the env-guard preamble
  verbatim from `tests/test_seasons.py:1-18`.

## 6. Rollout

Normal draft-PR → CodeRabbit flow. Suggested split (each independently
green): PR-1 = §2 SourceTag fixes + invariant gate + AST sweep test (ship
first — the detect-link gap is losing mainnet hackathon credit **today**);
PR-2 = §4 `submit_checked` consolidation; PR-3 = §3 memos. Testnet
verification per issue acceptance: mint + swap + trustline + deep-link
payment, then confirm `SourceTag`/`Memos` on the validated txs via clio.

## 7. Issue disposition

| Issue | Disposition |
|---|---|
| #75 | **Close now, no code change** — the inline `main.py` mint no longer exists (main.py is an 8-line shim since the spine refactor); its two acceptance boxes are satisfied by `xrpl_ops.mint_nft:58` + `tests/test_xrpl_source_tag.py`. Comment should note the *relocated* equivalent gap (detect link) is tracked under #61/this spec. |
| #61 | **Closed by PR-1** — after the two fixes (§2.1) every enumerated path carries the tag, with choke-point + AST enforcement. On-chain testnet verification per its acceptance list. |
| #57 | **Fully absorbed — close as duplicate/superseded** once #61 (source_tag half) and #54 (memo half) land; its "treat inbound memos as untrusted" note is folded into §3.1. No independent work remains. |
| #54 | **Closed by PR-3** — schema differs deliberately from the issue sketch (single JSON memo vs four memos; `initiator/platform/action` → `actor/surface/flow`; Discord IDs omitted, not encoded). |
| #58 | **Closed by PR-2** — simulate via xrpl-py 5.0.0 `simulate` + integer-drops reserve math + deterministic-failure short-circuit at the new single choke point. |
