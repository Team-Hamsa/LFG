# X (Twitter) Integration — Design

**Date:** 2026-07-05
**Issue:** #41 (feat: X (Twitter) integration)
**Status:** Draft (pending brainstorm review)

## 1. Motivation & scope

Issue #41 asks for: OAuth2 connect flow, auto-post on mint (image + traits +
XRPL link), manual share buttons in the web UI, and an admin toggle. This spec
grounds each item in the infrastructure that already exists and splits the work
into a low-risk MVP (brand-account auto-post + zero-OAuth share buttons) and a
deferred per-user OAuth phase, with the tier-limit and effort arguments below.

The codebase already anticipates this: the surface firehose consumers note
"the X surface is deferred to #41" (`surfaces/telegram_bot/events.py:11-12`).

## 2. Existing infrastructure (verified in code)

| Piece | Where | Relevance |
|---|---|---|
| In-process event bus + `/events` WS firehose | `lfg_service/events.py` (Event, InMemoryEventBus); routes at `lfg_service/app.py:1285-1286` | X poster becomes one more subscriber — mint flow is untouched. |
| `mint.completed` / `mint.failed` publish | `lfg_service/app.py:863-871` (`handle_mint_status`), idempotence via `session._published`; generic `publish_terminal` at `app.py:137-179` for swap/harvest/assemble/equip | Event `data` is `session.to_dict()` (`lfg_core/mint_flow.py:137-154`): includes `nft_number`, `nft_id`, `image_url`. |
| Identity enrichment at publish time | `enrich_minter_identity`, `lfg_service/app.py:66-118` | Gives display handle for tweet copy ("minted by @…" — Discord/TG handle, not X handle). |
| Reconnecting firehose client for out-of-process surfaces | `surfaces/_client/events.py:18-` (`stream_events`), service-token auth (`lfg_service/auth.py`) | The X surface is a separate pm2 process exactly like `lfg-telegram`'s events task (`surfaces/telegram_bot/events.py`). |
| Feature-off-when-env-unset convention | `lfg_core/config.py` — e.g. `TELEGRAM_MINI_APP_URL` (unset ⇒ no button), `ECONOMY_ENABLED` boolean flag (`config.py:135`) | `X_*` env vars unset ⇒ feature fully off, no new failure modes. |
| Public image URLs | BunnyCDN `image_url` on every mint (`lfg_core/mint_flow.py:256`, pull zone `config.py:117-120`) | Source bytes for media upload; also OG-card image. |
| Web UI | `webapp/client/app.js` (vanilla JS, no build), served on `:8176`; mint result panel already renders `image_url`/`nft_number` | Manual share button goes on the mint-success and swap-success panels. |
| HTTP deps | `requirements.txt`: aiohttp (async), no tweepy/requests/httpx/oauthlib | Transport: aiohttp. Signing: **`oauthlib` (new dep)** for OAuth 1.0a. tweepy rejected (sync, heavyweight, drags requests). A hand-rolled RFC 5849 signer is rejected as security-sensitive: percent-encoding, parameter normalization, and the multipart body-exclusion rule are notorious for subtle bugs that surface as intermittent 401s. `oauthlib.oauth1.Client` is tiny, battle-tested, sign-only (produces the `Authorization` header; we send with aiohttp). Multipart media uploads MUST sign with the body **excluded** (RFC 5849 §3.4.1.3.1 — only oauth_* params, no body params for non-form-encoded bodies); a unit test pins this behavior. |
| Secrets posture | `.env` only (repo convention, CLAUDE.md); no secret store | Brand-account keys live in `.env`. Per-user refresh tokens (phase 2 only) need at-rest encryption — see §7. |

No XRPL transactions are created anywhere in this feature ⇒ SourceTag rules
don't apply.

## 3. X API reality — assumptions vs verified

House rule: external-API claims below are **assumptions from training
knowledge (cutoff Jan 2026), not verified against live X docs** — the repo has
no X integration to learn from. Each carries a verification step; **Task 0 of
the plan is to verify all of them before writing code.**

- **A1 (high confidence):** `POST /2/tweets` requires *user context* — OAuth
  1.0a user tokens or OAuth 2.0 user tokens (Authorization Code + PKCE).
  App-only bearer tokens cannot post. *Verify:* X docs "manage tweets".
- **A2 (medium):** Free tier allows ~500 posts/month at the app level
  (originally 1,500/mo write-only at launch of the paid tiers in 2023; later
  reduced) and a very low per-user daily cap (on the order of ~17
  requests/24h for `POST /2/tweets` was reported). Basic ($100–200/mo) allows
  ~3,000 posts/mo/user. **Numbers uncertain — X changed these repeatedly.**
  *Verify:* developer.x.com products page + the rate-limit headers returned
  by a live test post.
