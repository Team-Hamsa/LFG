# Discord Migration (Spine Plan 3 of 4) — Design-Decisions DRAFT

**Status:** ⚠️ DRAFT for review — NOT an approved spec. Produced autonomously overnight (2026-06-25) to tee up the decisions a real brainstorm needs. Do not implement from this; it exists so the open decisions below can be answered quickly, then promoted to a proper spec.
**Issue:** #53 (migrate `main.py` onto the shared spine). Closes tagging issues #75/#61/#57 per the initiative map.
**Depends on:** PR #76 (Plan 1, `lfg_service`) + PR #77 (Plan 2, Surface SDK) — both open/unmerged. Plan 3 cannot land until both do.
**Parent spec:** `docs/superpowers/specs/2026-06-17-shared-services-spine-design.md` §5.2.

---

## 1. Goal (unchanged from parent §5.2)

Invert `main.py` (1,900 lines): today it **owns** a parallel mint/compose/rarity pipeline and composites from local `trait_layers/` test art; afterward it **calls** the shared `lfg_service` (via the Plan 2 SDK) and renders results as Discord embeds/QRs. Net effect: **Discord and web mint identical art/rarity** from one `lfg_core` pipeline, and the duplicate Discord pipeline is deleted.

The Discord bot is already a separate pm2 process (`lfg-bot`) that does **not** consume the service today; this plan makes it a thin SDK client.

## 2. Current `main.py` inventory

**Slash commands:** `/letsgo` (mint UI), `/register <wallet>`, `/admin` (admin panel).

**Views / modals (Discord UI to KEEP & rewire):**
- `MintView` — Mint NFT button, Set-LFGO-Trustline button, Buy-Token link.
- `AdminView` — stats, lookup, burn, rarity odds/boost/toggle buttons.
- Modals: `BurnNFTModal`, `NFTLookupModal`, `RarityOddsModal`, `RarityBoostModal`, `RarityDisableModal`.

**Parallel pipeline to DELETE (parent §5.2):** `get_trait_files`, `get_random_trait`, `get_sorted_trait_layers`, `mint_nft_for_user` (main.py:347), `create_nft_offer` (449), `create_payment_request[_static]` (530/720), `wait_for_payment_via_subscription` (564), `generate_xumm_qr` (688), `check_payment_status` (728), local `trait_layers/` compositing, `_rarity_pick_for_legacy`, and the static-payment / QR-to-CDN / subscription plumbing inside `mint_button` (812+).

## 3. SDK coverage map — what maps cleanly vs. what's missing

| Discord feature | Service/SDK today? | Mapping |
|---|---|---|
| `/register <wallet>` | ✅ `client.register(discord_id, name, wallet)` | direct; service dual-writes `identities` + `Users` (Plan 1) |
| Mint button → payment → mint → offer | ✅ `client.start_mint` / `wait_for_mint` / `qr_png` | the whole flow is the `mint_flow` state machine behind the service |
| Mint completion announce (admin-log/DM) | ✅ `client.events(types=["mint.completed","mint.failed"])` | the reconnecting firehose Plan 2 ships |
| **Set-LFGO-Trustline button** | ❌ no service/SDK endpoint | `lfg_core.xrpl_ops`/`xumm_ops` have the pieces, but nothing is exposed |
| **`/admin`: stats / lookup** | ❌ no endpoint | reads `LFG`/`onchain` DB + `db_helpers` today |
| **`/admin`: burn** | ❌ no endpoint | `lfg_core` can burn (`xrpl_ops`); not service-exposed |
| **`/admin`: rarity odds/boost/toggle** | ❌ no endpoint | `lfg_core.rarity` exists; not service-exposed |

The user-facing **mint + register + completion** path is fully covered by Plan 1+2. The **trustline button** and the **entire `/admin` panel** have no service/SDK equivalent — this is the crux of the scope decision.

## 4. OPEN DECISIONS (need your answers before this becomes a spec)

