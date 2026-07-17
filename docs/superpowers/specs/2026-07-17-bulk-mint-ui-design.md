# Bulk Mint UI (Activity) — Design

**Date:** 2026-07-17
**Issue:** #215 (backend merged; this spec covers the client UI + flag)
**Status:** Approved design, pre-implementation

## Goal

Expose the merged bulk-mint backend (#215: `POST /api/mint/bulk`,
`GET /api/mint/bulk/active`, `GET /api/mint/bulk/{id}`,
`POST /api/mint/bulk/{id}/cancel`) in the Activity client, behind a server-side
feature flag so it can be exercised on staging before prod release.

## Feature flag

- New env var **`BULK_MINT_UI_ENABLED`** (parsed in `lfg_core/config.py`,
  default **off** — same truthy parsing as `ECONOMY_ENABLED`).
- `handle_config` (`lfg_service/app.py`) adds two fields to `GET /api/config`:
  - `"bulk_mint_ui": config.BULK_MINT_UI_ENABLED`
  - `"bulk_mint_max": config.BULK_MINT_MAX`
- Flag **off** → the client renders exactly today's UI: no stepper, no bulk
  calls, zero behavior change. The bulk API endpoints themselves stay live
  regardless (they already exist and are quantity-capped server-side).
- Rollout: set `BULK_MINT_UI_ENABLED=1` in the staging env
  (`docs/ops/env.staging.example` gains the line); prod stays unset until
  promoted.

## Entry point — quantity stepper on mint home

- A compact `[−] 1 [+]` stepper next to the existing **Mint** button on the
  mint home screen. Rendered only when `bulk_mint_ui` is true.
- Range clamped client-side to `1..bulk_mint_max`; the server clamps again
  (`clamp_to_headroom`), so client clamping is UX only.
- Quantity is settled **before any payload is created** — the stepper lives on
  the home screen; nothing is sent until Mint is clicked.
- Click behavior:
  - **Qty 1** → the existing single-mint path (`startMint()` →
    `POST /api/mint`) byte-for-byte unchanged.
  - **Qty ≥ 2** → `POST /api/mint/bulk` with body `{"quantity": N}` (plus the
    same Discord context the single path sends). The response carries the K×
    payment QR / deep link / push state; render it with the same payment
    screen treatment as single mint.
- Cancel while `awaiting_payment` → `POST /api/mint/bulk/{id}/cancel`
  (mirrors single-mint cancel, #141). Once paid, no cancel — fulfillment runs
  to completion (server-enforced).
- 409 `collection_full` on start → user-facing "collection is full" error on
  the home screen.

## Fulfillment screen — live progress + accept list

After the payment signs, the client polls `GET /api/mint/bulk/{id}` (~3 s,
same cadence as the single-mint poll) and renders one screen:

- **Progress line** driven by the job's `minted` / `offered` counts and
  `quantity`: "Minting 3 / 5…", flipping to
  "All minted — accept your NFTs below." when the job reaches `done`.
- **Unit list** that fills in as units land, one row per unit:
  - `pending` → placeholder row (spinner / dimmed).
  - `minted` (offer not yet created) → image + "creating offer…".
  - `offered` → NFT image (`image_url`), edition number (`nft_number`), and an
    **Accept** button.
  - `failed` → the unit's `error`; if the failure was a cap-race /
    exhausted-retries loss, surface the mint-credit message (the server
    already converts these into durable `mint_credits` rows — the copy says
    the payment is preserved as a credit).
- Job terminal states other than `done` (`failed`, `payment_timeout`) render
  the same error treatment single mint uses.
- Accepting is **not** required to finish the screen — offers carry no
  on-ledger expiration (#215 design), so "come back later" is safe. A Done
  button returns to mint home at any point after fulfillment completes.

## Per-unit accept — new endpoint, lazy payloads

**`POST /api/mint/bulk/{session_id}/units/{index}/accept`**

- Auth: session owner + platform match, same guards as
  `handle_bulk_mint_status`.
- Preconditions: unit exists and is `offered` (else 409); job not
  `awaiting_payment`.
- Builds an accept-offer XUMM payload via the existing
  `xumm_ops.create_accept_offer_payload` for the unit's `offer_id`, threading
  the job's `push_user_token` and `return_url`. SourceTag, provenance memos,
  15-minute payload expiry, and push-with-QR-fallback are all inherited from
  `_create_xumm_payload` — no new payload plumbing.
- **Payloads are created on click only, never eagerly.** A 10-unit job must
  not open 10 XUMM payloads up front — the open-payload cap incident (#260)
  makes eager creation a denial-of-service on ourselves. Repeat clicks create
  a fresh payload (the previous one expires in 15 min; acceptable v1
  behavior).
- Response: `{ qr, link, push }` (same shape the other accept flows
  return); the client shows the existing accept modal (QR + deep link +
  honest push messaging per #212).
- The endpoint does **not** poll for acceptance; the on-chain listener already
  records ownership transfer. The client may optimistically mark a row
  "accept sent" after showing the payload; v1 does not verify acceptance.

## Resume

- On Activity launch, alongside the existing `resumeMint()` call, the client
  calls `GET /api/mint/bulk/active`:
  - `awaiting_payment` → re-open the bulk payment screen (existing
    `payment_qr` / `payment_link` from the status body).
  - `paid` / `fulfilling` → open the fulfillment screen and start polling.
  - `null` → nothing (fall through to normal home / single-mint resume).
- `done` jobs are terminal and not returned by `/active`; unaccepted offers
  after that are reachable via Xaman's Events tab. Acceptable for v1 — a
  claim-later UX is #218's scope.
- Order: check bulk-active **before** single-mint resume (a user can't have
  both; bulk is the rarer, more expensive flow to strand). If the bulk check
  errors, fall through to the existing resume path.

## Error handling summary

| Failure | Behavior |
|---|---|
| `POST /api/mint/bulk` 409 `collection_full` | inline home-screen error |
| Payment timeout | job → `payment_timeout`; error screen, back to home |
| Unit mint failure (retries exhausted / cap race) | row shows error + mint-credit copy; job continues |
| Offer creation stuck (`minted` after final pass) | job stays `fulfilling`; row shows "creating offer…"; server resume sweep keeps retrying |
| Accept-payload create fails | modal shows retryable error; nothing on-chain happened |
| `/api/mint/bulk/active` errors on launch | fall through to existing resume, no hard failure |

## Testing

- **pytest (`tests/`)**: config flag surfaces in `/api/config` (on/off);
  accept endpoint — auth mismatch 403/404, non-`offered` unit 409, happy path
  builds a payload with the unit's `offer_id` and the job's push token
  (xumm mocked); flag parsing default-off.
- **webapp smoke**: stepper hidden when `bulk_mint_ui` false; visible and
  clamped when true; qty 1 hits `/api/mint`, qty 2 hits `/api/mint/bulk`
  (existing no-build smoke harness with mocked `api()`).
- **Staging manual**: full 2–3 unit bulk mint on testnet via staging stack,
  including a mid-fulfillment Activity relaunch (resume path) and one accept.

## Out of scope

- Claim-later / offer-inventory UX for `done` jobs (#218).
- Burn-to-mint entitlements (#220).
- Acceptance verification / auto-refresh of accepted rows (listener already
  tracks ownership; UI polish later).
- Discord-bot or Telegram surfaces — Activity only (both reach the same
  service; other surfaces can follow the same endpoints later).
