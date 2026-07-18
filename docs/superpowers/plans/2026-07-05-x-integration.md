# X (Twitter) Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-tweet every successful mint from the brand X account (image + traits + XRPL link), add zero-OAuth "Share on X" buttons to the Activity, an OG card page, and an admin pause switch — closing the MVP scope of #41 while leaving per-user OAuth2 (phase 3) specced and gated.

**Architecture:** A new out-of-process firehose consumer `surfaces/x_bot/` (pm2 `lfg-x`) subscribes to `/events` exactly like the Telegram announce task and posts via aiohttp requests signed with `oauthlib` (OAuth 1.0a); dedup/budget/pause state lives in a poster-owned sqlite `x_state.db`. Web UI shares use X Web Intents (no API). Mint flow is untouched — posting can never block a mint.

**Tech Stack:** Python 3.10+, aiohttp (existing), `oauthlib` (NEW dep — OAuth 1.0a signing; hand-rolled RFC 5849 rejected per spec §2), sqlite3, vanilla JS webapp, pytest.

**Spec:** `docs/superpowers/specs/2026-07-05-x-integration-design.md`

## Global Constraints

- Every new test file importing `lfg_core` at module top MUST start with the env-guard preamble copied **verbatim** from `tests/test_seasons.py` lines 1–18 (`XUMM_API_KEY` … `BUNNY_PULL_ZONE`).
- All PRs open as **draft** (`gh pr create --draft`); CodeRabbit review before merge; ≤4 ready-for-review flips/hour. (superseded 2026-07-14: PRs open ready; CodeRabbit paid — see repo convention)
- No XRPL transactions in this plan ⇒ no SourceTag surface; if any task grows a tx path, `SourceTag = 2606160021` is mandatory.
- All `X_*` env vars optional: unset ⇒ feature off (house convention). Nothing in `lfg_service`/`lfg_core` may hard-require X creds.
- The poster must never raise across an event-loop iteration: one bad event is logged and skipped.
- Run `.venv/bin/python -m pytest` (repo venv). Pre-push runs ruff format.
- **PAUSE after Task 0:** the user reviews verified tier facts (posting caps, media endpoint) before PR-1 code is written; budget default and media path depend on them.

## File Structure

```
surfaces/x_bot/__init__.py     # NEW PR-1
surfaces/x_bot/bot.py          # NEW PR-1: entry — stream_events loop + pause/budget gate
surfaces/x_bot/poster.py       # NEW PR-1: pure event→tweet-text composition + dedup keying
surfaces/x_bot/x_api.py        # NEW PR-1: oauthlib-signed requests, post_tweet(), upload_media() seam
surfaces/x_bot/state.py        # NEW PR-1: x_state.db (x_posts, settings) helpers
run_x.py                       # NEW PR-1: canonical-import shim (mirrors run_telegram.py)
lfg_core/config.py             # MOD PR-1: X_* vars, X_ENABLED composite flag
lfg_core/mint_flow.py          # MOD PR-1: add `traits` to MintSession.to_dict()
lfg_service/app.py             # MOD PR-2: POST /api/admin/x/pause (+resume/status)
surfaces/discord_bot/…admin…   # MOD PR-2: /admin X pause/resume button
webapp/server.py               # MOD PR-3: GET /nft/<number> OG card page
webapp/client/app.js           # MOD PR-3: Share-on-X buttons (mint + swap panels)
tests/test_x_poster.py         # NEW PR-1
tests/test_x_admin_toggle.py   # NEW PR-2
tests/test_og_page.py          # NEW PR-3
docs/…(this spec/plan)         # direct-to-main docs commit (trivial path)
```

---

## Task 0 — Verify X API assumptions (no code) — **human gate**