### D1 — Admin panel scope (the big one)
The `/admin` panel (stats, lookup, burn, rarity boost/toggle) has no service surface. Two paths:
- **(A) Service-ify admin now.** Add admin-scoped endpoints (`/api/admin/stats`, `/api/admin/nft/{n}`, `/api/admin/burn`, `/api/admin/rarity/*`) gated by a service-token+admin scope, plus SDK methods, then the bot calls them. Matches parent §5.2's "admin → admin-scoped service endpoints," fully unifies, but is a large scope expansion (new server endpoints + SDK + tests on top of the bot rewire).
- **(B) ⭐ Recommended: migrate the user path now, keep admin local this plan.** Plan 3 inverts only the mint/register/events path (the value: identical art Discord↔web). The `/admin` panel keeps calling `lfg_core`/`db_helpers` directly in-process for now (it already does, and it's admin-only/low-traffic). Service-ify admin in a focused **Plan 3b** after mainnet. Smaller, faster, unblocks the 7/21 goal.

### D2 — Trustline button
No service endpoint. Options:
- **(A) ⭐ Recommended: keep a minimal bot-local XUMM TrustSet helper** (it's a single `TrustSet` payload via `lfg_core.xumm_ops`; SourceTag `2606160021` required). Low-risk, keeps the migration focused.
- **(B) Add `/api/trustline` to the service + SDK** (cleaner long-term; more scope). Fits if D1=A.

### D3 — Repo layout / process model
Parent §5.1 puts the bot at `surfaces/discord_bot/`. Confirm: relocate `main.py` → `surfaces/discord_bot/` refactored into a thin adapter (slash-command cog + views + an `LFGServiceClient`), **staying a separate pm2 process** (`lfg-bot`, unchanged launch). Recommended: yes, move + refactor in place; keep the pm2 process and add a launch shim if the entrypoint path changes (mirrors Plan 1's `webapp/server.py` shim).

### D4 — Mint completion signal: poll vs. events
- **(A) ⭐ Recommended: `wait_for_mint` polling for the interactive per-user response** (the user is actively waiting on the Discord interaction; polling the status the bot already started is simplest and self-contained), **plus** an `events()` subscription used only to drive the **admin-log channel** announcement + optional user DM. Best of both.
- **(B) Pure events** for everything (more moving parts in the interactive path).

### D5 — Cleanup
After migration, delete `trait_layers/` from disk (closes #53's note) and the parallel-pipeline functions. Confirm the **SourceTag `2606160021`** invariant is preserved end-to-end (it now lives in the service's `xrpl_ops`/`xumm_ops`, not the bot — verify the service sets it on every tx the bot triggers).

## 5. Proposed Plan 3 scope (assuming recommended answers B/A/yes/A/yes)

A focused inversion:
1. Scaffold `surfaces/discord_bot/` adapter + construct `LFGServiceClient` from `SERVICE_TOKEN_DISCORD` + `LFG_SERVICE_URL`.
2. `/register` → `client.register(...)`; delete `register_user` direct write from the bot path.
3. `/letsgo` Mint button → `client.start_mint(discord_id)` → render payment QR (from returned payment data / `qr_png`) → `wait_for_mint` → render offer-accept QR. Delete the parallel mint/compose/offer/payment plumbing.
4. Bot-local trustline helper retained (D2-A), SourceTag-correct.
5. `/admin` panel left calling `lfg_core`/`db_helpers` locally (D1-B) — untouched this plan.
6. Subscribe `client.events(...)` → admin-log channel announcements + optional minter DM (run as a cancellable background task; `aclose()` on shutdown per the SDK lifecycle note).
7. Delete `trait_layers/`; verify SourceTag invariant; pm2 launch shim if entrypoint moves.

Deferred to **Plan 3b** (post-mainnet): service-ify the admin panel + trustline (D1-A/D2-B) if full unification is wanted.

## 6. Testing approach (sketch)
- Bot adapter unit tests with a **mocked `LFGServiceClient`** (assert the right SDK calls per command/button; render-state mapping), repo-native sync style.
- Keep `/admin` local tests as-is (unchanged code).
- Manual E2E on testnet: `/register`, `/letsgo` mint end-to-end, trustline, an admin op — confirm Discord mints art identical to the web Activity.

---

### What I need from you (morning)
Answer **D1–D5** (recommendations marked ⭐). With those, I'll promote this to a proper spec + write the implementation plan. Until then this stays a DRAFT and `main.py` is untouched.
