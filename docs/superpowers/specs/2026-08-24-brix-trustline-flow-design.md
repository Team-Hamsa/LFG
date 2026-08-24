# BRIX trustline flow in the Activity — design

**Status:** implemented (#441) · 2026-08-24
**Follows:** PR #440 (tri-state trustline lookup on the claim path)
**Related:** #48 (BRIX daily drip), #239 (BRIX trait listings / on-ramp), #135 (push delivery)

## Problem

Two Activity paths refuse with 409 `trustline_required` when the wallet has no
BRIX trustline: `POST /api/brix/claim` and `POST /api/market/buy` on a trait
listing. In both cases the client dead-ends:

- `brix_pure.claimErrorView('trustline_required')` toasts *"add it in Xaman,
  then claim"* and pins the Claim button (`lockLabel: 'BRIX trustline
  needed'`) for the rest of the page load. It already emits `trustline: true`,
  but nothing in `app.js` consumes that flag — the hook for a TrustSet flow
  was designed in and never wired.
- The comment at `lfg_service/app.py` (`_market_buy_start`, "the Activity
  drives the TrustSet flow first, same signal the mint flow uses") is
  aspirational: the Activity has **no** TrustSet flow at all.

The only TrustSet builder in the repo is `surfaces/discord_bot/trustline.py`
— bot-local, hand-rolled XUMM transport, and hardcoded to the **LFGO** pair
(`TOKEN_CURRENCY_HEX` / `TOKEN_ISSUER_ADDRESS`), not BRIX. Nothing reusable.

The user must know, unprompted, how to add a trustline in Xaman by hand
(issuer address + 40-hex currency code). For a first-time claimant that is a
lost payout in practice.

## Goal

A wallet with no BRIX trustline can set one **from inside the Activity**, in
one Xaman signature, and land back on the action it was trying to do (Claim
or trait Buy) with the button re-armed — without a page reload.

## Non-goals

- LFGO trustline (mint pricing) — the mint flow already falls back to XRP for
  trustline-less wallets and never blocks. Out of scope.
- Discord-bot / Telegram-command surfaces. The Activity is the only surface
  that renders the Claim button; the Telegram Mini App and web surface run the
  same client and get this for free.
- Changing the 409 contract. `trustline_required` stays; this adds the exit.

## Design

### Server: `POST /api/brix/trustline`

Authed (`@require_wallet`). Builds a XUMM `TrustSet` payload for the BRIX pair
and returns the standard sign-delivery shape the client already renders.

```
POST /api/brix/trustline            →  { uuid, qr_png, deep_link, pushed, push, expires_at }
GET  /api/brix/trustline/{uuid}     →  { state: "pending" | "signed" | "rejected" | "expired",
                                          tx_hash?, signer? }
```

Payload builder: new `xumm_ops.create_trustset_payload(currency, issuer,
limit, *, account, user_token, platform, expire_minutes)` going through
`_create_xumm_payload`, so it inherits — with no per-path code —

- `SourceTag = 2606160021` (hackathon invariant),
- provenance memos `initiator=user, platform=<surface>, action=trustset`
  (`memos.ACTION_TRUSTSET` already exists in the closed enum),
- the 15-minute `expire` cap (#260),
- push delivery via `_push_token(user)` (#135/#212) with QR fallback,
- **`Account` pinned to the session wallet** (#314 signer-pinning). A shared
  QR signed by a different wallet must not set a trustline on the wrong
  account.

txjson:

```json
{
  "TransactionType": "TrustSet",
  "Account": "<session wallet>",
  "Flags": 131072,
  "LimitAmount": { "currency": BRIX_CURRENCY_HEX, "issuer": BRIX_ISSUER, "value": BRIX_TRUSTLINE_LIMIT }
}
```

`Flags = tfSetNoRipple` (131072), matching the bot's LFGO TrustSet.
`BRIX_TRUSTLINE_LIMIT` is a new optional env knob; default should comfortably
exceed any plausible drip accrual plus market activity — propose
`1000000000` (1e9; BRIX supply is ~35M on mainnet, and a limit below a
holder's incoming payout makes the claim Payment fail `tecPATH_DRY`). Do NOT
reuse `TOKEN_TRUSTLINE_LIMIT` (default 1000 — sized for LFGO mint pricing,
and far below one large claim).

Status poll mirrors `handle_signin_status`: `get_payload_status(uuid)`; on
`signed`, verify `signer == session wallet` (a mismatch reports `rejected`
with `code: signer_mismatch`, like `advance_buy_session`), and re-capture
`issued_user_token` (#212 self-heal) via the existing
`_persist_issued_user_token`. **No DB state** — the ledger is the truth for
whether a line exists; the claim endpoint re-checks it on the next POST
anyway. Nothing to persist, nothing to reconcile.

Pre-flight: if `get_trustline_state` is already `PRESENT`, return 200
`{state: "already_set"}` and build nothing (idempotent; also what a client
that raced a manual Xaman add sees).

### Client

`brix_pure.claimErrorView('trustline_required').trustline === true` finally
gets a consumer. When `ev.trustline`:

1. Instead of pinning the Claim button with `lockLabel`, swap its label to
   **"Set BRIX trustline"** and rebind its click to `startBrixTrustline()`.
   (Keep `lockLabel` for `claims_disabled`, which has no user exit.)
2. `startBrixTrustline()` → `POST /api/brix/trustline` → render the returned
   sign delivery through the existing `applySignDelivery` / sign-panel glue
   (`signdelivery_pure.js`) — same panel the mint/buy flows use, so push /
   QR / deep-link rendering and the "sent to your Xaman app" copy come free.
3. Poll `GET /api/brix/trustline/{uuid}` on the existing sign-poll cadence.
   On `signed`: toast *"BRIX trustline set — you can claim now."*, close the
   panel, clear `brixLock`, restore the button to **Claim**, and call
   `loadBrix()`. On `rejected` / `expired`: toast and restore the "Set BRIX
   trustline" affordance (retryable).
4. `resumeAll()` / webview relaunch (#216/#221): a `trustline` session is
   stateless server-side, so on relaunch the client simply re-tries the
   original action — if the line landed, the claim proceeds; if not, the
   409 re-arms the trustline button. No resume endpoint needed.

Trait Buy reuses the same `startBrixTrustline()` on its own 409
`trustline_required` (market_pure error view gains the same `trustline:
true` marker), then re-issues the buy. This closes the "Activity drives the
TrustSet flow first" comment into a true statement.

Pure-module split, as elsewhere: state transitions (`trustlineView(state)`,
button label/handler selection) go in `brix_pure.js` and are tested via the
existing `tests/test_brix_*_js.py` node harness; `app.js` is DOM glue only.
Bump `app.js?v=` and `brix_pure.js?v=` in lockstep (cache-buster rule).

### Failure modes considered

| Case | Behaviour |
|---|---|
| Lookup fails during pre-flight | Treat as not-present and build the payload — a redundant TrustSet on an existing line is a harmless no-op tx; *not* refusing here (unlike the claim path) because nothing is bound and the user explicitly asked to set it. |
| Wrong wallet signs the shared QR | `signer_mismatch` → panel says so; the session wallet's claim still 409s and re-arms. No cross-wallet effect. |
| XUMM API unreachable | 503 `signing_unavailable` (existing code path in `_create_xumm_payload` callers); button stays "Set BRIX trustline". |
| Trustline set manually in Xaman while panel open | Poll never sees `signed`; user closes the panel and hits Claim → server pre-check `PRESENT` → payout proceeds. Fine. |
| Limit lower than accrued payout | Prevented by the 1e9 default; if an operator lowers it, the claim Payment fails deterministically (`tecPATH_DRY`) and the existing claim recovery marks it `failed` with accruals unbound. Documented on the env knob. |

## Tests

- `tests/test_brix_endpoints.py`: trustline POST returns a payload with
  `SourceTag`, `action=trustset` memo, `Account == wallet`,
  `LimitAmount == BRIX pair`; `already_set` short-circuit; status poll
  `signer_mismatch`; user-token recapture on signed.
- `tests/test_provenance*.py`: the new builder is in the stamp-and-validate
  sweep (every payload site must carry a memo — #399).
- `tests/test_brix_*_js.py`: `trustline_required` view yields the
  "Set BRIX trustline" affordance and no `lockLabel`; signed → Claim restored.
- `tests/test_market_pure_js.py`: trait-buy 409 exposes `trustline: true`.

## Rollout

No flag needed — the endpoint is inert unless a 409 arms the button, and a
TrustSet is user-signed with no custody or backend spend. Ships with
`BRIX_TRUSTLINE_LIMIT` documented in `CLAUDE.md` env block and
`docs/ops/env.staging.example`. Staging on testnet first (BRIX issuer is the
SEED account there), then promote.