- **A3 (medium):** Media upload historically lives on v1.1
  (`upload.twitter.com/1.1/media/upload.json`, works with OAuth 1.0a user
  context, available on the free tier); a v2 media upload endpoint
  (`POST /2/media/upload`) was rolled out ~2024–2025 with v1.1 deprecation
  announced. *Verify:* attempt both against the brand account in Task 0;
  build behind an internal `upload_media()` seam so switching endpoints is a
  one-function change.
- **A4 (high):** OAuth 2.0 user access tokens expire in ~2 hours; refresh
  requires the `offline.access` scope; refresh tokens rotate on use (each
  refresh returns a new refresh token that must be persisted atomically).
- **A5 (high):** Tweet text limit 280 chars (weighted); URLs count as 23 via
  t.co wrapping.
- **A6 (high):** The Web Intent URL
  `https://twitter.com/intent/tweet?text=…&url=…` requires **no API access,
  no OAuth, no tier** — it opens X's composer pre-filled, user posts as
  themselves. Link previews on X render only if the shared URL serves
  `twitter:card` meta tags.

## 4. Key decision — who posts?

**Chosen: (a) brand-account auto-post as MVP, (b) per-user OAuth deferred to
phase 3, with Web-Intent share buttons covering the "users share" need in
phase 2 at zero OAuth cost.**

Rationale:

