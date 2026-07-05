# Web UI (#42) Re-scope — Design

**Issue:** Team-Hamsa/LFG #42 "feat: Web UI" (open). Written pre-spine; most of
its scope has since shipped as the vanilla-JS Activity (`webapp/`, served on
:8176 by `webapp/server.py` → `lfg_service/app.py`). This doc inventories what
exists, marks what is spec'd elsewhere, and designs ONLY the genuine remainder.

## 1. Inventory — what the Activity already is

Grounded in code read on branch `feat/shared-layer-dirs` (matches origin/main
for these files):

- **Frontend** `webapp/client/app.js` (1436 lines, no-build vanilla JS).
  Panels (`ALL_PANELS`, app.js:155-157): `register-panel`, `mint-panel`,
  `flow-panel`, `swap-panel`, `swap-traits-panel`, `swap-result-panel`,
  `dressup-panel`. Plus a two-tier leaderboard selector (app.js:175-299,
  8 boards via `GET /api/leaderboard`). Startup calls `/api/me` and routes to
  mint home if a wallet is registered (app.js:1424-1425); "change wallet"
  re-runs the Xaman sign-in (app.js:1401).
- **Backend** `lfg_service/app.py` (route table :1253-1289): config, token,
  session, telegram/auth, me, account, register, mint (+status/regenerate),
  signin (+status), nfts, leaderboard, swap (+status), qr.png, img, layer,
  closet, economy, equip/harvest/assemble/extract/deposit (+statuses),
  /events, /events/me, static client.
- **Auth modes** (all mint the same HMAC session token,
  `make_session_token` app.py:253):
  1. **Discord Activity**: `POST /api/token` — Embedded App SDK OAuth2 code
     exchange against Discord (app.py:343-381). Requires `DISCORD_CLIENT_SECRET`
     and only works launched inside Discord.
  2. **Bot surfaces**: `POST /api/session` — `@require_service_token`
     (app.py:386-397); needs `SERVICE_TOKEN_*`, not client-callable.
  3. **Telegram Mini App**: `POST /api/telegram/auth` — HMAC-validated
     `initData` (app.py:399-435); only works launched from Telegram.
  4. **Dev**: `WEBAPP_DEV_MODE` short-circuits `require_auth`/`require_wallet`
     to a mock user/owner (app.py:293, :313; flag from lfg_core/config.py:136).
- **Xaman sign-in exists but is NOT a bootstrap**: `POST /api/signin`
  (app.py:940-964) creates a XUMM SignIn payload and `GET /api/signin/{uuid}`
  (app.py:967-1013) captures + registers the signed wallet — but **both are
  `@require_auth`**: they *attach a wallet to an existing platform session*
  (discord/telegram id). There is no way to *create* a session from a wallet
  signature. A plain browser hitting :8176 has no path to a session token
  except `WEBAPP_DEV_MODE`.
- **Wallet/identity**: `/api/me` (wallet resolution, app.py:437-451),
  `/api/account` (own wallet + linked identities only — explicitly no public
  wallet lookup, app.py:453-462), `identity_store` link on sign-in
  (app.py:1000-1006).
- **No admin routes**: no burn/lookup/stats endpoints in the route table;
  admin exists only as Discord `/admin` (main.py).

## 2. Gap table — #42 scope × status

| #42 scope item | Status | Evidence |
|---|---|---|
| Wallet connect (XUMM deep link / QR) | **Done-in-Activity** *within a platform session*; **genuinely missing as a standalone-browser bootstrap** | `/api/signin` + status (app.py:940-1013) capture the wallet via Xaman SignIn, QR rendered same-origin via `/api/qr.png` (app.py:1030); but both endpoints are `@require_auth` — no session ⇒ no sign-in. |
| Mint flow in browser | **Done-in-Activity** | `POST /api/mint` + status/regenerate (app.py:716-895); full pay→generate→mint→offer UI in app.js:362-530 (LFGO and XRP paths). |
| Collection viewer w/ trait filters | **Spec'd-elsewhere** | *Own* collection grid exists (swap picker, `GET /api/nfts` app.py:752, app.js:629-660); NFT rarity/swaps leaderboards exist. The *public browse-with-filters* surface is owned by `2026-07-05-marketplace-design.md`: its `market_listings` table + browse API do the traits × price × liveness join over the collection (marketplace spec :71-89, :146-147) and its webapp market panel reuses the same `showPanel`/grid patterns (:58). The public per-NFT page is `2026-07-05-x-integration-design.md` §6.2 (OG card page `GET /nft/<number>`); AMM widget hooks are `2026-07-05-amm-backend-design.md` §4. **Drop from #42** (see §5). |
| User profile page | **Genuinely missing** | Only `wallet-display` text (app.js:166) and privacy-scoped `/api/account`. No page showing owned NFTs + mint/swap history; the data exists (`history_<net>.db` `nft_events`, on-chain index, leaderboard `me=` resolution app.py:504+). |
| Admin dashboard | **Genuinely missing** (recommend defer) | No admin endpoint in lfg_service; Discord `/admin` covers stats/lookup/burn. |
| Runs outside Discord (implied) | **Genuinely missing** | Auth modes 1-3 all require a host platform; `WEBAPP_DEV_MODE` is a mock, not auth. Public HTTPS exposure overlaps #89 Part B ops (expose :8176, BotFather URL) — ops, not code. |

