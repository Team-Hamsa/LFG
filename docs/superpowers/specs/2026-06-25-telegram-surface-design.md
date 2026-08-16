# Telegram Surface — Design (Spine Plan 4 of 4)

**Issue:** [#43](https://github.com/Team-Hamsa/LFG/issues/43)
**Date:** 2026-06-25
**Status:** Approved — ready for plan
**Predecessors:** Plan 1 service spine (#76), Plan 2 Surface SDK (#78), Plan 3 Discord migration (#79) — all merged to `main`.

## 1. Goal

Add Telegram as a first-class LFG surface with **full parity to the user-facing
Discord flow**: interactive minting, wallet registration, mint announcements to a
channel, and a DM to the minter on completion. Telegram rides the same
`lfg_service` backend through the Surface SDK (`LFGServiceClient`) — no parallel
mint/compose/rarity logic.

This is the last of the four shared-services-spine plans, completing the
"every surface on one backend" goal ahead of the **2026-07-21 mainnet launch**.

### Scope boundaries (locked with the user)

- **User-facing only.** Admin (burn / stats / lookup) stays **Discord-only**.
  Telegram has no guild-permission model; admin parity is explicitly out of scope.
- **Library:** `python-telegram-bot` (PTB) v21+ — fully async, maps cleanly onto
  the discord.py handler/command patterns the team already uses.
- **Webapp untouched.**

## 2. Two-part delivery (two PRs)

The work splits into a service prerequisite and the adapter. **They ship as two
separate PRs**, Part A first.

### Part A — Service platform-awareness (precursor PR)

**Problem.** The HMAC session token carries only `{id, name, exp}`
(`lfg_service/app.py` `make_session_token`). Identity resolution and event
publishing hardcode the string `"discord"` in six places, so a Telegram user's
wallet would never resolve and their mints would be announced as Discord:

- `handle_events_me` → `identity_store.resolve("discord", …)`
- `handle_me` → `identity_store.resolve("discord", …)`
- `handle_register` → `identity_store.link("discord", …)`
- `handle_signin_status` → `identity_store.link("discord", …)` (webapp-only Xaman
  path; Telegram never hits it — converting it is consistency-only and stays
  `discord` via the default)
- mint `publish_event` → `{"platform": "discord", "platform_user_id": …}`

The platform is already known at session-creation time: `require_service_token`
stashes `request["surface"]` (`lfg_service/auth.py:43`) derived from the
`SERVICE_TOKEN_<SURFACE>` env var. It simply isn't propagated past session
creation.

**Fix — thread `platform` through the session token (one stateless source of truth):**

1. `make_session_token(user)` stamps `platform` into the token payload, defaulting
   to `"discord"` when absent.
2. `handle_session` (service-token-authed) passes `platform=request["surface"]`.
   Discord bot → `discord`, Telegram bot → `telegram`.
3. `handle_token` (webapp Discord-OAuth path) is unchanged — it keeps the
   `"discord"` default because that login genuinely *is* a Discord identity.
4. A `_platform(user)` helper (`user.get("platform", "discord")`) replaces the
   hardcoded `"discord"` in the identity/publish sites above. The
   **required** sites for Telegram correctness are `handle_me`, `handle_register`,
   and the mint `publish_event` (Telegram calls these with a `telegram` token);
   `handle_events_me` and `handle_signin_status` are converted for consistency and
   remain `discord` for the webapp via the default.
5. `MintSession` gains a `platform` field, set in `handle_mint_start` from
   `_platform(user)`. The mint `publish_event` emits
   `{"platform": session.platform, "platform_user_id": session.discord_id}`.
   Session-ownership checks compare `(platform, id)` rather than `id` alone.

**Decisions:**
- The `MintSession.discord_id` **field name is kept as-is** (it is simply "the
  platform user-id"). Renaming it across all call sites is churn for no
  behavioral gain; sessions key on a UUID, so cross-platform id collision is a
  non-issue. The ownership check is hardened to also compare platform.
- **Backward-compatible by construction.** Every default is `"discord"`, so the
  Discord surface and the Discord-OAuth webapp behave byte-for-byte as before. A
  regression test asserts this.

### Part B — `surfaces/telegram_bot/` adapter (depends on Part A)

A package mirroring `surfaces/discord_bot/`, built on the Surface SDK:

| Module | Responsibility |
|--------|----------------|
| `config.py` | Env: `TELEGRAM_BOT_TOKEN`, `LFG_SERVICE_URL`, `SERVICE_TOKEN_TELEGRAM`, `TELEGRAM_ANNOUNCE_CHAT_ID`. `SERVICE_TOKEN_TELEGRAM` auto-registers the surface — **no service auth code change**. |
| `bot.py` | PTB v21 `Application` lifecycle. Builds one shared `LFGServiceClient`. Registers handlers. Starts the events task in `post_init`; cancels + `aclose()`s it **before** `svc.close()` in `post_shutdown` (mirrors Discord cleanup ordering). |
| `commands.py` | `/mint`, `/register <wallet>`, `/start` / `/help`. |
| `mint_view.py` | `handle_mint(svc, update, context)`: `start_mint` → send `qr_png(payment_link)` as photo → `wait_for_mint(user_id, session["id"])` → on state in `{offer_ready, done}` send the offer QR (prefer hosted `accept_qr_url`, else `qr_png(accept_deeplink)`). Shared `_friendly(err)` error mapping. |
| `render.py` | Pure builders → caption text + photo bytes (`InputFile`). Telegram has no embeds; captions + inline keyboards replace them. |
| `events.py` | `run_event_loop(svc, announce, dm_user)` over `svc.events(types=["mint.completed","mint.failed"])`. Announces to `TELEGRAM_ANNOUNCE_CHAT_ID`; DMs the minter on `mint.completed`, **gated on `identity.platform == "telegram"`** (the `/events` firehose is cross-surface — same pattern as Discord's `_is_discord`). |

**Identity.** `/register <wallet>` → `svc.register(wallet)` → service links
`identities("telegram", telegram_user_id)` via Part A. Wallet resolution,
`/events/me`, and mint all then use the token's `telegram` platform.

**SourceTag (Make Waves `2606160021`).** The Telegram surface builds **zero**
inline XRPL transactions — no trustline button, no admin burn (both Discord-only).
All minting flows through `lfg_service`, which already stamps the tag via
`lfg_core.xrpl_ops` / `xumm_ops`. The surface is SourceTag-clean by construction;
a parity test asserts no un-stamped inline tx exist in the package.

## 3. Data flow (interactive mint)

```
TG /mint
  → handle_mint(svc, update, ctx)
  → svc.start_mint(tg_user_id)            # SDK creates/repays service session
  → svc.qr_png(payment_link)              # photo: pay 1 token
  → svc.wait_for_mint(tg_user_id, sid)    # SDK polls service status
      → state in {offer_ready, done}
  → svc.qr_png(accept_deeplink) | accept_qr_url   # photo: accept NFT offer
  (meanwhile) lfg_service publishes mint.completed {platform: "telegram", ...}
  → events.run_event_loop announces to channel + DMs minter
```

## 4. Deployment

- New pm2 process `lfg-telegram` → `python -m surfaces.telegram_bot.bot`
  (no launch shim needed — module entry point).
- `python-telegram-bot>=21` added to `requirements.txt`.
- New env: `TELEGRAM_BOT_TOKEN`, `SERVICE_TOKEN_TELEGRAM`,
  `TELEGRAM_ANNOUNCE_CHAT_ID`. Documented in `CLAUDE.md`.

## 5. Testing (repo-native sync style — `new_event_loop`/direct-call, not pytest-asyncio)

**Part A (precursor PR):**
- session token carries and round-trips `platform`; absence defaults to `discord`.
- `handle_me` / `handle_register` / `handle_signin_status` / `handle_events_me`
  honor a non-discord platform (resolve/link under the right namespace).
- mint `publish_event` emits the session's real platform.
- **Discord regression:** with no platform supplied, every path stays `discord`
  (byte-identical behavior).

**Part B (adapter PR):**
- `handle_mint` happy path + each `_friendly` error branch.
- `events.run_event_loop`: announces + DMs on `telegram` `mint.completed`;
  announces-only (no DM) on `mint.failed` and on non-telegram events; `aclose()`
  runs in `finally`; survives a handler error and continues.
- `/register` maps to `svc.register` and surfaces `ServiceError` cleanly.
- SourceTag-clean invariant: no inline XRPL tx in the Telegram package.

## 6. Out of scope (explicit)

- Telegram admin (burn / stats / lookup) — stays Discord-only.
- Telegram trustline UX — minting handles trustline server-side via the service;
  no inline TrustSet on this surface.
- Webapp / Discord behavior changes beyond the backward-compatible Part A default.

## 7. Risks & mitigations

- **Risk:** Part A touches shared identity/publish code used by Discord + webapp.
  **Mitigation:** every change defaults to `discord`; a regression test pins the
  unchanged behavior; Part A ships as its own reviewed PR before the adapter.
- **Risk:** PTB lifecycle differs from discord.py (post_init/post_shutdown vs
  setup_hook/close). **Mitigation:** the events-task cancel-before-close ordering
  is mirrored explicitly and unit-tested via the same fake-svc pattern as Discord.
