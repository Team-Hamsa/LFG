# Xaman-Verified `/register` for the Discord & Telegram Bots — Design

**Date:** 2026-06-26
**Status:** Approved — ready for plan
**Context:** The bot `/register <wallet>` commands (Discord + Telegram) store a user-supplied address after only a **format check** (`is_valid_classic_address`) — no proof the user controls it. The webapp already verifies ownership via a XUMM (Xaman) sign-in. This brings that same proof-of-ownership to both bots.

## 1. Goal

Replace the unverified manual `/register <wallet>` on **both** bots with a **Xaman sign-in flow**: the user runs `/register` (no argument), scans a QR in Xaman, signs, and the **verified** wallet address is recorded as their registered (NFT-receiving) wallet. The manual address-string path is removed from the bots.

**User decisions (2026-06-26):**
- **Replace the manual path entirely** — `/register` takes no wallet argument; sign-in is the only registration path on the bots.
- **Keep** `POST /api/register` + `client.register()` — the endpoint and SDK method are retained (webapp / admin / future use); the bots simply stop calling them.
- The discord-only sign-in gate is replaced by **`(platform, id)`-keyed sign-in payloads** (the correct cross-surface-isolated form).

## 2. The existing sign-in contract (reused, not rebuilt)

The service already implements the whole verification flow — the bots only need to drive it:

- **`POST /api/signin`** (`handle_signin_start`, `@require_auth`) → creates a XUMM SignIn payload via `xumm_ops.create_signin_payload`, returns `{uuid, signin_link}`, and stores a pending record in the in-memory `signin_payloads` dict.
- **`GET /api/signin/{uuid}`** (`handle_signin_status`) → polls XUMM and returns `{"state": "pending"|"opened"|"signed"|"expired", "wallet"?}`. **On `signed`** it captures `s["account"]` (the proven address) and itself does `identity_store.link(...)` (+ legacy `register_user` for Discord), deletes the payload, and returns `{"state":"signed","wallet":<addr>}`.
- SDK already exposes `signin_start(user_id)` and `signin_status(user_id, uuid)`.

Because the **service** performs the storage on `signed`, the bot never calls `register()` — it only starts the payload, shows the QR, and polls to a terminal state.

## 3. Service changes — platform-aware sign-in

My PR-B fix gated `/api/signin` to discord-only (the payload `rec` carried no platform, so a bare-id ownership check was a cross-surface hole). Replace that gate with platform-keyed payloads:

- **`signin_payloads[uuid]`** record: `{platform, user_id, name, created_at}` — add `platform`, and rename `discord_id` → `user_id` since it now holds any platform's user-id. (Unlike `MintSession.discord_id`, which was kept to avoid churn across many call sites, `signin_payloads` is a private in-memory dict with no external consumer, so the rename is cheap and clearer.)
- **`handle_signin_start`:** drop the `_platform(user) != "discord"` gate; store `platform=_platform(user)`, `user_id=user["id"]`.
- **`handle_signin_status`:** ownership check becomes `not rec or rec["user_id"] != request["user"]["id"] or rec["platform"] != _platform(request["user"])` (drops the `!= "discord"` clause). On `signed`: `identity_store.link(rec["platform"], rec["user_id"], rec["name"], wallet)`; the **legacy `register_user` write stays gated to `rec["platform"] == "discord"`** (preserves the Greptile P1 fix — only Discord writes the legacy `Users` table; other platforms live in `identities`).

**Isolation preserved:** `telegram:55` and `discord:55` get distinct payloads keyed by `(platform, id)`; neither can read or complete the other's. **Webapp unaffected:** its tokens default to `platform="discord"`, so its records and behavior are byte-identical.

## 4. SDK — `wait_for_signin`

Add a terminal-state poller mirroring `wait_for_mint`:
- `SIGNIN_TERMINAL: frozenset[str] = frozenset({"signed", "expired"})`.
- `async wait_for_signin(self, user_id, uuid, *, interval=2.0, timeout=180.0, sleep=asyncio.sleep)` → reuses the existing `_poll(lambda: self.signin_status(user_id, uuid), SIGNIN_TERMINAL, …)`. On timeout it returns the last (non-terminal) status, which the caller treats like "expired".