- [ ] Create/confirm the brand X account and a developer-portal app; generate OAuth 1.0a access token+secret for the brand account. → ops checklist on #41 (needs brand creds)
- [x] From developer.x.com and one live scripted test (throwaway tweet, then delete): record (a) current Free-tier monthly/daily write caps, (b) whether media upload works on our tier and via which endpoint (v1.1 `media/upload.json` vs `POST /2/media/upload`), (c) rate-limit headers returned.
- [ ] In the Activity (real Discord, not the dev harness): confirm external-link behavior (`window.open` / `<a target=_blank>`) AND log `location.origin` — expected to be the `*.discordsays.com` sandbox proxy, confirming the share URL must come from `PUBLIC_SHARE_BASE_URL` config, never the browser origin (spec §6.1). → ops checklist on #41 (needs brand creds)
- [x] Update spec §3 assumption rows to "verified (2026-07-…)" and set the real `X_MONTHLY_POST_BUDGET` default.
- [ ] **PAUSE — user reviews findings and approves budget + media path (or approves Basic-tier purchase, which changes nothing structurally).**

## PR-1 — `surfaces/x_bot` poster (brand-account auto-post)

- [x] `lfg_core/config.py`: add `X_CONSUMER_KEY/SECRET`, `X_ACCESS_TOKEN/SECRET`, `X_MONTHLY_POST_BUDGET` (int, default from Task 0), `SERVICE_TOKEN_X` (shipped per house pattern in surfaces/x_bot/config.py instead; auth.py auto-registers SERVICE_TOKEN_* env vars); `X_ENABLED` true only when flag set AND all four creds non-empty (ECONOMY_ENABLED style, config.py:135).
- [x] `lfg_core/mint_flow.py`: include `traits` (the dict built at mint_flow.py:286-297, stored on the session) in `to_dict()`; additive — assert existing consumers (webapp mint panel, telegram announce) unaffected.
- [x] Add `oauthlib` to `requirements.txt`. `surfaces/x_bot/x_api.py`: sign requests with `oauthlib.oauth1.Client` (HMAC-SHA1, header placement), send with aiohttp; `post_tweet(text, media_id=None)` + `upload_media(bytes) -> media_id` behind a seam so v1.1↔v2 endpoint choice is one function. Multipart uploads sign with the body EXCLUDED from the signature base string (RFC 5849 §3.4.1.3.1).
- [x] Signing tests in `tests/test_x_poster.py`: (a) RFC 5849 example vectors (the RFC §3.4.1.1 / errata base-string example with its fixed nonce/timestamp reproduces the documented signature through our signer wrapper); (b) one known-good X API signature fixture (captured from Task 0's successful live post: freeze its nonce/timestamp/params and assert our signer reproduces the exact header) (deferred until the first live post exists); (c) multipart case — signature base string contains no body params.
- [x] `surfaces/x_bot/poster.py` (pure, no I/O): `should_post(event) -> event_key|None` (success events only, mint.completed first), `compose(event) -> text` (traits summary ≤280 chars per A5, rarest-slots pick via `lfg_core/rarity.py`, bithomp URL from `config.XRPL_NETWORK`).
- [x] `surfaces/x_bot/state.py`: `x_state.db` (gitignore it) with `x_posts` + `settings` tables per spec §5.5/§5.6; `already_posted()`, `record()`, `month_count()`, `posting_paused()`. Budget month is computed in **UTC** (`datetime.now(timezone.utc).strftime("%Y-%m")`); `month_count()` takes an injectable clock so the month-boundary test is deterministic.
- [x] `surfaces/x_bot/bot.py` + `run_x.py`: stream `/events` via `surfaces/_client/events.py:stream_events` with `SERVICE_TOKEN_X`; per event: dedup → pause check → budget check → download image (BunnyCDN `image_url`) → upload media → post → record. Retries 1s/4s/16s on 5xx only; 429 records `failed` + global backoff to reset time. Startup: exit(0) with a clear log line when `X_ENABLED` is false; verify creds with a `users/me`-style call and fail loudly if bad.
- [x] `tests/test_x_poster.py` (env-guard preamble verbatim): compose truncation, should_post filtering (failures never posted), dedup on duplicate event, budget cutoff → `skipped_budget`, UTC month-boundary (post at 23:59Z vs 00:01Z lands in different months regardless of local TZ), 4xx-no-retry vs 5xx-retry (mock x_api).
- [x] Draft PR; CodeRabbit; merge.
- [ ] Ops (post-merge, user-run): add `X_*` + `SERVICE_TOKEN_X` to `.env`; register the token in the service's token map; `pm2 start run_x.py --name lfg-x --interpreter .venv/bin/python`; testnet mint → confirm tweet with image; check `x_posts` row. → ops checklist on #41 (needs brand creds)

## PR-2 — admin runtime toggle

- [x] `lfg_service/app.py`: `POST /api/admin/x/pause`, `POST /api/admin/x/resume`, `GET /api/admin/x/status` — authed via `require_service_token` (app.py:386 pattern) **plus a surface restriction: `surface_for_token(...)` must be `"discord"`; any other valid surface token (telegram, x) → 403** (spec §5.6 — the human-authorization gate is the Discord bot's existing administrator-permission check in front of the `/admin` button). Handlers write/read the `settings.posting_paused` row (service gets read/write access to `x_state.db` path via config; single-writer discipline documented — service writes only `settings`, poster writes only `x_posts`).
- [x] Discord `/admin` panel: "X posting: pause/resume" button + status line (mirrors existing admin actions in `surfaces/discord_bot`).
- [x] `tests/test_x_admin_toggle.py` (env-guard preamble): no token → 401; valid non-Discord surface token (telegram) → 403; Discord token → 200; pause→poster-gate behavior (unit-level via state.py).
- [x] Draft PR; CodeRabbit; merge; verify pause stops tweets within one event on testnet.

## PR-3 — webapp share buttons + OG card page

- [x] `webapp/server.py`: `GET /nft/{number}` server-rendered HTML — edition lookup (LFG table, fallback on-chain index), `twitter:card=summary_large_image`, `twitter:image=<image_url>`, `og:title/description` (name + top traits), link out to bithomp. 404s cleanly for unknown/burned editions.
- [x] `lfg_core/config.py` + `webapp/server.py`: optional `PUBLIC_SHARE_BASE_URL` (unset ⇒ empty), exposed to the client via the webapp's existing client-config delivery path. The client must NEVER use `location.origin` for share URLs (Activity runs on the `*.discordsays.com` sandbox proxy — spec §6.1).
- [x] `webapp/client/app.js`: "Share on X" on mint-success and swap-result panels building a Web Intent URL (spec §6.1): shared `url` = `PUBLIC_SHARE_BASE_URL + /nft/<number>` when configured, else the bithomp NFT page. Apply Task-0's verified iframe behavior: if `window.open` is blocked, render an `<a target="_blank">` and, failing that, a copyable link.
- [x] `tests/test_og_page.py` (env-guard preamble): meta tags present, 404 path, image URL escaping.
- [x] Draft PR; CodeRabbit; merge; visual check in the Activity (WEBAPP_DEV_MODE harness + real Discord).

## PR-4 (deferred, tracked, not built now) — per-user OAuth2 PKCE

Gated on Basic tier or verified Free-tier headroom AND public HTTPS callback (shared dependency with Mini-App #89 Part B). Scope per spec §7: `/api/x/connect` PKCE flow bound to wallet session, `x_accounts` table with Fernet-encrypted tokens (`cryptography` new dep, `X_TOKEN_ENC_KEY`), atomic refresh rotation, revoke-then-delete disconnect, "Share from my account" button falling back to Web Intent. Open a follow-up issue when phases 1–3 land and close #41 against phases 1–3 + that follow-up.

Filed as #252 (2026-07-17); #41 closed against PR-1 (#245), PR-2, PR-3 + #252. Bulk-mint event publishing tracked separately as #253.

## Verification (whole feature)

- [ ] Testnet mint end-to-end: tweet appears with image, traits, correct test.bithomp link; `x_posts` has one `posted` row; re-polling the mint session does not double-post.
- [ ] Kill `lfg-x` mid-mint: mint completes normally (fail-safe check).
- [ ] Budget forced to 0: mint completes, row is `skipped_budget`, admin status shows it.
- [ ] Full suite `.venv/bin/python -m pytest` green.