## 3. Genuine gap A — standalone-browser wallet session ("web" platform)

Goal: a plain browser at the public URL can become an authenticated session by
proving wallet ownership via Xaman — reusing the existing SignIn machinery
(`xumm_ops.create_signin_payload` / `get_payload_status`, already exercised by
app.py:954 and :983).

Design — two unauthenticated endpoints (mirror the signin pair, inverted:
they *create* the session rather than requiring one):

- `POST /api/web/signin` → creates a XUMM SignIn payload, stores
  `{created_at, client_ip}` keyed by payload uuid in a new
  `web_signin_payloads` dict (prune pattern as `_prune_signin_payloads`,
  app.py:933). Returns `{uuid, signin_link}`; client renders via existing
  `/api/qr.png`.

  **Abuse controls** (this is an unauthenticated payload-creation endpoint —
  a spam vector against the XUMM app quota and a session-fixation surface;
  each control below is a designed behavior, not a TODO):
  - **Per-IP creation throttle:** sliding-window counter keyed on client IP
    (from the reverse-proxy `X-Forwarded-For` left-most hop when
    `WEBAPP_TRUST_PROXY` is set, else the socket peer): max **5 pending
    payloads per IP** and max **10 creations per IP per 10 minutes** → 429
    with `Retry-After`. Global backstop: max 200 pending payloads total →
    429.
  - **Payload expiry:** the payload is created with XUMM
    `options.expire = 10` (minutes — same choice as the AMM spec's signed
    payloads), and the server-side record is pruned on the same 10-minute
    clock; a pruned/expired uuid behaves exactly like an unknown one.
- `GET /api/web/signin/{uuid}` → polls `get_payload_status`. On
  `signed && is_valid_classic_address(account)`:
  - **Signed-account binding:** the session wallet is exactly `s["account"]`
    as returned by the XUMM API server-side (`get_payload_status`, same
    field the existing flow trusts at app.py:983-989). The client never
    supplies a wallet in any request; there is no wallet parameter to either
    endpoint. This kills session fixation: knowing/leaking a uuid only lets
    an attacker receive a session bound to *whichever wallet actually
    signed* — the QR-scanning wallet holder, whose approval in Xaman is the
    consent event.
  - mint a session token with `platform="web"`, `id=<signed account>`,
    `name=""` (`make_session_token` app.py:253 already carries `platform`);
  - `identity_store.link("web", wallet, "", wallet)` so `/events/me`,
    leaderboard `me=`, and `/api/account` work;
  - **do NOT write the legacy Users table** — it is discord-keyed only
    (guard already documented at app.py:669-674 and :986-988);
  - **One-time exchange:** the payload record is deleted *before* the token
    is returned; the token is issued **exactly once**. A subsequent GET for
    the same uuid → **410 Gone** (tombstone the uuid for the residual expiry
    window so replay is distinguishable from never-existed, which stays 404).
  - Fail-closed: anything short of a XUMM-verified signed classic address
    returns pending/expired/404/410 — never a token. Unknown uuid → 404,
    expired → `{state:"expired"}` then pruned, unsigned → `{state:"pending"|
    "opened"}` (no oracle beyond payload state).
- `_resolve_wallet` (app.py:283) gets a `platform == "web"` branch:
  the wallet IS the user id — return `user["id"]` if it's a valid classic
  address, else None (no DB hop). `require_wallet` then works unchanged for
  mint/swap/economy.
- Frontend: `webapp/client/app.js` boot path — when not in Discord/Telegram
  and `dev_mode` is false (`/api/config` app.py:1016-1023 already exposes the
  flag), show a "Connect wallet" panel that drives the new endpoint pair and
  stashes `session_token` in `sessionStorage` (not localStorage: 24h TTL
  tokens, shared machines). Everything downstream (mint, swap, dressup,
  leaderboards) reuses the existing `api()` bearer plumbing.
- Sessions are bearer-token, TTL from `SESSION_TTL`; re-auth = rescan. No
  refresh tokens in MVP.

Explicitly *not* in scope: linking a web session to Discord/Telegram
identities is already handled by the #90 link-intent flow once the wallet
matches — no new code.

## 4. Genuine gap B — per-wallet profile page

- `GET /api/profile/{wallet}` — **public, read-only** (consistent with
  `/api/leaderboard` being public). Validates classic address; returns:
  `{wallet, display_name (via _lb_display_name app.py:497 rules — respects
  the /api/account privacy stance: display handle only, never the identity
  list), nfts: [...owned live editions from the on-chain index...],
  stats: {mints, swaps, builds} and recent events from history DB
  (nft_events keyed by wallet)}`. Per-network like the leaderboard. 60s cache
  reusing the `_LB_CACHE` pattern (app.py:465-484).