## 5. Bot flow — both surfaces (mirrors the mint flow)

`/register` (no argument):
```
/register
  → svc.signin_start(user_id)              # {uuid, signin_link}
  → svc.qr_png(signin_link)                # render the XUMM deeplink as a QR
       send: QR + "Scan with Xaman to verify and register your wallet."
  → svc.wait_for_signin(user_id, uuid)
       state == "signed"  → "✅ Wallet verified and registered: <wallet>"
       state == "expired" → "⚠️ Sign-in expired — run /register again."
       (poll timed out, non-terminal) → same expired-style message
  (ServiceError at any step) → friendly_error(e)   # reuse surfaces/_shared
```

- **Telegram** (`surfaces/telegram_bot/`): a `handle_register(svc, update, context)` coroutine (new `register_view.py`, paralleling `mint_view.py`): `bot.send_photo(render.photo_input(qr, "signin_qr.png"), caption=…)` then the outcome message. The command handler loses its `context.args` wallet handling.
- **Discord** (`surfaces/discord_bot/`): a `handle_register(svc, interaction)` coroutine: `interaction.response.defer(ephemeral=True)` → followup with a sign-in embed + QR `discord.File` → outcome embed. Replaces the body of the current `_register_impl`; the `/register` command drops its `wallet` parameter.
- **Shared:** reuse `surfaces/_shared/mint_result.friendly_error` for `ServiceError` mapping, and add a small `surfaces/_shared/signin_result.py` with a `signin_outcome(state: str) -> str` map (signed / expired / fallback) so both bots render identical wording — same extract-don't-duplicate pattern as `mint_result` (each surface still wraps the string in its own embed/caption).

## 6. Removed / unchanged

**Removed (bots only):** the `wallet` argument on both `/register` commands and the bots' `client.register(...)` calls.
**Kept:** `POST /api/register`, `client.register()`, the manual `is_valid_classic_address` path (now only reachable via the webapp/admin, not the bots).
**Unchanged:** the mint payment flow; the webapp's sign-in UX (already Xaman-verified); all existing registrations (no migration — `identity_store.link` / `register_user` upsert, so re-running `/register` simply re-verifies and updates).

## 7. Error handling

- Service unreachable / XUMM down at `signin_start` → `502`/`ServiceError` → friendly "couldn't reach the sign-in service, try again."
- `signed` but address somehow invalid → the service already guards with `is_valid_classic_address`; treated as non-signed (stays pending → eventually expires).
- Poll timeout → expired-style message; the payload self-prunes server-side (`_prune_signin_payloads`, `SIGNIN_TTL`).
- One in-flight sign-in per user is naturally bounded by payload expiry; no one-active-session guard is required (sign-in is idempotent — a second `/register` just creates a fresh payload).

## 8. Testing

- **Service:** `signin_start`/`signin_status` under both `telegram` and `discord` platform tokens; **cross-platform isolation** (a `telegram` token gets `404` on a `discord`-created payload and vice-versa); `signed` links under the rec's platform; **legacy `register_user` only fires for `discord`**; webapp regression (default `discord` path unchanged); the discord-only-gate tests from PR B are replaced by platform-keyed equivalents.
- **SDK:** `wait_for_signin` returns on `signed`/`expired`, keeps polling on `pending`/`opened`, returns last status on timeout (sync fake clock, like the mint poller tests).
- **Bots (each surface):** `/register` happy path (signin_start → QR sent → `wait_for_signin` signed → success message with the wallet); expired branch; `ServiceError` at `signin_start` → friendly message. Repo-native sync tests with fake `svc`/`update`/`interaction` (mirrors the mint-handler tests).

## 9. Out of scope

- Changing how minting proves payment (already Xaman-signed).
- A separate "change/disconnect wallet" command (re-running `/register` covers change).
- Webapp UI changes.
- Rate-limiting sign-in attempts beyond the existing payload TTL.
