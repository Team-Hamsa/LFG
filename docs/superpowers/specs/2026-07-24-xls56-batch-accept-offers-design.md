# XLS-56 Batch: single-signature accept of multiple NFT offers — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #219

## Problem

Bulk minting (#215) delivers N gift offers, one `NFTokenCreateOffer`
destination-locked to the recipient per unit. Today the recipient must sign a
separate `NFTokenAcceptOffer` for every offer:

- The pending-offers tray (#218, `lfg_service/app.py::handle_pending_offer_accept`,
  client `webapp/client/app.js::offerAccept`) builds ONE XUMM accept payload
  per row, on click (`xumm_ops.create_accept_offer_payload`). Ten bulk-minted
  editions = ten QR scans / ten Xaman confirmations.
- Each accept is its own XUMM payload, so a claim of a full bulk batch also
  pushes against the open-payload cap (#260) and the per-minute XUMM quota
  (#254 429 incident).

The XRPL **Batch** amendment (XLS-56) lets a single account submit up to **8**
inner transactions under **one signature** (`xrpl.models.transactions.Batch`,
present in the installed `xrpl-py` 4.5.0). Wrapping the accepts in one Batch
collapses N confirmations into one — a large UX win for bulk-mint acceptance
and the pending-offers tray.

This is a **feasibility-gated** feature: the design is buildable today against
xrpl-py, but two external dependencies are unverified (Xaman payload-API Batch
signing support; XLS-56 mainnet amendment activation). The spec therefore
ships the flow behind an env gate defaulted OFF, with an explicit blocked-on
section and phased rollout.

## Constraints discovered

- **SourceTag + provenance memos (Make Waves hackathon, #54):** every non-`SignIn`
  txjson must carry `SourceTag = 2606160021` and a `Memos` block.
  `xumm_ops._create_xumm_payload` `setdefault`s both on the txjson it is handed
  — for a Batch, that stamps the **outer** Batch transaction, which is correct
  (the outer tx is the one signed and the one that lands with the app's tag).
  Action reuses the existing `memos.ACTION_ACCEPT_OFFER` (`"accept-offer"`),
  initiator `memos.INITIATOR_USER`, platform via
  `memos.platform_for_surface(...)`.
- **XLS-56 inner-txn cap = 8**, bulk cap (`BULK_MINT_MAX`) defaults to 10 — a
  claim of a maxed bulk batch, or a large pending-offers selection, MUST be
  chunked into groups of ≤8 (one Batch payload / one signature per chunk).
- **`Batch._get_errors` requires ≥2 inner transactions.** A one-offer selection
  must fall back to the existing single-offer accept path, never build a Batch.
- **Inner txns carry the `tfInnerBatchTxn` flag.** `xrpl.models.transactions.Batch`
  sets this automatically in `__post_init__`; when hand-building txjson we must
  set `Flags: 0x40000000` (`TransactionFlag.TF_INNER_BATCH_TXN`) on each inner
  `NFTokenAcceptOffer` or Xaman/the ledger rejects it.
- **Signer pinning (mainnet 2026-07-21 wrong-wallet incident):** the outer
  Batch `Account` and each inner `Account` MUST be pinned to the caller's
  wallet. Unlike a priced marketplace accept, the pending-offers tray only
  surfaces **free** gifts (`filter_claimable_offers` rejects any `amount != "0"`),
  so a wrong-wallet signature only wastes a fee — but pinning is free insurance
  and matches `create_accept_offer_payload(account=...)`.
- **Fail-closed re-verification:** like `handle_pending_offer_accept`, every
  offer_index in the batch must be re-checked on-ledger
  (`xrpl_ops.get_account_nft_offers` → `xrpl_ops.filter_claimable_offers`)
  immediately before the payload is built; any offer no longer live/claimable
  is dropped from the Batch (not the whole request failed) so a single
  just-claimed offer can't strand the rest.
- **No-custody model:** the Batch is user-signed in Xaman; the backend only
  builds the payload and re-verifies. Abandoning a Batch leaves every offer
  live and claimable (identical to abandoning a single accept today).
- **Network seam:** pending offers resolve on `config.XRPL_NETWORK` (character
  network) via `nft_index.index_db_path(config.XRPL_NETWORK)` — batch accept
  inherits the same resolution; no `ECONOMY_NETWORK` involvement (gift/mint
  offers are character-side).

## Design

Three independent seams, all behind a single feasibility gate.

### 0. Feasibility gate (`lfg_core/config.py`)

```python
BATCH_ACCEPT_ENABLED_DEFAULT = "0"          # named so a test locks the shipped default
BATCH_ACCEPT_ENABLED = env_flag("BATCH_ACCEPT_ENABLED", BATCH_ACCEPT_ENABLED_DEFAULT)
BATCH_ACCEPT_MAX_INNER = int(os.getenv("BATCH_ACCEPT_MAX_INNER", "8"))  # XLS-56 hard cap
```

With the gate OFF (the shipped default), the new endpoint returns
`409 batch_disabled` and the client never shows the batch button — behavior is
byte-for-byte today's per-offer tray. This lets the code land, be reviewed, and
be unit-tested while the external dependencies (below) remain unconfirmed; the
gate flips per-stack once Xaman + amendment are verified.

### 1. Payload builder (`lfg_core/xumm_ops.py`)

New `create_batch_accept_payload(account, offer_ids, ...)`:

```python
async def create_batch_accept_payload(
    account: str,
    offer_ids: list[str],
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """XUMM payload for an XLS-56 Batch of NFTokenAcceptOffers — one signature
    accepts every offer in `offer_ids` (2..8). Each inner tx is pinned to
    `account` and flagged tfInnerBatchTxn; the outer Batch carries SourceTag +
    memos via _create_xumm_payload. Caller guarantees 2 <= len(offer_ids) <= 8
    and that all offers were re-verified live."""
    TF_INNER = 0x40000000
    TF_INDEPENDENT = 0x00080000  # each inner applies on its own merit
    inner = [
        {
            "RawTransaction": {
                "TransactionType": "NFTokenAcceptOffer",
                "Account": account,
                "NFTokenSellOffer": oid,
                "Flags": TF_INNER,
            }
        }
        for oid in offer_ids
    ]
    txjson = {
        "TransactionType": "Batch",
        "Account": account,
        "Flags": TF_INDEPENDENT,
        "RawTransactions": inner,
    }
    return await _create_xumm_payload(
        txjson,
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(
            memos.INITIATOR_USER, platform, memos.ACTION_ACCEPT_OFFER, campaign
        ),
    )
```

`TF_INDEPENDENT` (0x00080000) is chosen over `TF_ALL_OR_NOTHING` so that a
single inner accept failing (e.g. an offer claimed a block earlier via another
device) does not fail the whole claim. `_create_xumm_payload` stamps
`SourceTag`/`Memos` on the outer Batch exactly as it does for every other
payload — no change to that function.

> **Note on inner-tx wire shape:** the exact fields Xaman requires inside
> `RawTransactions` (Fee=0, Sequence handling, whether the outer signer's
> account must be repeated) are part of the **blocked-on** verification. The
> builder above encodes the XLS-56 spec shape; the integration task confirms
> it against a real Xaman testnet sign before the gate is flipped.

### 2. Service endpoint (`lfg_service/app.py`)

New `@require_wallet handle_pending_offers_accept_batch` on
`POST /api/offers/accept-batch`:

1. If `not config.BATCH_ACCEPT_ENABLED` → `409 {"code": "batch_disabled"}`.
2. Dev mode → `501` (mirrors `handle_pending_offer_accept`).
3. Read body `offer_indices: list[str]`; validate non-empty list of strings.
4. Re-verify on-ledger: `get_account_nft_offers(bot_wallet_address())` →
   `filter_claimable_offers(offers, wallet, time.time())`; intersect with the
   requested indices, preserving request order. On lookup failure → `503
   pending_unavailable` (same as the single path).
5. If fewer than 2 survive verification → return `{"single": true, "offer_index": <the one>}`
   (client falls back to the existing single-offer accept) or `410 offer_gone`
   if zero survive.
6. Chunk the survivors into groups of `config.BATCH_ACCEPT_MAX_INNER` (≤8).
   Build one `create_batch_accept_payload` per chunk (pinning `account=wallet`,
   `user_token=await _push_token(request["user"])`, `platform=...`). Return
   `{"batches": [{"qr", "link", "push", "count"}...]}` — the client renders one
   QR per chunk (a 10-offer claim = 2 signatures, still far better than 10).

Registered next to the existing routes:
`app.router.add_post("/api/offers/accept-batch", handle_pending_offers_accept_batch)`.

The existing single-offer `POST /api/offers/accept` is untouched — it remains
the fallback for 1-offer selections and the gate-off path.

### 3. Client (`webapp/client/app.js`, `webapp/client/index.html`)

A server-advertised capability drives the UI so the client never assumes Batch
works. `handle_pending_offers` already returns `{offers: [...]}`; extend it to
include `"batch": config.BATCH_ACCEPT_ENABLED` (a bool). In `openOffers()`:

- When `batch` is true and `offers.length >= 2`, render a checkbox per
  `offerRow` and a sticky **"Accept selected (1 signature)"** button.
- The button POSTs `/api/offers/accept-batch` with the checked `offer_index`
  list, then renders one QR block per returned chunk (reusing the `.u-accept`
  markup already in `offerAccept`), with copy like *"Scan once to claim these
  N."* A `{single:true}` response routes back through `offerAccept`.
- When `batch` is false, the panel is exactly today's per-row Accept list.

Any `app.js`/`index.html` change bumps the cache-buster (`?v=` on the module
import in `index.html`) in the same commit, per repo convention.

## Out of scope

- **Marketplace multi-buy** (batch-accepting priced sell offers / bids). Priced
  accepts move money, need per-offer amount re-verification and per-kind
  denomination handling (#239), and a partial-fill Batch has settlement
  implications the gift path doesn't. A follow-up once the gift path is proven.
- **Burn-to-mint Batch** (the "infinite minting" burn-M + accept-M path teased
  in #215/#220) — depends on the `BurnEntitlement` stub
  (`lfg_core/entitlement.py`), out of scope here.
- **Batch of `NFTokenCreateOffer` (bulk minting the offers themselves)** —
  #219 is about the *accept* side only.
- Discord-bot / Telegram native batch UI — the pending-offers tray is
  Activity/web only today; those surfaces inherit nothing new.

## Open questions / decisions for maintainer

1. **Xaman/XUMM Batch signing support is the gating dependency and is
   UNVERIFIED.** Does the XUMM payload API accept a `txjson` with
   `TransactionType: "Batch"` and a `RawTransactions` array, and does the Xaman
   app render + sign it? As of the codebase's xrpl-py (4.5.0, which *models*
   Batch) this must be confirmed by an actual testnet sign before the gate is
   flipped. If Xaman cannot sign Batch payloads yet, the feature ships gate-off
   and waits.
2. **XLS-56 mainnet amendment activation status is UNVERIFIED here** (no live
   network access in triage). Batch has been available on Devnet; confirm
   whether the amendment is enabled on **mainnet** (`s1.ripple.com`) and
   **testnet** (`s.altnet.rippletest.net`) before enabling per-stack. Until
   enabled on a network, a Batch tx returns `temDISABLED`.
3. **Inner-transaction wire shape:** exact required fields (Fee, Sequence,
   whether inner `Account` is mandatory or inherited) for a Batch that Xaman
   will sign — verify against XLS-56 final + a real sign; adjust
   `create_batch_accept_payload` accordingly.
4. **Batch flag choice:** `TF_INDEPENDENT` (proposed — best-effort, partial OK)
   vs `TF_ALL_OR_NOTHING`. Independent matches the "claim what's still there"
   spirit of the tray; confirm.
5. **Rollout order:** enable on testnet/staging first (`BATCH_ACCEPT_ENABLED=1`
   on the staging stack) once Xaman testnet signing is confirmed; flip prod
   only after mainnet amendment + Xaman mainnet signing are both verified.

## Testing

- **Unit — `xumm_ops.create_batch_accept_payload`** (fake `_post_xumm_payload`):
  asserts the built `txjson` is `TransactionType: "Batch"`, `Account` pinned,
  `RawTransactions` length == len(offer_ids), each inner is a
  `NFTokenAcceptOffer` with the caller's `Account` and `Flags & 0x40000000`,
  and (after `_create_xumm_payload`) the outer carries `SourceTag ==
  config.SOURCE_TAG` and a `Memos` block. Also assert a `<2` or `>8` input is
  rejected by the caller contract.
- **Unit — chunking helper:** 1→single fallback, 2→one batch, 8→one batch,
  10→two batches (8+2), 16→two batches of 8.
- **Unit — endpoint gate:** `BATCH_ACCEPT_ENABLED=0` → `409 batch_disabled`
  with no XUMM call; `=1` builds payloads. Re-verification drops a
  no-longer-claimable offer_index (reuse `tests/test_pending_offers.py`
  fixtures / `filter_claimable_offers`).
- **Integration — `webapp/test_smoke.py`:** `/api/offers/pending` includes
  `batch` bool; `/api/offers/accept-batch` in dev mode → 501; gate-off → 409.
- **Manual smoke (post-Xaman-verification, testnet):** bulk-mint 3 editions to
  a test wallet, open the tray, select all 3, tap "Accept selected", scan the
  single Batch QR in Xaman, confirm all 3 land in the wallet and the tray
  empties; confirm the on-ledger Batch tx carries `SourceTag 2606160021` and
  the accept-offer memo.

## Blocked-on (summary)

| Dependency | State | Unblocks |
| --- | --- | --- |
| Xaman payload-API Batch signing | UNVERIFIED — confirm with a real sign | flipping `BATCH_ACCEPT_ENABLED=1` anywhere |
| XLS-56 amendment on testnet | UNVERIFIED | staging enable |
| XLS-56 amendment on mainnet | UNVERIFIED | prod enable |

The code (builder + endpoint + tests + gated UI) can land now with the gate
OFF; only the enable step is blocked.