- Frontend: `profile-panel` added to `ALL_PANELS`; reachable from leaderboard
  rows (click a row → profile) and from "my profile" when authed. Deep-link
  `#/profile/rXXX` hash routing (the client is hash-friendly; no server route
  changes needed since `/` serves the SPA).
- Privacy line: shows only what the chain already shows (owned NFTs, tx
  events) + the display handle the leaderboard already exposes. Never linked
  platform identities (that stays owner-only via `/api/account`).

## 5. Deliberately NOT designed

- **Browser admin dashboard — defer.** Rationale: (1) Discord `/admin`
  already covers stats/lookup/burn with Discord-native permission gating;
  (2) a browser version needs an admin-grade auth story (wallet-allowlist or
  Discord-role check over OAuth) that gap A does not provide — a "web"
  session proves wallet ownership, not operator status, and shipping burn
  buttons behind a new auth surface on a newly public host is the riskiest
  possible first tenant; (3) zero user-facing value for the July 21 mainnet
  push. Re-open as its own issue if operating from Discord becomes painful.
- **Public trait-filter collection browser — drop from #42.** Now
  affirmatively owned elsewhere: `2026-07-05-marketplace-design.md` builds
  the browse-with-filters API + webapp market panel (traits × price ×
  liveness over `market_listings`), `2026-07-05-x-integration-design.md`
  §6.2 provides the public per-NFT OG page, and the AMM widget is
  `2026-07-05-amm-backend-design.md` §4. Duplicating any of that in #42
  would be pure overlap.
- **Public HTTPS exposure — ops, tracked in #89 Part B**, not #42 code scope
  (same host:8176, same TLS/BotFather step). #42 gap-A code must merely be
  *safe* on a public origin (throttled unauth endpoints, fail-closed).
  Shared with the x-integration OG page: both go live behind the same
  public origin, and **all absolute public URLs — profile deep links
  included — build from `PUBLIC_SHARE_BASE_URL` (x-integration spec
  :229-256), never from the request Host header** (host-header injection on
  a public origin). Profile share links become
  `<PUBLIC_SHARE_BASE_URL>/#/profile/rXXX`; unset ⇒ no absolute self-URLs
  emitted, relative hash links only.

## 6. Recommended re-scoped #42 body (ready to paste)

```markdown
## Summary (re-scoped 2026-07-05)
Most of the original scope shipped as the Activity (webapp/ on :8176):
mint flow, swap, dressing room/closet, leaderboards, Xaman wallet capture —
inside Discord (Embedded App SDK) and Telegram (Mini App). See
docs/superpowers/specs/2026-07-05-web-ui-rescope-design.md for the full
inventory/gap table. Remaining scope:

## Scope
- [ ] Standalone-browser wallet session: unauthenticated `POST /api/web/signin`
      + `GET /api/web/signin/{uuid}` mint a `platform="web"` session from a
      verified Xaman SignIn. Fail-closed with concrete abuse controls:
      per-IP sliding-window throttle (429), XUMM `options.expire=10`,
      one-time payload→session exchange (replay → 410), session wallet bound
      server-side to the XUMM-reported signed account (never client-asserted),
      no legacy Users write. Frontend "Connect wallet" boot panel when not
      hosted by Discord/Telegram.
- [ ] Public per-wallet profile page: `GET /api/profile/{wallet}` (owned NFTs
      from on-chain index + mint/swap stats from history DB, display-handle
      only) + `profile-panel` with `#/profile/rXXX` deep links, reachable from
      leaderboard rows. Absolute share URLs built from `PUBLIC_SHARE_BASE_URL`
      (per the x-integration spec), never the Host header.

## Out of scope (was in the original issue)
- Browser admin dashboard — deferred; Discord `/admin` covers it and a web
  admin needs its own auth design. Open a new issue if needed.
- Trait-filter collection browser — owned by sibling specs: marketplace
  browse API + market panel (docs/superpowers/specs/2026-07-05-marketplace-design.md),
  public per-NFT OG page (…/2026-07-05-x-integration-design.md §6.2),
  AMM widget (…/2026-07-05-amm-backend-design.md §4).
- Public HTTPS exposure — ops work tracked in #89 Part B (shared dependency
  with the x-integration OG page).
```

## 7. Assumptions vs verified

- **Verified**: everything in §1-2 by reading the cited lines. Sibling-spec
  claims verified against origin/main @ c2b5cff (six 2026-07-05 specs:
  amm-backend, brix-daily-distribution, marketplace, phase-aware-sync-persist,
  tx-hygiene, x-integration). An earlier draft of this doc claimed the
  marketplace/x-integration specs did not exist — that was a stale-fetch
  error, corrected 2026-07-05.
- **Assumed (verify in implementation)**: `xumm_ops.create_signin_payload`
  works with `return_url=None` (called with a Discord return URL today,
  app.py:954/:713 — a web flow passes none) and accepts/needs an
  `options.expire` argument; `identity_store.link` accepts a wallet as
  `platform_user_id`; SESSION_TTL value acceptable for web UX; client IP
  extraction behind the eventual #89 reverse proxy (`WEBAPP_TRUST_PROXY`
  semantics to be confirmed against the actual proxy config).