- **Tier limits kill per-user server-side posting at Free tier** (A2): the
  monthly app-level write cap is shared across *all* users; a handful of
  active sharers exhausts it and then the brand auto-post starts failing too.
  Per-user OAuth also brings token storage/rotation/revocation, a public
  HTTPS callback (same ops dependency that keeps Mini-App #89 Part B open),
  and PII duty — all for posts a Web Intent produces for free.
- **Brand auto-post is one account, four static OAuth 1.0a credentials, no
  refresh flow** (OAuth 1.0a tokens don't expire), and it satisfies the
  hackathon-visible half of #41 (every mint tweeted with image + traits +
  explorer link).
- **Web Intents give "optional manual share buttons in the web UI" exactly as
  the issue asks**, from the user's own account, with zero API budget and
  zero secrets. The only quality gap — no image preview — is closed by a tiny
  public OG page (§6).
- Phase 3 (true per-user OAuth2 PKCE) stays specced (§7) so the issue's
  "OAuth2 flow" scope has a designed home, gated on paying for Basic tier or
  confirming Free-tier budget is acceptable.

## 5. Phase 1 — brand-account auto-post (MVP)

### 5.1 New surface: `surfaces/x_bot/`

A separate pm2 process (`lfg-x`), mirroring the Telegram events consumer:

```
surfaces/x_bot/
├── __init__.py
├── bot.py        # entry: LFGServiceClient + stream_events loop
├── poster.py     # event → tweet decision + text composition (pure, testable)
└── x_api.py      # aiohttp calls signed via oauthlib.oauth1.Client: post_tweet(), upload_media()
run_x.py          # shim entry (mirrors run_telegram.py canonical-import lesson)
```

- Subscribes to the same `_ANNOUNCE_EVENT_TYPES` universe but posts **only
  success events** (`mint.completed`; optionally `assemble.completed` later).
  Failures are never tweeted.
- **Fire-and-forget by construction:** the poster is a separate process
  reading the firehose; nothing in `mint_flow`/`app.py` changes, so a dead or
  rate-limited X poster cannot block, slow, or fail a mint. This matches the
  existing announce posture and is the fail-safe-ordering answer.

### 5.2 Auth & config (all optional; unset ⇒ surface refuses to start / stays off)

```
X_ENABLED=1                    # master flag, config.py boolean convention like ECONOMY_ENABLED
X_CONSUMER_KEY=…               # OAuth 1.0a app credentials
X_CONSUMER_SECRET=…
X_ACCESS_TOKEN=…               # brand account user token (generated once in the dev portal)
X_ACCESS_SECRET=…
SERVICE_TOKEN_X=…              # firehose service token, same pattern as SERVICE_TOKEN_TELEGRAM
X_MONTHLY_POST_BUDGET=450      # self-imposed cap below the tier cap (A2)
```

`config.py` addition follows the house pattern: `X_ENABLED` is true only when
the flag is set *and* all four credentials are non-empty.

### 5.3 Tweet composition

```
🎨 LFGO #1234 just minted!
Hat: Wizard Hat · Eyes: Laser · Body: Ape (+5 more)
🔗 https://bithomp.com/en/nft/<nft_id>
#XRPL #NFT
```

- Traits: the mint event `data` (session.to_dict) does **not** carry traits
  today — only `nft_number`/`nft_id`/`image_url` (`mint_flow.py:137-154`).
  Add a `traits` dict to `MintSession.to_dict()` (already computed at
  `mint_flow.py:286-297` for metadata) — small, additive, benefits all
  consumers. Pick 2–3 rarest slots for the tweet (rarity data exists in
  `lfg_core/rarity.py`), truncate to fit 280 (A5).
- XRPL link: bithomp per-`nft_id` URL (mainnet `bithomp.com`, testnet
  `test.bithomp.com`), derived from `config.XRPL_NETWORK`.

### 5.4 Media

**Upload image bytes** (download `image_url` from BunnyCDN via aiohttp, upload
via the media endpoint behind the `upload_media()` seam — A3). Rationale: a
native photo tweet renders full-size for the brand account regardless of any
card infrastructure; tweeting a bare PNG URL renders as a t.co link with no
preview. If Task-0 verification finds media upload unavailable on our tier,
degrade to text + OG-page URL (§6) and say so in the PR.

### 5.5 Dedup, retries, budget — `x_posts` table

New sqlite table (own file `x_state.db`, gitignored like the other per-network
DBs) written by the poster process only:

```sql
CREATE TABLE x_posts (
  event_key   TEXT PRIMARY KEY,   -- "mint:<nft_id>" (nft_id, not nft_number: remints get new ids; an nft_number retry after failure reuses the number but a *successful* mint has exactly one nft_id)
  tweet_id    TEXT,
  posted_at   TIMESTAMP,
  month       TEXT,               -- "2026-07" for budget accounting — computed from posted_at in **UTC** (datetime.now(timezone.utc).strftime("%Y-%m")); never local time, so the budget boundary is unambiguous and test-reproducible
  status      TEXT                -- posted | skipped_budget | failed
);
```

- **Dedup:** skip if `event_key` exists with status `posted`. Protects against
  the documented sub-tick double-publish window (`app.py:175-179`) and process
  restarts replaying nothing (firehose is live-only, no replay — so restart
  loss is *missed* posts, never duplicates; acceptable for a social feed).
- **Retry:** in-process, max 3 attempts with exponential backoff (1s/4s/16s)
  for 5xx/network; **no retry on 4xx**. On 429, record `failed`, log the
  reset header, and back off posting entirely until the reset time.
- **Budget:** count `status='posted'` rows for the current **UTC** month; at
  `X_MONTHLY_POST_BUDGET` switch to `skipped_budget` (logged, visible to
  admin) instead of posting. Never trust the tier to enforce limits politely.

### 5.6 Admin toggle

Matches the `ECONOMY_ENABLED` precedent (env flag, `config.py:135`) plus a
runtime kill switch, because "restart pm2 to stop tweeting" is a bad incident
response:

- `X_ENABLED` env = master (off ⇒ process exits at startup).
- Runtime: `x_state.db` gets a `settings(key,value)` row `posting_paused`.
  The Discord `/admin` panel gains an "X posting: pause/resume" button that
  calls a new service endpoint `POST /api/admin/x/pause`; the service writes
  the flag; the poster checks it before each post. The poster stays running
  and keeps recording `skipped_*` rows, so resuming loses nothing new.
- **Endpoint auth (deliberate restriction):** a generic `require_service_token`
  would let ANY surface token (telegram, or the X poster's own token) pause
  posting. Instead the pause/resume/status endpoints require **the Discord
  surface token specifically** (`surface_for_token(...) == "discord"`,
  `lfg_service/auth.py`), and the human-authorization gate lives on the
  Discord side: the `/admin` button is only reachable behind the existing
  administrator-permission check in the Discord bot. Other surface tokens
  get 403. (A dedicated admin-token env was considered and rejected — it
  adds a secret without adding a distinct trust domain, since the Discord
  bot process would hold it anyway.)

## 6. Phase 2 — manual share buttons (Web Intents) + OG card page

### 6.1 Share buttons (webapp, vanilla JS)

On the mint-success panel and swap-result panel in `webapp/client/app.js`:
a "Share on X" anchor built client-side:

```js
const text = `I just minted LFGO #${s.nft_number}! 🎨 #XRPL`;
const url  = shareBase                                  // from server config, NOT location.origin
  ? `${shareBase}/nft/${s.nft_number}`
  : bithompNftUrl(s.nft_id);                            // fallback when no public host configured
open(`https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}&url=${encodeURIComponent(url)}`);
```

**`location.origin` must NOT be used for the share URL:** inside the Discord
Activity the page is served from Discord's sandbox proxy origin
(`*.discordsays.com`), which is not our public host — X's crawler can't reach
it and the intent would share a dead link. `shareBase` comes from a new
optional config value `PUBLIC_SHARE_BASE_URL` (unset ⇒ empty ⇒ bithomp
fallback, house convention), delivered to the client via the existing
client-config path the webapp already uses (or baked into the rendered page)
— never derived from the browser origin. The same rule applies to any URL
the OG page (§6.2) emits about itself.

No API, no OAuth, no secrets, iframe caveats permitting — **verify
`window.open` to an external origin works in the sandboxed Activity iframe,
and confirm the proxied origin behavior above** (memory: native
`window.confirm` is a silent no-op there); fallback is rendering the intent
URL as a copyable link/`<a target=_blank>`.

### 6.2 OG card page `GET /nft/<number>`

Tiny server-rendered HTML route in `webapp/server.py`: looks up the edition
(LFG table / on-chain index), emits `twitter:card=summary_large_image`,
`twitter:image=<image_url>`, `og:title`, trait summary, and a redirect/link to
bithomp. This is what makes intent-shared links (and any organic link paste)
render as a rich card.

**Ops dependency (assumption, flagged):** cards render only if X's crawler can
reach the URL — requires the webapp exposed on public HTTPS. That is the same
open ops item as Telegram Mini-App #89 Part B. Until then,
`PUBLIC_SHARE_BASE_URL` stays unset and share buttons use the bithomp URL as
the shared `url` (bithomp already serves its own OG tags); setting the env var
flips them to `<PUBLIC_SHARE_BASE_URL>/nft/<number>` with no code change. Any
absolute self-URLs in the OG page (`og:url`, canonical) are likewise built
from `PUBLIC_SHARE_BASE_URL`, never from the request Host header. This keeps
phase 2 shippable now.

## 7. Phase 3 — per-user OAuth2 PKCE (specced, deferred)

Gated on: Basic tier purchase OR verified Free-tier budget headroom, AND
public HTTPS callback (same dependency as §6.2).

- **Flow:** `GET /api/x/connect` → authorize URL (scopes `tweet.read
  tweet.write users.read offline.access`, PKCE S256, `state` bound to the
  wallet session via the existing `require_wallet`/sign-in session machinery
  in `lfg_service/app.py`) → callback exchanges code, stores tokens.
- **Storage:** new table `x_accounts(wallet PK, x_user_id, x_handle,
  refresh_token_enc, access_token_enc, expires_at, connected_at)` in the
  identity DB. Tokens encrypted at rest with Fernet (`cryptography` — new
  dep) using `X_TOKEN_ENC_KEY` from `.env`. Justification: the repo's secret
  posture is .env-only; a leaked DB file (they're gitignored but live on
  disk beside CSVs) must not leak posting capability for users' personal X
  accounts. Refresh rotation (A4) is persisted atomically (single UPDATE)
  before the new access token is used.
- **Revocation:** `DELETE /api/x/connect` calls X's revoke endpoint
  best-effort, then deletes the row unconditionally (fail-closed on our
  side: local delete always wins).
- **Use:** "Share from my account" button posts server-side on the user's
  behalf; falls back to Web Intent when not connected.

## 8. Failure posture summary

| Failure | Effect on mint | Effect on X feature |
|---|---|---|
| X API down / 5xx | none (separate process) | 3 retries, then `failed` row; missed post |
| 429 / budget hit | none | `skipped_budget`, pause until reset; admin-visible |
| Poster process dead | none | events missed (no replay); pm2 restarts it |
| Double event publish | none | deduped by `event_key` |
| Creds revoked | none | startup check fails loudly in pm2 logs |

## 9. Out of scope

- Achievements/trades auto-posts (issue mentions them; event types
  `swap.completed`/`assemble.completed` are wired but posting them is a
  one-line allowlist change once mint posting is proven and budget is known).
- X replies/DMs/engagement ingestion.
- Any XRPL transaction (no SourceTag surface).

## 10. Verification checklist (Task 0 of the plan)

1. Confirm current Free-tier write caps + media-upload availability from
   developer.x.com and a live test post from the brand account (A2, A3).
2. Confirm chosen media endpoint (v1.1 vs v2) with one scripted upload (A3).
3. Confirm `window.open`/anchor-target behavior for external links inside the
   Discord Activity iframe (§6.1), and confirm the Activity's effective origin
   is the `*.discordsays.com` sandbox proxy (i.e. `location.origin` is NOT our
   public host — validates the `PUBLIC_SHARE_BASE_URL` requirement).
4. Record findings in the PR description; update this spec's §3 rows from
   "assumption" to "verified" with dates.
