# Session resume for swap / economy / market flows — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #221

## Problem

Discord **mobile** kills and relaunches the Activity webview every time the user
app-switches to Xaman to sign a payload (fresh `instance_id`/`frame_id` on
return). Any flow state held only in client JS — the session id being polled —
is lost, and the relaunched client boots straight to the mint home screen with
no way back to a flow the **server** is still running.

#216 fixed this for mint: `handle_mint_active` (`GET /api/mint/active`, `lfg_service/app.py`)
returns the caller's live non-terminal `MintSession` via the generic
`_active_session(...)` helper, and the client's `resumeMint()` /
`resumeBulkMint()` (`webapp/client/app.js`) call it on boot to re-attach and
resume the existing poll loop. The boot path already does this:

```js
if (me.wallet) {
  if (!(await resumeBulkMint()) && !(await resumeMint())) showMintHome();
}
```

Every other Activity-surfaced flow has the identical exposure and **no** resume:

- **Trait swap** — `swap_sessions` (`lfg_core/swap_flow.py::SwapSession`). The
  fee-QR screen forces the same app-switch. #216 added regenerate/cancel to
  swap, but a relaunched client still can't find its way back to a running swap.
- **Marketplace list / buy / cancel / bid / trait-list** — the
  `ListSession` / `BuySession` / `CancelSession` / `BidSession` /
  `BidAcceptSession` / `TraitSellSession` dataclasses in `lfg_core/market_flow.py`,
  all stored in the single `market_sessions` dict keyed by id with a `.kind`
  discriminator. Buy is highest-stakes (money in flight; settlement is
  restart-safe server-side, but the buyer loses all UI feedback mid-signature).
- **Economy ops** — harvest / assemble / equip / extract / deposit, wrapped as
  `EconomyWebSession` (`webapp/economy_api.py`) in the `economy_sessions` dict,
  again with a `.kind` discriminator.
- **Shop buy** — `shop_sessions` / `ShopBuySession` already has a *partial*
  resume path (`resumeShopBuy(sessionId)` in `app.js`), but only when a 409
  `session_active` is hit on a fresh buy — **not** on a cold boot after a
  webview relaunch.

The server keeps every one of these sessions running (they poll XUMM and drive
the state machine on their own tasks). The gap is purely the client's inability
to rediscover the session id after a relaunch.

## Constraints discovered

- **Read-only re-attach — no transaction is built.** Resume only rediscovers an
  in-memory session and re-renders it; it never signs, submits, or mints.
  SourceTag=2606160021 and provenance memos are therefore untouched — they were
  already stamped when the flow's payload was first built. No new tx path.
- **Per-user + per-platform isolation is already enforced.**
  `_active_session(sessions, terminal_states, discord_id, platform)` filters on
  both `discord_id` and `getattr(s, "platform", "discord")`, so a Discord
  session can't leak into a Telegram/web boot and vice-versa. Every status
  handler (`handle_swap_status`, `handle_market_*_status`, economy status) also
  re-checks `discord_id` + `platform` ownership before returning a session.
