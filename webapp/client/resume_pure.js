// Cold-boot session-resume decisions (issue #221). Pure functions only — no
// DOM, no fetch — so they run under Node (tests/test_resume_pure_js.py, same
// harness as mint_pure.js / market_pure.js). app.js's resumeAnyFlow() feeds
// the GET /api/sessions/active envelope through pickActiveFlow() and routes
// the winner to the existing per-flow poller.

// Priority: money/irreversibility first. A user can realistically only have
// one live flow (every start handler 409s `session_active` on a second), so
// ties are a defensive rarity.
export const FLOW_ORDER = ['mint', 'bulk', 'swap', 'market', 'economy', 'shop'];

// Client-side mirror of each flow's server TERMINAL_STATES (defensive: the
// server prunes terminal sessions before answering, but a race must never
// strand the user on a dead panel — same guard mint_pure's
// TERMINAL_MINT_STATES draws). Keep in lockstep with:
//   mint_flow / bulk_mint_flow / swap_flow / market_flow / economy_api
//   (webapp) / shop_flow TERMINAL_STATES.
// Note swap's offers_ready is terminal by design: the accept offers already
// sit in Xaman; resume must not re-drive signing.
const TERMINAL = {
  mint: new Set(['offer_ready', 'done', 'failed', 'payment_timeout', 'cancelled']),
  bulk: new Set(['done', 'failed', 'payment_timeout', 'cancelled']),
  swap: new Set(['done', 'failed', 'offers_ready', 'payment_timeout', 'cancelled']),
  market: new Set(['done', 'failed', 'unknown', 'listed']),
  economy: new Set(['done', 'failed']),
  shop: new Set(['done', 'failed']),
};

// pickActiveFlow(sessions) -> {flow, session} | null
//   sessions: the /api/sessions/active envelope ({mint,bulk,swap,market,
//   economy,shop}, each a session dict or null). Returns the highest-priority
//   live session (intact, so the caller can route on session.kind etc.), or
//   null when nothing is resumable.
export function pickActiveFlow(sessions) {
  if (!sessions) return null;
  for (const flow of FLOW_ORDER) {
    const s = sessions[flow];
    if (s && s.id && !TERMINAL[flow].has(s.state)) return { flow, session: s };
  }
  return null;
}
