// webapp/client/mint_pure.js
// Pure decision logic for the mint flow, kept free of DOM/network code so it
// can be executed and unit-tested under Node (tests/test_mint_pure_js.py) —
// same split as market_pure.js.

// After a cancel attempt (issue #141), decide where the client goes.
//
//   cancelResult  — the session dict returned by POST /api/mint/{id}/cancel
//                   when it succeeded (2xx), else null. Note a 200 can carry
//                   a NON-cancelled terminal state: cancelling an
//                   already-finished session is a server-side no-op that
//                   echoes the real state (e.g. offer_ready).
//   refetchResult — the session dict from the GET /api/mint/{id} refetch the
//                   client performs when the cancel POST failed (409 payment
//                   already confirmed / transient error), else null when that
//                   refetch failed too (session gone) or was not attempted.
//
// Returns 'home'   — the session is cancelled or gone; back to the start.
//         'resume' — the session is still live (or ended some other way):
//                    the user may have PAID, so stay on the flow panel and
//                    keep polling until the real pipeline outcome renders
//                    (generating/minting stages, the offer_ready accept QR,
//                    or the real failure message). Never silently go home.
export function cancelMintOutcome(cancelResult, refetchResult) {
  const s = cancelResult || refetchResult;
  if (!s) return 'home'; // session unknown to the server on both calls
  return s.state === 'cancelled' ? 'home' : 'resume';
}

// Mint session resume: Discord mobile kills/reloads the Activity webview when
// the user app-switches to Xaman to sign, so the relaunched client has lost
// its in-memory currentMintId while the server-side mint session is still
// running. On boot the client asks GET /api/mint/active and re-attaches.
//
//   activeResult — the response body ({session: <dict>|null}), or null when
//                  the call itself failed.
//
// Returns the session id to resume polling, or null to show the normal home
// screen. The server only returns non-terminal sessions, but stay defensive:
// resuming a terminal session would strand the user on a dead flow panel.
const TERMINAL_MINT_STATES = new Set(['offer_ready', 'done', 'failed', 'payment_timeout', 'cancelled']);

export function activeMintSessionId(activeResult) {
  const s = activeResult && activeResult.session;
  if (!s || !s.id || TERMINAL_MINT_STATES.has(s.state)) return null;
  return s.id;
}