- **Terminal states must be pruned, not resumed.** Each flow owns a
  `TERMINAL_STATES` set — `swap_flow.TERMINAL_STATES`
  (`{done, failed, offers_ready, payment_timeout, cancelled}`),
  `market_flow.TERMINAL_STATES` (`{done, failed, unknown, listed}`),
  `economy_api.TERMINAL_STATES` (`{done, failed}`). The active-session endpoints
  already `_prune_sessions(...)` before scanning; the client resume picker must
  independently skip terminal states (defensive, mirroring
  `mint_pure.activeMintSessionId`'s `TERMINAL_MINT_STATES` guard) so a race can
  never strand the user on a dead panel. Note swap's `offers_ready` is terminal
  but the accept offer still sits in Xaman — resume shows the results screen, it
  does not re-drive signing.
- **`kind` is the router — but it is not currently in every `to_dict()`.**
  `ListSession.to_dict()` / `BuySession` / `CancelSession` / `BidSession` and
  `economy_api.economy_session_dict()` do **not** emit `kind`. The client can't
  route a resumed market/economy session to the right poll+render function
  without it. Surfacing `kind` in those payloads is a prerequisite.
- **Resume decision logic belongs in Node-testable pure modules** (the
  `mint_pure.js` / `market_pure.js` pattern), per the #216 carryover note — not
  buried in DOM-coupled `app.js` code.
- **No-build vanilla-JS client.** Any `app.js` edit and any change to an
  imported pure module bumps its `?v=` cache-buster in `webapp/client/index.html`
  (and the `import ... ?v=N` in `app.js`) in the same commit, or Discord serves
  a stale bundle.
- **ECONOMY_NETWORK seam is irrelevant to resume** — sessions live in the
  service process memory, not a per-network DB; the endpoint reads the same
  in-memory dicts the status handlers already serve.

## Design

Two independent seams: a server active-session endpoint, and a client boot
resume dispatcher. Mirror #216 exactly.

### Server — one consolidated endpoint (one boot round-trip)

Add `handle_sessions_active` (`@require_auth`) →
`GET /api/sessions/active`, returning every live flow for the caller in one
payload so the client boot makes a single request (the issue's preferred shape):

```json
{
  "mint":    { ...MintSession.to_dict() }    | null,
  "bulk":    { ...BulkMintJob.to_dict() }     | null,
  "swap":    { ...SwapSession.to_dict() }     | null,
  "market":  { ...<kind>Session.to_dict() }   | null,   // kind ∈ list|buy|cancel|bid|bid_accept|trait_list
  "economy": { ...EconomyWebSession.to_dict() } | null, // kind ∈ harvest|assemble|equip|extract|deposit
  "shop":    { ...ShopBuySession.to_dict() }  | null
}
```

Each field reuses the exact primitives the per-flow `.../active` and status
handlers already use — no new lookup logic:

```python
@require_auth
async def handle_sessions_active(request):
    user = request["user"]
    uid, plat = user["id"], _platform(user)
    _prune_sessions(swap_sessions, swap_flow.TERMINAL_STATES)
    _prune_sessions(market_sessions, market_flow.TERMINAL_STATES)
    _prune_sessions(economy_sessions, economy_api.TERMINAL_STATES)
    _prune_shop_sessions()

    def pick(store, terminal):
        s = _active_session(store, terminal, uid, plat)
        return s.to_dict() if s else None

    return web.json_response({
        "mint":    pick(mint_sessions,   mint_flow.TERMINAL_STATES),
        "bulk":    (lambda j: j.to_dict() if j else None)(
                       _active_session(bulk_sessions, bulk_mint_flow.TERMINAL_STATES, uid, plat)),
        "swap":    pick(swap_sessions,   swap_flow.TERMINAL_STATES),
        "market":  pick(market_sessions, market_flow.TERMINAL_STATES),
        "economy": pick(economy_sessions, economy_api.TERMINAL_STATES),
        "shop":    pick(shop_sessions,   _SHOP_TERMINAL_STATES),
    })
```

Registered next to the existing routes in `create_app`:
`app.router.add_get("/api/sessions/active", handle_sessions_active)`.
The existing `/api/mint/active` and `/api/mint/bulk/active` stay (backwards
compat; the unified endpoint is additive and the boot path migrates to it).

**Surface `kind` in the routable payloads** (prerequisite — see Constraints):
- `market_flow.py`: add `"kind": self.kind` to `to_dict()` on `ListSession`,
  `BuySession`, `CancelSession`, `BidSession`, `BidAcceptSession`,
  `TraitSellSession` (each already carries a `kind` field whose value matches a
  `MARKET_STATUS_PATH` key — `list`/`buy`/`cancel`/`bid`/`bid_accept`/`trait_list`).
- `webapp/economy_api.py`: add `"kind": kind` to the `base` dict in
  `economy_session_dict()` (value ∈ `harvest`/`assemble`/`equip`/`extract`/`deposit`,
  matching the `/api/<kind>/<id>` status path).

These are purely additive keys; no existing consumer breaks (the per-flow
pollers ignore unknown keys).

### Client — one boot resume dispatcher

New pure module `webapp/client/resume_pure.js` (Node-testable, no DOM), with the
priority picker:

```js
// Priority: money/irreversibility first. Returns {flow, session} or null.
const ORDER = ['mint', 'bulk', 'swap', 'market', 'economy', 'shop'];
const TERMINAL = {
  mint: new Set(['offer_ready','done','failed','payment_timeout','cancelled']),
  bulk: new Set(['done','failed','cancelled','payment_timeout']),
  swap: new Set(['done','failed','offers_ready','payment_timeout','cancelled']),
  market: new Set(['done','failed','unknown','listed']),
  economy: new Set(['done','failed']),
  shop: new Set(['done','failed','expired','settled']),
};
export function pickActiveFlow(sessions) {
  for (const flow of ORDER) {
    const s = sessions && sessions[flow];
    if (s && s.id && !TERMINAL[flow].has(s.state)) return { flow, session: s };
  }
  return null;
}
```

`app.js` gains `resumeAnyFlow()`, replacing the two-call
`resumeBulkMint()/resumeMint()` boot chain with one round-trip:

```js
async function resumeAnyFlow() {
  let sessions = null;
  try { sessions = await api('/api/sessions/active'); } catch (_) { return false; }
  const picked = resumePure.pickActiveFlow(sessions);
  if (!picked) return false;
  const { flow, session } = picked;
  switch (flow) {
    case 'mint':    return attachMintResume(session);      // existing resumeMint body, minus its own fetch
    case 'bulk':    return attachBulkResume(session);       // existing resumeBulkMint body
    case 'swap':    openSwapper(); pollSwap(session.id); return true;   // pollSwap already renders every state
    case 'market':  return attachMarketResume(session);     // showPanel('flow-panel') + pollMarketFlow(kind, id, RENDER[kind])
    case 'economy': return attachEconomyResume(session);    // reopen economy panel + pollEconomyOp(kind, session)
    case 'shop':    await resumeShopBuy(session.id); return true;       // existing
  }
  return false;
}
```

Boot sites (both the `insideWeb` branch and the Telegram/Discord branch) become:

```js
if (!(await resumeAnyFlow())) showMintHome();
```

Per-flow attach helpers reuse existing renderers/pollers verbatim:
- **swap** → `openSwapper()` to reveal the swap panel, then `pollSwap(id)`;
  `pollSwap` already branches on `awaiting_payment` (`renderSwapPayment`),
  `offers_ready` (`renderSwapResults`), `payment_timeout`, `failed`.
- **market** → `showPanel('flow-panel')`, then
  `pollMarketFlow(session.kind, session.id, RENDER[session.kind])` where `RENDER`
  maps each kind to its existing render fn (the same ones `marketFlow(...)`
  passes today); `MARKET_STATUS_PATH[kind]` already resolves the poll URL.
- **economy** → reveal the economy/build panel and hand the session dict to
  `pollEconomyOp(session.kind, session)` (its first arg is the kind, second the
  start-shaped response — the resumed `to_dict()` is that shape). A small
  "Reconnecting…" `showFlow` covers the gap, matching `resumeMint`'s banner.

All resume renderers show the same `🔄 Reconnecting…` spinner banner
`resumeMint()` uses before the first poll tick lands.

### Data-model changes

None to any DB or on-ledger object. The only wire-format change is the additive
`kind` key in the market/economy status payloads and the new
`/api/sessions/active` response envelope.

### On-ledger tx shape

No transaction is built by this feature. SourceTag / memos are unaffected.

## Out of scope

- Fixing the **root cause** — the wedged XUMM push delivery (#212). Once push
  works for registered users the forced app-switch (and thus relaunch
  frequency) drops, but the resume gap is independently real and this issue
  closes it.
- Cross-**surface** resume (start on Discord, resume on web) — sessions remain
  per-platform by design; `_active_session` filters on platform.
- Persisting sessions across a **service restart** (they are in-memory). Bulk
  mint already has durable disk resume (`bulk_mint_flow.load_all_resumable`);
  swap/market/economy do not, and adding it is a separate, larger effort. This
  issue only covers webview relaunch, where the server process keeps running.
- Discord-bot / Telegram-native resume — those surfaces don't run the Activity
  webview; the exposure is Activity-specific.

## Open questions / decisions for maintainer

1. **One unified endpoint vs. per-flow endpoints.** This draft chose the single
   `GET /api/sessions/active` (issue's preferred "one round-trip" shape) and
   keeps the existing `/api/mint/active` for compat. Alternative: add
   `/api/swap/active`, `/api/market/active`, `/api/economy/active` mirroring
   mint exactly and leave the boot chain as sequential probes. Unified is one
   request but a wider blast radius on the boot path; confirm the trade.
2. **Resume priority order.** Draft ranks mint > bulk > swap > market >
   economy > shop (money/irreversibility first). A user can realistically only
   have one live flow at a time (each start-handler 409s `session_active` on a
   second), so ties should be rare — but confirm the ordering, especially
   market-buy vs. swap.
3. **Economy resume UX.** `pollEconomyOp` currently returns a Promise that the
   *caller* awaits and then renders the terminal result; harvest/assemble also
   have fire-and-forget tracker UI (spec 2026-07-21). Should a resumed economy
   op re-open its full panel, or just show a compact "operation in progress →
   result" flow overlay? Draft assumes the latter (simplest, matches mint's
   reconnect banner).
4. **Should `/api/mint/active` be retired** once the boot path uses the unified
   endpoint, or kept indefinitely? Draft keeps it (zero-risk, still used by any
   cached client bundle).
5. **Bulk `TERMINAL_STATES` client mirror** — confirm the exact terminal set for
   bulk in `resume_pure.js` matches `bulk_mint_flow.TERMINAL_STATES` server-side
   (draft lists `done/failed/cancelled/payment_timeout` — verify against the
   module).

## Testing

**Unit (pytest, `tests/`):**
- `handle_sessions_active`: seed one live `SwapSession` / `BuySession` /
  `EconomyWebSession` for a user and assert the endpoint returns each under the
  right key with the right `state`, and `null` for empty flows. Assert a
  terminal session is pruned/omitted. Assert a session for a *different*
  `discord_id` or `platform` does **not** appear (isolation).
- `to_dict()` additions: assert `kind` is present and correct on each market
  session and each `economy_session_dict` kind.

**Unit (Node, `webapp/client/*.test` harness like `mint_pure`):**
- `resume_pure.pickActiveFlow`: priority ordering; skips terminal per-flow;
  returns `null` on an all-null / all-terminal payload; picks the higher-priority
  flow when two are (defensively) live.

**Integration (aiohttp test client):**
- Boot a full swap to `awaiting_payment`, then hit `/api/sessions/active` and
  assert `swap` carries the payment link and non-terminal state (proves a
  relaunched client can re-render the fee QR).

**Manual smoke (Discord mobile, the real repro):**
- Start a trait swap → reach the fee QR → app-switch to Xaman and back (forces
  webview relaunch) → confirm the Activity reopens onto the swap fee QR, not the
  mint home. Repeat for a marketplace **buy** and an economy **harvest**.
- Confirm a completed flow relaunched *after* it finished lands on the home
  screen (terminal pruned), not a dead panel.
