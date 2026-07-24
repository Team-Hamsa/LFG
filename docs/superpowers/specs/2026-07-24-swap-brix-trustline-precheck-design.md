# Swap burn-remint BRIX-trustline precheck — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #166

## Problem

A mainnet trait swap on the legacy **burn-remint** path (`lfg_core/swap_flow.py`)
failed at the offer step with `tecNO_LINE` **after both originals were already
burned**, stranding the reminted tokens in the issuer wallet with "offer failed
— contact an administrator".

Root cause: the swapper held enough BRIX, so `detect_swap_payment`
(`swap_flow.py:93`, a thin wrapper over `brix_payment.detect_payment_path`)
selected the BRIX fee path, and the replacement offers were priced in BRIX via
`_offer_amount` → `xrpl_ops.swap_offer_amount()` (`xrpl_ops.py:305`), which
builds an `IssuedCurrencyAmount` on `SWAP_OFFER_CURRENCY_HEX` /
`SWAP_OFFER_ISSUER`.

Under XLS-20, an `NFTokenCreateOffer` whose `Amount` is an IOU, on an NFT that
carries a `TransferFee` (all our transferable tokens — `NFT_TRANSFER_FEE=7000`),
requires **the NFT's issuer to hold a trustline for that IOU** (the royalty is
paid out in it). On testnet this is invisible because
`_default_brix_issuer = _default_swap_issuer` (`config.py:66-67`) — the NFT
issuer IS the BRIX issuer. On mainnet they are distinct accounts
(`config.py:72-73`: NFT issuer `rLfgoMint…`, BRIX issuer `rLfgoBriX…`), and
`rLfgoMint` had no BRIX trustline → `tecNO_LINE` on every BRIX-priced offer.

The swap ordering (module docstring, `swap_flow.py:9-40`) is deliberately
fail-safe — reversible steps first, the burn last — but it has **no gate for
this failure mode**: the missing-trustline condition is knowable up front, yet
the current flow only discovers it at `_create_offer_and_accept`
(`swap_flow.py:516`), which runs *after* the irreversible burn (`swap_flow.py:821`).
The offer failure then correctly reverts nothing (the burn already happened),
leaving the "contact an administrator" state.

## Constraints discovered

