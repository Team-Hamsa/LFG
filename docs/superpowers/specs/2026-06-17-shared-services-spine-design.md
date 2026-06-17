# Shared-Services Spine — Design Spec

**Date:** 2026-06-17
**Issues:** #53 (migrate `main.py` onto `lfg_core`), #43 (Telegram integration). Forward-looking for #42 (Web UI), #41 (X integration).
**Status:** Approved design, pre-implementation.

## 1. Problem

LFG has multiple interaction surfaces growing independently:

- **`main.py`** — the legacy Discord bot (1,897 lines). It predates the shared-services refactor and carries *parallel* implementations of mint / compose / rarity logic, composites from local `trait_layers/` test art, and never adopted `lfg_core`.
- **`webapp/server.py`** — the Discord Activity backend. This one *does* consume `lfg_core` and is the de-facto example of the target pattern.
- **Planned:** Web UI (#42), Telegram (#43), X/Twitter (#41) — three more surfaces that each need the same mint/swap/rarity/identity spine.

The risk is divergence: two (soon five) code paths for the same domain logic mean bugs fixed in one don't reach the others, and surfaces can mint art/rarity that disagree. The intended architecture is **one shared pipeline behind every front-end**.

`lfg_core/` already holds the Discord-free domain logic (`mint_flow`, `swap_flow`, `rarity`, `xrpl_ops`, `xumm_ops`, `layer_store`, `cdn`, `config`, `traits`). What's missing is a **service layer** that owns runtime state (sessions), identity, outbound events, and a stable API — so every surface becomes a thin client of one backend instead of re-implementing the pipeline.

## 2. Goals & Non-Goals

### Goals
- Define the **`lfg-service`** contract: REST (commands/status) + WebSocket (outbound events), service-level auth, and a generalized identity model.
- **Promote `webapp/server.py`** into that canonical service (it already implements ~80% of the REST surface).
- **Migrate the Discord bot (#53)** to consume the service as a thin adapter — deleting its parallel pipeline so Discord and web mint identical art/rarity.
- **Add the Telegram surface (#43)** as the first consumer of the outbound event bus, validating both directions of the contract.
- Provide a **shared surface SDK** so clients don't each reimplement HTTP/WS plumbing.

### Non-Goals (this spec)
- Web UI (#42) and X integration (#41) — separate downstream specs that implement adapters against this spine.
- Linked multi-surface profiles (the "one human, many surfaces/wallets" feature) — deferred; the schema reserves a hook for it.
- Redis / external broker adoption — deferred behind an interface (see §6).
- Externalizing session state for restart-survival — sessions stay in-process.

## 3. Architecture & Topology

Three layers; surfaces are separate processes talking to a single always-on service over HTTP/WS.

```
┌─────────────────────────────────────────────────────────────┐
│  SURFACES (thin clients, separate processes)                 │
│  discord_bot/    telegram_bot/   webapp client   x_poster/   │
│  (main.py→cog)   (#43)           (browser)       (#41, later)│
│       │               │               │              │       │
└───────┼───────────────┼───────────────┼──────────────┼───────┘
        │  HTTP (commands/status)   +   WS (outbound events)
┌───────┴──────────────────────────────────────────────────────┐
│  lfg-service  (single always-on process)                      │
│  ─ REST API:  /api/mint, /swap, /register, /nfts, /status …   │
│  ─ WS /events: mint.completed, swap.completed, … (pub/sub)    │
│  ─ Session registry, identity resolver, service-auth, bus     │
├───────────────────────────────────────────────────────────────┤
│  lfg_core  (unchanged domain library)                         │
│  mint_flow · swap_flow · rarity · xrpl_ops · xumm_ops ·       │
│  layer_store · cdn · config · traits                          │
└───────────────────────────────────────────────────────────────┘
```

Decisions baked in:
- `lfg_core` is **unchanged**; the new work is the service wrapper, not a domain rewrite.
- The existing `webapp/server.py` API becomes the canonical service for *all* surfaces; the browser client stays served by it, bots become HTTP/WS clients.
- Each surface authenticates with a **per-surface service token**, distinct from end-user auth.
- **REST** for request/response; **WebSocket `/events`** for the service to push domain events outward (so Telegram/X/Discord-admin notify without polling).
- The structural heart of #53 is **inverting `main.py`**: it currently *owns* the mint pipeline; afterward it *calls* the service for it and renders results as Discord embeds/QRs.

## 4. Service Contract

### 4.1 Authentication — two distinct layers

- **Service auth.** Every surface process holds a per-surface bearer token (`SERVICE_TOKEN_DISCORD`, `SERVICE_TOKEN_TELEGRAM`, …) sent as `Authorization: Bearer <token>`. The service validates against a configured set and tags the caller with a `surface` name. Gates who may call the API at all; lets privileged endpoints (admin, notify) be restricted per surface.
- **End-user auth.** Unchanged from today's webapp: the HMAC-signed session token (`make_session_token`) identifies the human. The browser uses Discord OAuth; bots mint a user session by asserting `(platform, platform_user_id)`, trusted *because* the surface presented a valid service token.

### 4.2 REST endpoints

Existing webapp routes are kept and generalized: `discord_id` becomes a generic `identity = {platform, platform_user_id}`.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| POST | `/api/session` | Surface asserts a user identity → user session token | OAuth still valid for web |
| POST | `/api/register` | Map identity → wallet | writes `identities` |
| GET  | `/api/me` | Resolve caller → wallet, history | |
| POST | `/api/mint` | Start a mint session | returns `session_id` + payment link/QR data |
| GET  | `/api/mint/{id}` | Poll mint status | `mint_flow` state machine |
| POST | `/api/mint/{id}/regenerate` | Reroll traits | existing |
| POST | `/api/swap` | Start swap session | |
| GET  | `/api/swap/{id}` | Poll swap status | |
| GET  | `/api/nfts` | List owned NFTs | |
| POST | `/api/signin` + GET `/api/signin/{uuid}` | XUMM sign-in | existing |
| GET  | `/api/qr.png`, `/api/img` | QR + CDN image proxy | existing |
| GET  | `/api/config` | Public client config | |

Endpoints return structured errors `{error, code}` so surfaces can map codes to friendly messages.

### 4.3 Identity model

Centralized in a new `lfg_service/identity.py`: `resolve(platform, platform_user_id) -> wallet | None` and `link(platform, platform_user_id, wallet)`. The XRPL **wallet is the canonical account**; surfaces map their platform user-id to it. The `Users` table is migrated to an `identities` table:

```sql
CREATE TABLE identities (
    platform          TEXT NOT NULL,   -- 'discord' | 'telegram' | 'web' | 'x'
    platform_user_id  TEXT NOT NULL,
    platform_username TEXT,
    wallet            TEXT NOT NULL,
    account_id        INTEGER,         -- NULL now; reserved for future linked profiles
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (platform, platform_user_id)
);
```

Existing `Users` rows backfill as `platform='discord'`. `account_id` is the forward hook for linked multi-surface profiles — nullable and unused now, so that feature lands later without a migration. A Discord user and a Telegram user with the same wallet are, for now, independent identity rows that happen to resolve to the same wallet.

**Migration:** an idempotent `init_db`/migration step creates `identities`, copies `Users(discord_id, discord_name, wallet)` → `identities('discord', discord_id, discord_name, wallet)`, and leaves `Users` in place during transition (read paths switch to `identities`; `Users` dropped in a later cleanup once no code reads it).

### 4.4 WebSocket event bus

`GET /events` (WS upgrade, service-token auth). Surfaces subscribe with an optional type filter. The service publishes domain events that `mint_flow` / `swap_flow` emit at terminal transitions.

Envelope:
```json
{
  "type": "mint.completed",
  "ts": 1718600000,
  "identity": {"platform": "discord", "platform_user_id": "123"},
  "wallet": "rXXXX",
  "data": {"nft_number": 3712, "nft_id": "000800...", "image_url": "...", "traits": {}}
}
```

Event types (this spec): `mint.completed`, `mint.failed`, `swap.completed`, `swap.failed`. (`x.*` share events deferred to #41.)

Two endpoints over the same bus, distinguished by **who** connects:

- **`GET /events`** — **service-token** auth. For trusted backend surfaces (Discord bot, Telegram bot) that fan out to many users; the connection receives the firehose (subject to its type filter) because one process serves all its users.
- **`GET /events/me`** — **user-session-token** (HMAC) auth. For untrusted single-user clients (the browser). The service **filters strictly** to the authenticated user's own events (matched on resolved identity/wallet) — a browser can never observe another user's events, and never holds a service token.

Consumers:
- **Telegram** (`/events`) → posts mint announcements to a channel / DMs the minter; failures to a private admin channel.
- **Discord** (`/events`) → drives the existing admin-log channel + optional user DM.
- **Browser** (`/events/me`) → push replaces per-session polling: instant `mint.completed` / `swap.completed` for that user, fewer requests, lower latency. (Polling remains as a fallback if the WS drops.)

**Delivery semantics:** at-most-once, in-memory pub/sub, no broker. A subscriber disconnected when an event fires misses it — acceptable for notifications at this stage (the browser's polling fallback covers its own session). Replay/guaranteed delivery is the Redis-later upgrade (§6).

## 5. Surfaces

### 5.1 Repo layout

```
lfg_core/            # unchanged domain library
lfg_service/         # promoted from webapp/server.py
  app.py             #   aiohttp app, route table
  auth.py            #   service-token + user-session auth
  identity.py        #   resolve()/link() over identities table
  sessions.py        #   mint/swap session registries
  events.py          #   EventBus protocol + InMemoryEventBus
  routes/            #   mint.py, swap.py, nfts.py, signin.py, config.py
  client/            #   browser frontend (moved from webapp/client)
surfaces/
  discord_bot/       # main.py refactored here, thin adapter
  telegram_bot/      # #43
  _client/           #   shared Python HTTP+WS client SDK
```

The **surface SDK** (`surfaces/_client/`) wraps REST+WS calls (auth headers, retry, event subscription) so Discord/Telegram/X don't each reimplement HTTP plumbing — the client-side analogue of `lfg_core`'s server-side sharing.

### 5.2 Discord migration (#53) — invert `main.py`

- **Delete** the parallel pipeline: `get_trait_files`, `get_random_trait`, `get_sorted_trait_layers`, `mint_nft_for_user`, `create_nft_offer`, local `TRAIT_LAYERS_DIR` compositing, `_rarity_pick_for_legacy`, and the static-payment / QR / subscription mint plumbing.
- **Keep & rewire** the Discord UI (`MintView`, `AdminView`, modals, slash commands). Button handlers now call the surface SDK (`client.start_mint(identity, …)`, poll `client.mint_status(id)`) and render returned state as embeds/QRs.
- `/register` → `client.register(identity, wallet)` → `identities`.
- Admin actions (burn, lookup, rarity) → admin-scoped service endpoints.
- Bot subscribes to `/events` for `mint.completed` / `mint.failed` → admin-log channel + optional user DM.
- After migration, `trait_layers/` is deleted from disk (closes #53's note).

This is a *path* change, not a behavioral one: Discord and web run the same `lfg_core` flows, so they mint identical art/rarity.

### 5.3 Telegram surface (#43)

New thin process on the surface SDK:
- Commands `/mint`, `/stats`, `/lookup`, `/register <wallet>` → REST (`identity = {platform:'telegram', platform_user_id: tg_user_id}`).
- Mint flow: Telegram can't embed XUMM, so it replies with the payment QR image (`/api/qr.png`) + deep link, then edits the message as it polls status.
- Subscribes to `/events` → posts `mint.completed` announcements to a configured channel / DMs the minter; the first consumer that exists *only* to react to events (validates the outbound bus).
- Admin alerts (failures) → private Telegram channel via a filtered event subscription.

## 6. Redis (deferred, interface-ready)

The event bus is defined as an `EventBus` protocol: `publish(event)` and `subscribe(filter) -> async iterator`. The shipped implementation is `InMemoryEventBus`. **Redis Streams** is a documented drop-in second implementation behind the same interface — no surface code changes when swapped.

Explicit triggers to make the swap:
1. Notification **replay / guaranteed delivery** is required (subscriber reconnects must catch missed events), or
2. The service runs **more than one replica** (in-memory bus/sessions break across replicas).

Session storage stays in-process: `MintSession`/`SwapSession` hold live `asyncio` tasks and aren't cleanly serializable; mint sessions live minutes and XRPL/XUMM are the source of truth, so a restart mid-mint just means a re-mint — not worth externalizing now.

## 7. Error Handling

- Surface SDK treats the service as fallible: timeouts, retries with backoff (reuse `RETRY_MAX_ATTEMPTS` / `RETRY_BASE_DELAY`), user-visible "try again" on persistent failure.
- Service endpoints return `{error, code}`; surfaces map codes to friendly messages per platform.
- WS reconnects with backoff; on reconnect the surface resumes (events are at-most-once today; durability is the Redis-later upgrade).

## 8. Testing

- `lfg_core` keeps its existing tests (unchanged domain).
- **Service route tests** with a fake `lfg_core`: assert service-token auth, identity resolution, and session lifecycle.
- **`EventBus` contract tests** (publish / subscribe / filter) that the future Redis impl must also pass.
- **`/events/me` scoping test**: a user-session WS receives only its own user's events and never another user's (security-critical filter).
- **Surface SDK tests** against a mock service (auth headers, retry, event subscription).
- **Smoke test per surface**; existing `webapp/test_smoke.py` migrates to `lfg_service`.
- **Identity-migration test**: `Users` → `identities` backfill is idempotent and preserves wallets.

## 9. Decomposition & Sequencing

This spec is the **spine + two clients**. Suggested implementation order:

1. **Service spine** — promote `webapp/server.py` → `lfg_service/`; add `auth.py`, `identity.py` (+ migration), `events.py` (`EventBus` + in-memory), `/api/session`, `/events` (service-token) and `/events/me` (user-scoped); switch the browser client from polling to `/events/me` with polling fallback. Web client keeps working throughout.
2. **Surface SDK** — `surfaces/_client/` with REST+WS + retry, plus tests against a mock service.
3. **Discord migration (#53)** — invert `main.py` onto the SDK; subscribe to events; delete the parallel pipeline and `trait_layers/`.
4. **Telegram surface (#43)** — new process on the SDK; first event-bus consumer.

Downstream specs (own cycles): **#42 Web UI** (richer browser frontend on the same API), **#41 X integration** (event consumer + `x.*` events + OAuth2 posting).
