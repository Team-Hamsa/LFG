// signdelivery_pure.js — Xaman sign-request delivery decisions (#142).
//
// Pure, DOM-free module (same pattern as mint_pure.js / build_pure.js) so the
// truth table is executable under Node (tests/test_signdelivery_pure_js.py).
// app.js consumes it via applySignDelivery(): on a coarse-pointer (touch)
// device the deep link is the primary affordance — tapping opens the sign
// request directly in the Xaman app, auto-opened once per payload — and the
// QR collapses behind a "sign on another device" disclosure. On a fine
// pointer (desktop) the QR stays primary, exactly as today, preserving the
// desktop-screen -> phone-camera cross-device path.

// Decide how a sign request is presented.
//   push:    per-payload delivery state from the backend (#212):
//            'sent' = push-delivered to the user's Xaman app already,
//            'failed' / null = QR + deep link are the only paths.
//   coarse:  primary input is touch (matchMedia('(pointer: coarse)')).
//   hasLink: a xumm_url deep link is available.
//   hasQr:   QR data is available.
// Returns { linkPrimary, qrCollapsed, autoOpen }.
export function signDelivery({ push = null, coarse = false, hasLink = false, hasQr = false } = {}) {
  const linkPrimary = coarse && hasLink;
  // Never collapse the QR when it is the only affordance left (no link).
  const qrCollapsed = hasQr && hasLink && (push === 'sent' || linkPrimary);
  // Auto-open at most once per payload, and never when push already delivered
  // the request into Xaman (an unprompted app-switch on top of a push is
  // jarring and redundant).
  const autoOpen = linkPrimary && push !== 'sent';
  return { linkPrimary, qrCollapsed, autoOpen };
}

// Dedup guard: open a payload's deep link at most once. `seen` is the list of
// links already auto-opened; a falsy link never opens.
export function shouldAutoOpen(seen, link) {
  if (!link) return false;
  return !seen.includes(link);
}

// Post-launch bookkeeping: `launched === false` means the launch was
// DETECTABLY blocked (window.open returned null under a popup blocker) — the
// link is un-marked so a later render may retry the auto-open. Any other
// outcome (success, or an opener whose result is undetectable, like the
// Discord SDK's promise) keeps the optimistic mark.
export function autoOpenOutcome(seen, link, launched) {
  if (launched === false) return seen.filter((l) => l !== link);
  return seen;
}