- **Destructive steps must never precede this check.** The burn
  (`swap_flow.py:835`, `xrpl_ops.burn_nft`) is the documented point of no return
  (#211: it stamps the on-chain index via `_persist_remint_to_index`). The check
  must run before *any* on-chain or fee-collecting step.
- **Fail-safe ordering already exists** — `run_swap_session` runs
  `detect_swap_payment` at the top (`swap_flow.py:693`), before the modify-fee
  collection, mint, modify, and burn. That is the natural insertion point: the
  answer is a persistent config-level fact, knowable before composing anything.
- **The issuer trustline is a config/ops fact, not per-user.** The relevant
  issuer is the **NFT issuer** `config.SWAP_ISSUER_ADDRESS` (what `mint_nft` is
  called with at `swap_flow.py:775`), and the IOU is
  `config.SWAP_OFFER_CURRENCY_HEX` / `config.SWAP_OFFER_ISSUER` — the exact pair
  `swap_offer_amount()` prices the offer in. `get_trustline_balance(address,
  currency, issuer)` (`xrpl_ops.py:638`) returns `None` when there is no
  trustline (or on a transient lookup failure — callers treat both as "not a
  holder"); the precheck must not fail-closed on a transient `None`.
- **Only the burn-remint path is affected.** The mutable / `NFTokenModify` path
  collects its fee as a plain XUMM **Payment** upfront (`_collect_modify_fee`,
  `swap_flow.py:600`) — user → app wallet, no `NFTokenCreateOffer`, no royalty —
  so it never hits this `tecNO_LINE`. A session that is *entirely* modify items
  needs no precheck even when `pay_with == "BRIX"`. The precheck gates on
  `burn_items` being non-empty (issue point 2 — "if its fee is ever collected via
  a BRIX-priced offer" — is a documented future guard, not a current path).
- **XRP-denominated offers never hit this.** When `pay_with == "XRP"`,
  `_offer_amount` returns native drops (`xrp_to_drops`), which carry no IOU
  royalty — so the XRP fee path is inherently trustline-safe and is a valid
  graceful fallback.
- **SourceTag / memos unchanged.** This change adds no new transaction; the
  precheck is a read-only `account_lines` lookup. Every existing swap tx already
  carries `SourceTag=2606160021` and provenance memos via `mint_nft` /
  `create_nft_offer` / `burn_nft` / `modify_nft` — nothing here alters that.

## Design

Two independent seams.

### Seam 1 — pre-burn precheck in `run_swap_session` (the fix)

Insert an issuer-trustline gate in `lfg_core/swap_flow.py::run_swap_session`
**immediately after** `detect_swap_payment` sets `session.pay_with`
(`swap_flow.py:693`) and after `burn_items` is computed (`swap_flow.py:704`),
i.e. before `_collect_modify_fee`, mint, modify, and burn.

New helper (in `swap_flow.py`, or as `xrpl_ops.issuer_holds_trustline`):

```python
async def _issuer_holds_offer_trustline() -> bool:
    """True if the NFT issuer holds a trustline for the BRIX pair the
    replacement offer is priced in. A BRIX-priced NFTokenCreateOffer on a
    TransferFee token fails tecNO_LINE unless the issuer can receive the
    royalty IOU (XLS-20). None (no line OR transient blip) → False."""
    bal = await xrpl_ops.get_trustline_balance(
        config.SWAP_ISSUER_ADDRESS,
        config.SWAP_OFFER_CURRENCY_HEX,
        config.SWAP_OFFER_ISSUER,
    )
    return bal is not None
```

Gate logic (after `pay_with`/`burn_items` are known):

```python
if burn_items and session.pay_with == "BRIX" and not await _issuer_holds_offer_trustline():
    logging.error(
        "SWAP PRECONDITION: NFT issuer %s holds no %s trustline to %s — "
        "BRIX-priced replacement offers would fail tecNO_LINE. Falling back "
        "to XRP fee path for session %s.",
        config.SWAP_ISSUER_ADDRESS, config.SWAP_OFFER_CURRENCY_HEX,
        config.SWAP_OFFER_ISSUER, session.id,
    )
    # Graceful degradation: re-price the whole session on the trustline-safe
    # XRP path (native-drops offers carry no IOU royalty). Reuse the AMM quote
    # brix_payment already computes for non-holders.
    cost = await xrpl_ops.get_amm_xrp_cost(
        config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER,
        Decimal(swap_fee_total(2)),
    )
    if cost is None:
        session.state = FAILED
        session.error = (
            "Swaps are temporarily unavailable — please try again shortly."
        )
        return
    session.pay_with = "XRP"
    total = str((cost * Decimal(config.SWAP_XRP_FEE_BUFFER))
                .quantize(Decimal("0.000001"), rounding=ROUND_UP))
    session.fee_per_nft = (Decimal(total) / 2).quantize(
        Decimal("0.000001"), rounding=ROUND_UP)
```

Because the precheck sits before `_collect_modify_fee` and before the mint, a
mixed (modify + burn) session collects its fee in the *new* currency and every
downstream `_offer_amount(session)` / `_collect_modify_fee(session)` reads the
flipped `session.pay_with` consistently — no destructive step runs on the stale
BRIX assumption. If the AMM cannot quote (unlikely, and already a fail-mode of
`detect_swap_payment`), the session fails cleanly **pre-burn** with a
user-facing message and nothing on-chain has changed.

**Alternative (maintainer decision):** hard-abort instead of XRP fallback — set
`FAILED` with an ops-facing precondition error and never silently change the
fee currency the user expected. Simpler, but the user's swap fails until ops
sets the trustline. See open questions.

### Seam 2 — ops assertion (defence in depth)

Add a startup/audit assertion so the misconfiguration is caught before a user
hits it. Preferred home: extend `scripts/audit_history.py` or add a tiny
`scripts/audit_swap_preconditions.py` that, when the BRIX fee path is live on
mainnet, verifies
`get_trustline_balance(SWAP_ISSUER_ADDRESS, SWAP_OFFER_CURRENCY_HEX,
SWAP_OFFER_ISSUER) is not None` and exits non-zero (CI/pre-deploy-gate style,
mirroring `audit_trait_files.py`'s exit-code contract) with a clear remediation
line ("set a BRIX trustline on the NFT issuer via Xaman"). This is the durable
root-cause fix; Seam 1 keeps a single user's swap working in the meantime.

### Data model / tx shape

No schema change, no new transaction. The precheck is one read-only
`account_lines` request (`get_trustline_balance`). When the XRP fallback fires,
the replacement offer is denominated in native drops (existing `_offer_amount`
XRP branch) — the same offer shape already shipped for non-BRIX-holders, which
already carries `SourceTag` + memos via `create_nft_offer`.

## Out of scope

- The **secondary DB cleanup** in the issue (testnet-era rows 3550–3554 in the
  network-agnostic `lfg_nfts.db` `LFG` table surfacing 404 images in mainnet
  views). This is a one-off ops/data task, unrelated to the burn ordering fix;
  it should be a separate issue/script (network-tag or purge). Not part of this
  code change.
- Retroactively re-crediting the already-recovered #144/#165 tokens (done
  manually per the issue).
- Any change to the mutable / `NFTokenModify` fee path (it is Payment-based and
  unaffected).

## Open questions / decisions for maintainer

1. **XRP fallback vs. hard abort.** Fallback keeps the user's swap succeeding
   (they pay XRP; the silent buy-and-burn converts it to BRIX) but silently
   changes the fee currency. Hard abort is truthful ("swaps paused — ops must
   set the issuer trustline") but blocks the user. Recommendation: **XRP
   fallback + LOUD error log + Seam 2 audit**, since the trustline should always
   be present and the fallback is a safety net, not a normal path.
2. **Where does the startup assertion live** — a new
   `scripts/audit_swap_preconditions.py`, an extension of `audit_history.py`, or
   an at-boot check in `lfg_service.app.on_startup`? A boot check risks failing
   startup on a transient `account_lines` blip (`None` is ambiguous); a
   CLI/CI-gate script is safer.
3. **Should the DB-cleanup secondary item be split into its own issue?**
   (Recommend yes.)

## Testing

- **Unit (`tests/test_swap_trustline_precheck.py`, new):**
  - Issuer HAS trustline (`get_trustline_balance` → `Decimal`), `pay_with`
    starts BRIX → `_issuer_holds_offer_trustline()` True, `run_swap_session`
    keeps `pay_with == "BRIX"`, mint/modify/burn/offer are reached.
  - Issuer LACKS trustline (`get_trustline_balance` → `None`), burn item
    present, `pay_with` BRIX → session flips to `pay_with == "XRP"`, `_offer_amount`
    returns native drops, **no burn happens on the BRIX assumption**, error log
    emitted.
  - Issuer lacks trustline AND `get_amm_xrp_cost` → `None` → session `FAILED`
    **before any mint/burn** (assert `xrpl_ops.mint_nft` / `burn_nft` mocks
    never called).
  - Modify-only session (`burn_items` empty) with missing trustline → precheck
    is skipped, no fallback, `pay_with` stays BRIX (Payment path unaffected).
  - Env-guard preamble at module top (copy the block from
    `tests/test_swap_offer_recovery.py`).
- **Integration:** reuse the existing `run_swap_session` harness in
  `tests/test_swap_offer_recovery.py` — patch `xrpl_ops.get_trustline_balance`
  and assert the ordering (no destructive call precedes the precheck).
- **Manual smoke (testnet is unaffected — issuer == BRIX issuer):** on a staging
  box configured with a *separate* BRIX issuer and the NFT issuer trustline
  removed, run a burn-remint swap and confirm it falls back to XRP and delivers,
  rather than burning-then-`tecNO_LINE`. Then run the Seam 2 audit script and
  confirm it exits non-zero.
