# Tweet-to-Mint (X Integration) — Design Spec

**Issue:** Team-Hamsa/Mint-Bot#41
**Date:** 2026-06-13
**Status:** Design approved, pending spec review

## Overview

Let users mint LFG NFTs by tweeting a command at the project's X account. A user
links their X handle to their Xaman wallet once (in the web app). Afterward,
tweeting `@letseffinggo !mint` triggers a paid mint: the backend pushes a payment
request to the user's Xaman, runs the existing mint flow on payment, replies to
the tweet with the NFT image, and pushes an NFT-accept offer to Xaman.

This reuses the existing `lfg_core` mint pipeline (`mint_flow`, `xrpl_ops`,
`xumm_ops`) and the authenticated webapp session. The only new external surface is
the X API (read mentions, post replies) and Xaman **push** delivery (new — current
integration is QR/deep-link only).

### End-to-end flow

1. User links X account in the webapp (one-time).
2. User tweets `@letseffinggo !mint`.
3. Ingestion worker (polling, 30s) detects the mention, validates it.
4. Orchestrator pushes a Xaman payment payload to the user's wallet.
5. User signs payment in Xaman.
6. Mint executes (`mint_flow`): compose image → upload to CDN → `NFTokenMint`.
7. Bot replies to the original tweet with the NFT image (no URL).
8. Orchestrator creates an NFT offer and pushes an accept-offer payload to Xaman.
9. User accepts in Xaman; NFT lands in their wallet.

## X API economics (pay-per-usage)

Per https://docs.x.com/x-api/getting-started/pricing (fetched 2026-06-13):

- Reading **own** account mentions = "Owned Reads" at **$0.001/resource**.
- Reads **deduplicate within 24h UTC windows** — re-polling the same mention is free.
- Post creation = **$0.015/request**; **$0.200 if the post contains a URL**.
- Media upload = **$0.005/resource**.
- Configurable spending limits + auto-recharge thresholds (hard cost ceiling).

**Cost design consequences:**
- Replies contain **image only, no URL** → $0.015 + $0.005, not $0.20.
- The expensive write (reply) happens **only after an on-chain payment**, so the
  paywall bounds posting volume regardless of mention spam.
- Mention reads are the only spam-exposed cost ($0.001 each, deduped); contained
  by the X spending cap + the abuse controls below.

## Components

Five independently testable units.

### 1. X Link (webapp)
- Adds a "Connect X" action to the existing authenticated session (user already
  has Discord identity + registered wallet).
- **X OAuth2 (PKCE)** confirms handle ownership; we capture `x_user_id`, `x_handle`.
- In the same linking flow, run a Xaman **sign-in payload** and capture the
  returned `user_token` (required to push later payloads). If the user already has
  a recent `user_token`, reuse it.
- Persist the link (see Data). Re-linking updates the row.
- New route(s) in `webapp/server.py` following the existing OAuth/session pattern.

### 2. Mention Ingestion Worker
- Background async loop, **30s interval**, polling X API v2
  `GET /2/users/:id/mentions` for @letseffinggo, cursored by persisted `since_id`.
- For each new mention, in order, drop if any fail:
  - Feature flag disabled.
  - Author not in `x_links` (unlinked handle).
  - Author blacklisted (see abuse controls).
  - Text does not contain the strict command `!mint`.
  - `tweet_id` already present in `x_mint_sessions` (dedup).
- Surviving mentions create a new `x_mint_sessions` row (`pending_payment`) and are
  handed to the orchestrator.
- Advances `since_id` to the newest seen id only after durable persistence.

### 3. Social Mint Orchestrator
- Resumable, keyed by `tweet_id`. Drives the status machine:
  `pending_payment → paid → minted → replied` (+ `failed`, `expired`).
- Pushes Xaman **payment** payload via stored `user_token`; awaits signed status
  (reusing `xumm_ops` payload-status polling) up to a timeout.
- On payment confirmed → calls existing `mint_flow` (compose, upload, mint).
- On mint success → triggers X Poster (step 4) for the reply.
- Then creates the NFT offer (`xrpl_ops`) and pushes an **accept-offer** payload to
  Xaman.
