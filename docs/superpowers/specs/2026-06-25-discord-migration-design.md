# Discord Migration — Design Spec (Spine Plan 3 of 4)

**Date:** 2026-06-25
**Status:** Approved design (decisions D1–D5 answered by the user 2026-06-25), pre-implementation.
**Issue:** #53 (migrate `main.py` onto the shared spine). Addresses the SourceTag/divergence root cause behind #75/#61/#57.
**Depends on:** PR #76 (Plan 1, `lfg_service`) + PR #77 (Plan 2, Surface SDK) — Plan 3 is stacked on `feat/spine-plan2-surface-sdk` and cannot merge until both land.
**Parent spec:** `docs/superpowers/specs/2026-06-17-shared-services-spine-design.md` §5.2.
**Supersedes:** the `…-discord-migration-DRAFT.md` decisions doc.

## 1. Problem & Goal

`main.py` (1,900 lines) **owns** a parallel mint pipeline: it builds XUMM payment requests, polls payment, selects traits from local `trait_layers/` test art, composites with FFmpeg, uploads to BunnyCDN, mints, creates the offer, and renders the accept QR — none of it going through `lfg_core`. The web Activity already runs the canonical pipeline behind `lfg_service`. Result: two code paths that can mint divergent art/rarity, and the bot's inline XRPL txns lack the hackathon SourceTag (#75).

**Goal:** invert the bot's **user-facing mint/register path** so it *calls* `lfg_service` through the Plan 2 Surface SDK and renders results as Discord embeds/QRs. Discord and web then mint **identical art/rarity** from one pipeline, and the bot's mint txns inherit the service's SourceTag stamping.

## 2. Locked Decisions (D1–D5)

- **D1 = B — Admin stays local this plan.** Only the user mint/register path is inverted. `/admin` (stats, lookup, burn, rarity odds/boost/toggle) keeps calling `lfg_core`/`db_helpers` in-process, unchanged. Service-ifying admin is a deferred **Plan 3b** (post-mainnet).
- **D2 = A — Trustline stays bot-local.** The "Set LFGO Trustline" button keeps its bot-local XUMM `TrustSet` flow (it is not part of the mint pipeline). Must carry SourceTag `2606160021`.
- **D3 = yes — Layout.** `main.py` → `surfaces/discord_bot/` refactored into a thin adapter (slash-command tree + views + an `LFGServiceClient`), staying a **separate pm2 process** (`lfg-bot`); a launch shim preserves the existing pm2 entrypoint if the module path changes.
- **D4 = A — Completion signal.** The interactive per-user mint response uses `client.wait_for_mint(...)` polling (the user is actively waiting on the Discord interaction). A separate background `client.events(...)` subscription drives the **admin-log channel** announcement + optional minter DM.
- **D5 = keep `trait_layers/` (defer deletion).** Per the user's latency note, do **not** delete `trait_layers/` in this plan. Still **verify** the SourceTag `2606160021` invariant holds end-to-end through the service for every tx the bot now triggers (mint, offer, accept).

## 3. Architecture

```
surfaces/
  _client/            # Plan 2 SDK (LFGServiceClient) — consumed, unchanged
  discord_bot/        # NEW — the thin Discord adapter (was main.py)
    __init__.py
    bot.py            #   RetryBot + tree, on_ready, client lifecycle, events task
    config.py         #   env: DISCORD_BOT_TOKEN, LFG_SERVICE_URL, SERVICE_TOKEN_DISCORD, ADMIN_LOG_CHANNEL_ID, VIEW_TIMEOUT
    views.py          #   MintView (mint/trustline/buy), AdminView — rewired to the SDK / local admin
    mint_view.py      #   the inverted mint button handler (SDK calls + embed rendering)
    trustline.py      #   bot-local XUMM TrustSet helper + status poll (D2=A), SourceTag-correct
    admin.py          #   AdminView + modals — KEPT calling lfg_core/db_helpers locally (D1=B)
    render.py         #   embed/QR builders shared across handlers
main.py               #   launch shim -> surfaces.discord_bot.bot:main  (pm2 entrypoint unchanged)
```

The bot constructs **one** `LFGServiceClient(LFG_SERVICE_URL, SERVICE_TOKEN_DISCORD, "discord")` at startup, shared across handlers, closed on shutdown. Every user-scoped call passes the Discord user id; the SDK mints + caches that user's service session (the service resolves the wallet via `identities`/`Users`, which `/register` dual-wrote).

> **Decomposition note.** Splitting the 1,900-line `main.py` into the focused modules above is part of this plan (the file has long outgrown one responsibility). The split is mechanical relocation of *kept* code (admin, trustline, views) plus the inverted mint handler — not a rewrite of admin/trustline behavior.

## 4. Command-by-command migration

### 4.1 `/register <wallet>` → `client.register`
Replace the direct `register_user(...)` call with `await client.register(discord_id, discord_name, wallet)`. The service performs the `identities` + `Users` dual-write (Plan 1). On `ServiceError`, render the existing failure message.

