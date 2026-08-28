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

// --- WalletConnect / Joey Wallet (#447) ---------------------------------
//
// A WalletConnect sign request reuses every flow's existing `link` field, but
// with an `lfg-wc://<request_id>` scheme in place of a xumm.app URL (see
// lfg_core/signing/walletconnect.py). There is no QR and no deep link to
// open: app.js hands the id to wcSign(), which fetches the txjson from
// /api/sign/{id} and asks Joey to sign it.

const WC_SCHEME = 'lfg-wc://';

export function isWcLink(link) {
  return typeof link === 'string' && link.startsWith(WC_SCHEME);
}

// The sign-request id carried by a WalletConnect link, or null for anything
// else (including a scheme with no id after it).
export function wcRequestId(link) {
  if (!isWcLink(link)) return null;
  return link.slice(WC_SCHEME.length) || null;
}

// What to POST to /api/sign/{id}/result for a Joey `xrpl_signTransaction`
// response. `hash` is present only when the wallet actually SUBMITTED the
// transaction; a submit that failed inside the wallet returns the signed
// tx_json with no hash, which is a failure — never report it as success.
export function wcResultAction(resp) {
  const hash = resp && resp.hash;
  if (typeof hash === 'string' && hash) return { hash };
  return { error: 'no hash returned' };
}

// A user declining in Joey arrives as a WalletConnect JSON-RPC error rather
// than a transport failure: post {rejected:true}, not {error}.
export function isWcRejection(err) {
  if (!err) return false;
  const code = err.code;
  if (code === 5000 || code === 4001) return true;
  return typeof err.message === 'string' && /reject/i.test(err.message);
}

// Is the server's answer to POST /api/sign/{id}/result the LAST word on this
// request? Only a terminal answer may retire the id client-side.
//
// A non-terminal answer — 202 (not on-ledger yet), 503 (ledger unreachable),
// or no body at all (the POST never landed) — means the row is STILL pending
// server-side. The client must then re-post the same stored outcome on a later
// tick; it must never re-sign, because the transaction may already have been
// submitted and a second signature would double-submit it.
const WC_TERMINAL_STATES = ['signed', 'rejected', 'failed', 'mismatch', 'expired',
                            'already_resolved'];
// Terminal refusals carry a code and no state: 409 already_resolved, 409
// tx_mismatch, 410 tx_not_found (expired without ever validating).
const WC_TERMINAL_CODES = ['already_resolved', 'tx_mismatch', 'tx_not_found'];

export function wcOutcomeTerminal(body) {
  if (!body) return false;
  // `state` is the authority where present: a 202 carries state 'pending'
  // alongside the tx_not_found code, and retiring it would wedge the request.
  if (body.state) return WC_TERMINAL_STATES.includes(body.state);
  return WC_TERMINAL_CODES.includes(body.code);
}