- Forward-only transitions; idempotent per step so a restart resumes safely and
  never double-mints or double-replies.

### 4. X Poster
- Uploads the NFT PNG as media, posts a reply (`in_reply_to_tweet_id` = original
  tweet) with a caption (NFT number + trait summary), **no URL**.
- Bounded-backoff retry on transient failures; records the reply id on the session.

### 5. Admin toggle
- Feature flag (env var + admin UI control) gating the ingestion worker. When off,
  the worker idles (no polling spend) and no new sessions start; in-flight sessions
  are allowed to finish.

## Data / persistence (new)

**`x_links`**
| column | notes |
|---|---|
| `x_user_id` | PK (X numeric id, stable across handle changes) |
| `x_handle` | display handle |
| `discord_id` | FK to existing user |
| `wallet` | XRPL address |
| `xumm_user_token` | for Xaman push |
| `created_at`, `updated_at` | |

**`x_mint_sessions`**
| column | notes |
|---|---|
| `tweet_id` | PK — one mint per tweet; idempotency + restart safety |
| `x_user_id` | author |
| `status` | `pending_payment` / `paid` / `minted` / `replied` / `failed` / `expired` |
| `nft_number` | once minted |
| `reply_tweet_id` | once replied |
| `error` | terminal failure reason |
| `created_at`, `updated_at` | |

**`x_ingest_state`** — single row holding the `since_id` cursor (and last poll ts).

**Failure tracking** — count of failed/incomplete sessions per `x_user_id` in a
rolling 24h window (derived from `x_mint_sessions` timestamps, or a small counter
table).

## Abuse & cost controls

- **Paywall** — reply (the costly write) only after a confirmed on-chain payment.
- **Linked-only** — unlinked authors dropped before any spend beyond the shared
  mention read.
- **No cap on *paid* mints** — paying users can mint freely.
- **Failed-attempt blacklist** — a handle with **5 failed/incomplete transactions
  within 24h is blacklisted for 24h** (mentions ignored). "Failed/incomplete" =
  payment not signed before timeout, payment rejected, or mint error after a valid
  command. Window and threshold are config constants.
- **Tweet-id dedup** — never process the same tweet twice.
- **X spending cap / auto-recharge ceiling** — hard backstop against runaway reads.
- **Payment timeout** — unsigned payment expires the session (counts as a failure
  for blacklist purposes).

## Failure handling

- Every orchestrator step is idempotent and keyed to `tweet_id`; the status machine
  only moves forward. A restart re-reads session rows and resumes.
- The reply is posted strictly after mint + offer are confirmed — never on an
  unconfirmed mint.
- Transient X/Xaman/XRPL errors retry with bounded backoff; terminal failures set
  `status=failed` with `error`, and may send a courtesy reply (optional, cheap).

## Rollout

- **Polling only** for the initial rollout (filtered-stream / realtime deferred).
- Feature ships behind the admin toggle, default **off**, enabled for the demo.

## Testing (TDD)

**Unit**
- Command parser (strict `!mint` detection, ignores near-misses).
- Mention filter chain (unlinked / blacklisted / non-command / duplicate drops).
- Blacklist logic (5 fails / 24h window, expiry).
- Session state machine (forward-only transitions, idempotency).
- Link handler (OAuth callback → row upsert, user_token capture).
- X Poster (media upload + reply, no-URL caption) with mocked X client.
- Xaman push (payment + accept-offer) with mocked Xaman client.

**Integration**
- End-to-end happy path with mocked X + Xaman: mention → payment → mint → reply →
  offer.
- Restart/resume mid-session resumes without double-mint/double-reply.
- Spam/abuse: repeated failures trigger blacklist; unlinked mentions are no-ops.

## Out of scope (this spec)

- Realtime filtered-stream ingestion.
- Tweet-to-trade / tweet-to-swap (future).
- AI agent integration via XRPL Payments skill — tracked separately in
  Team-Hamsa/Mint-Bot#49.
- Manual share buttons in the web UI (issue #41 optional item) — can be a small
  follow-up reusing the X Poster.
