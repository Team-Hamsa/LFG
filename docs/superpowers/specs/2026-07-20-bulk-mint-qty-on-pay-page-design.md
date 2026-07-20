# Bulk-mint quantity on the Mint pay page — design

**Date:** 2026-07-20
**Issue:** #215 follow-up (bulk-mint UX revision; supersedes the PR #272 home-screen stepper)
**Surface:** Activity web client (`webapp/client/`) only — no server changes.

## Problem

PR #272 shipped the bulk-mint UI by adding a quantity stepper (`[−] N [+]`)
onto the **home screen**, sitting beside the primary "⛏️ Mint NFT" button, and
branching that button to the bulk flow when `N > 1`. This changed the primary
home UI, which is undesirable. The home screen should look as it did before
bulk mint existed; quantity selection belongs on the pay page, gated behind an
explicit Regenerate action.

## Goals

1. Restore the home screen to its pre-#272 look — no stepper, primary button
   always starts a single mint.
2. Move quantity selection onto the Mint **pay page** (the `showFlow` QR view).
3. The initial pay-page QR is always a quantity-1 single mint.
4. Changing quantity **immediately cancels** the live session/payload and
   invalidates (dims) the shown QR; a new QR is produced **only** when the user
   taps Regenerate, which is visually highlighted while the QR is stale.
5. Relabel the home action buttons (text only; keep emoji icons).

Non-goals: any server/endpoint change; altering the bulk job/session shapes;
touching the single-mint resume machinery; changing swap/market flows.

## Home-screen changes (`index.html`)

- **Remove** the `#mint-qty` stepper block (`index.html` lines ~53–57) from
  `.mint-row`. The row becomes just the `#mint-btn`.
- **Relabel** buttons (text only — emojis unchanged):
  | id | before | after |
  |----|--------|-------|
  | `mint-btn`    | ⛏️ Mint NFT      | ⛏️ Mint  |
  | `swap-btn`    | 🏗️ Build         | 🏗️ Build |
  | `swapper-btn` | 🔁 Trait Swapper | 🔁 Swap  |
  | `market-btn`  | 🛒 Marketplace   | 🛒 Trade |

## Pay-page quantity control

The stepper markup moves into the `showFlow` view (a `#flow-qty` block,
mirroring the old `#mint-qty` structure: `#qty-minus` / `#qty-value` /
`#qty-plus`). It renders **only** when:
- the flow view is the **mint** pay context (not swap/market), and
- `bulkCfg.enabled` is true (server `bulk_mint_ui` flag on).

Otherwise it stays hidden and the pay page is exactly today's single-mint page.

### State model (client)

- `bulkCfg = { enabled, max }` — from `/api/config` (unchanged).
- `selectedQty` — the stepper value (1..`bulkCfg.max`), default 1.
- `liveQty` — the quantity the currently-live session/job was created for
  (1 for a `MintSession`, `job.quantity` for a `BulkMintJob`).
- Derived `stale = selectedQty !== liveQty`.

### Behavior

- **Home → pay:** `mint-btn` calls `startMint()` (single `MintSession`,
  `liveQty = 1`, `selectedQty = 1`). QR shows immediately. Unchanged for users
  who never touch the stepper.
- **`−`/`+` press:** update `selectedQty` (clamped `[1, bulkCfg.max]`). If the
  new `selectedQty !== liveQty`:
  1. Cancel the live session/job server-side immediately (single →
     `POST /api/mint/{id}/cancel`; bulk → `POST /api/mint/bulk/{id}/cancel`),
     freeing the XUMM payload.
  2. Dim/blur the QR image and disable the "Accept in Xaman" link.
  3. Add `.needs-regen` (pulse) to the Regenerate button.
  If the press brings `selectedQty` back to a value with no live session, the
  QR stays dim until Regenerate (we do not resurrect a cancelled payload).
- **Regenerate (`flow-regen-btn`) — the commit gate:**
  - `selectedQty === 1` → `POST /api/mint` → single-mint pay view
    (`showFlow`), `liveQty = 1`.
  - `selectedQty > 1` → `POST /api/mint/bulk` → bulk pay view
    (`bulkPayView` / `pollBulk`), `liveQty = selectedQty`.
  - On success: clear `.needs-regen`, un-dim the QR, show the fresh QR.
  - When `selectedQty === liveQty` (QR merely expired, nothing changed): behaves
    exactly as today — a plain refresh of the same session
    (`regeneratePaymentQr` for single; bulk has no expiry so this case is
    single-only).
- **Cancel (`flow-cancel-btn`)**: unchanged — cancels the live session/job and
  returns home via `showMintHome()`.

### Why 1↔N always needs a Regenerate tap

Quantity 1 uses the single-mint endpoint/session; quantity >1 uses the bulk
endpoint/job (deliberately separate per #215 — different payload amount, job
shape, and resume path). There is no way to morph a live qty-1 QR into a bulk
QR without recreating the payload, so crossing the 1↔N boundary always cancels
and requires Regenerate. This is consistent with the "changing qty cancels;
only Regenerate rebuilds" rule and applies uniformly to every qty change.

## Styling (`styles.css`)

- `.needs-regen` on the Regenerate button: attention pulse (e.g.
  `animation` keyframe on box-shadow/opacity) so a stale QR reads as
  "action required."
- Dimmed-QR state (e.g. `.qr-stale` on the QR container): reduced opacity +
  slight blur, pointer-events off on the accept link.
- Respect `prefers-reduced-motion` — swap the pulse for a static highlight.

## Resume interaction

Boot resume is unchanged: `resumeBulkMint()` then `resumeMint()`
(`app.js` boot). A resumed bulk job renders via `renderBulkJob` /
`bulkPayView` with `liveQty = job.quantity` and `selectedQty` initialized to
match, so the stepper reflects the in-flight quantity. A resumed single mint
initializes both to 1.

## Testing

- **Smoke (staging, `BULK_MINT_UI_ENABLED=1`):**
  1. Home shows relabeled buttons, no stepper.
  2. Mint → pay page shows QR (qty 1) + stepper.
  3. Bump to 3 → QR dims, Regenerate pulses, prior payload cancelled
     (verify no live qty-1 payload lingers).
  4. Regenerate → bulk pay view for 3; pay → bulk progress/accept works.
  5. Drop back to 1 → dims/pulses; Regenerate → single-mint QR again.
  6. Cancel from pay page returns home cleanly.
- **Flag off (`BULK_MINT_UI_ENABLED=0`):** pay page shows no stepper; single
  mint behaves exactly as today (regression guard).
- Existing single-mint and bulk-job unit tests remain green; add client-facing
  assertions only where the no-build client harness (`webapp/` smoke tests)
  can reach the new DOM wiring.