### 4.2 `/letsgo` Mint button → invert onto the SDK
**Delete** the entire inline pipeline inside `mint_button` (trait select, FFmpeg composite, CDN upload, `mint_nft_for_user`, `record_nft_mint`, `create_nft_offer`, `generate_xumm_qr`, payment build/poll). Replace with:
1. `session = await client.start_mint(discord_id)` → returns `{session_id, …, payment data}`.
2. Render the **payment** embed/QR from the returned payment data (server-rendered QR via `client.qr_png(d=…)` or the returned `payment_link`).
3. `final = await client.wait_for_mint(discord_id, session["session_id"])` (bounded poll; the SDK handles backoff).
4. On terminal `offer_ready`/`done`: render the **offer-accept** embed/QR from `final` (accept link/QR + NFT image + number). On `failed`/`payment_timeout`: render the matching failure embed.
Map `ServiceError` codes (e.g. `BadRequest` "no wallet" → "register first"; a 409 "mint already in progress" → friendly note) to the existing message styles.

### 4.3 Trustline button (kept, D2=A)
Move `create_trustline_request` + the bounded XUMM status-poll loop into `surfaces/discord_bot/trustline.py` unchanged in behavior. **Verify** the `TrustSet` payload carries `SourceTag = 2606160021` (add it if missing — that is itself part of the #75 fix).

### 4.4 `/admin` (kept local, D1=B)
`AdminView` + `BurnNFTModal`/`NFTLookupModal`/`RarityOddsModal`/`RarityBoostModal`/`RarityDisableModal` relocate to `surfaces/discord_bot/admin.py` and keep calling `lfg_core`/`db_helpers`/`rarity` directly. No behavior change. (Burn still uses `lfg_core` issuer-burn, which already stamps SourceTag.)

### 4.5 Events → admin-log + DM (D4)
At startup the bot launches a **cancellable background task** running `async for ev in client.events(types=["mint.completed","mint.failed"]): …` → post to `ADMIN_LOG_CHANNEL_ID` and optionally DM the minter (resolve Discord id from `ev.identity`). The task is `aclose()`-d / cancelled on shutdown (per the SDK lifecycle contract). This is additive to the interactive `wait_for_mint` path, not a replacement.

## 5. Deleted vs. kept

**Deleted (parallel mint pipeline):** `mint_nft_for_user`, `create_nft_offer`, `create_payment_request[_static]`, `wait_for_payment_via_subscription`, `generate_xumm_qr`, `check_payment_status`, `get_sorted_trait_layers`/`get_random_trait`/`get_trait_files`, the inline FFmpeg/CDN/`record_nft_mint` block, `_rarity_pick_for_legacy` (if present), and `TRAIT_LAYERS_DIR` compositing usage.
**Kept:** all Discord UI (`MintView`, `AdminView`, modals, slash commands), the trustline flow, the admin flow, `RetryBot`, logging, `safe_followup`.
**NOT deleted (D5):** `trait_layers/` on disk stays.

## 6. Auth & identity wiring

- Bot holds `SERVICE_TOKEN_DISCORD` (new env) + `LFG_SERVICE_URL`. The service must be configured with the matching `SERVICE_TOKEN_DISCORD` (Plan 1 `auth.py`).
- `platform = "discord"`, `platform_user_id = str(discord_id)`. The SDK mints the per-user service session automatically; the bot never handles session tokens.
- `/register` dual-writes so the service's `require_wallet` (`get_user`) and `identities` both resolve the bot user's wallet.

## 7. SourceTag verification (D5)

Add a test/asserted check that the txns the bot now triggers through the service all carry `SourceTag = 2606160021`: the service path uses `lfg_core.xrpl_ops`/`xumm_ops`, which already stamp it — the verification confirms the bot no longer builds any unstamped inline tx, and that the kept trustline `TrustSet` stamps it too.

## 8. Testing

- **Adapter unit tests** with a **mocked `LFGServiceClient`** (an in-test fake exposing `register`/`start_mint`/`wait_for_mint`/`qr_png`/`events`): assert each command/button issues the right SDK calls with the right args, and maps returned state / `ServiceError` codes to the right embed. Repo-native sync style.
- **Trustline:** keep/port existing behavior; assert SourceTag on the built payload.
- **Admin:** unchanged code keeps its current coverage.
- **Manual testnet E2E** (post-merge of #76/#77): `/register`, `/letsgo` mint end-to-end, trustline, one admin op — confirm Discord mints art **identical** to the web Activity.

## 9. Out of scope / deferred

- **Plan 3b (post-mainnet):** service-ify the `/admin` panel + trustline (D1-A/D2-B) for full unification.
- **`trait_layers/` deletion** (D5) — deferred.
- **Telegram (#43)** — Plan 4.

## 10. Decomposition & sequencing

1. Scaffold `surfaces/discord_bot/` package + `config.py` + the launch shim; move `RetryBot`/`on_ready`/bot bootstrap; construct the shared `LFGServiceClient`.
2. Relocate admin (unchanged) + trustline (SourceTag-verified) into the package.
3. `/register` → `client.register`.
4. Invert the mint button onto `start_mint`/`wait_for_mint` + render; delete the parallel pipeline functions.
5. Events background task → admin-log + DM.
6. SourceTag verification test + adapter test suite; pm2 shim verified.
