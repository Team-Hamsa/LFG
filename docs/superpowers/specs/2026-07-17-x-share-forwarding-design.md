# X Share-Card Click-Through Forwarding + Share Attribution — Design

Date: 2026-07-17
Related: #41 (X integration), spec `2026-07-16-web-surface-design.md` (build.letseffinggo.com)

## Problem

Users share `PUBLIC_SHARE_BASE_URL/nft/<number>` links on X. The X crawler
renders a proper per-NFT card from the page's Twitter/OG meta tags
(`handle_nft_card`, `lfg_service/app.py`), but a **human** clicking the link
lands on the bare HTML body (h1 + image + Bithomp link) — off-brand, and a
dead end instead of a funnel into the minting webapp. Separately, there is no
way to know **who** shared a link or whether shares convert.

## Constraints / key facts

- The card image comes from meta tags, not the body — the human-visible body
  can be anything.
- **No HTTP 301/302 on the share URL**: X's crawler follows redirects and
  would card the destination (build.letseffinggo.com's generic page) instead
  of the per-NFT image.
- No UA sniffing, no meta-refresh (some crawlers follow meta-refresh).
  JS-only redirect keeps the bot on the card page deterministically.
- This touches only the user-initiated share flow. The #41 auto-poster stays
  link-free by cost directive ($0.015 vs $0.20/post) — out of scope.

## Design

### 1. Forwarding (`SHARE_FORWARD_URL`)

- New env var `SHARE_FORWARD_URL` (e.g. `https://build.letseffinggo.com`),
  parsed in `lfg_core/config.py` like `PUBLIC_SHARE_BASE_URL`
  (strip + rstrip `/`). **Unset ⇒ zero behavior change** (current body
  stays), per the repo's feature-flag convention.
- When set, `handle_nft_card` keeps its meta tags exactly as-is and replaces
  the body with a redirect shell:
  - minimal branded flash (dark background, collection title) — it lives
    ~200 ms;
  - inline `<script>location.replace(<url>)</script>` — `replace`, not
    `href=`, so the interstitial doesn't pollute back-button history in X's
    in-app browser;
  - visible fallback link "Open Let's Effing Go →" to the same URL for the
    no-JS case, with the Bithomp link retained in that fallback body.
- The forward URL is JSON-escaped into the script and `escape()`d into the
  fallback `<a>` (config-controlled, escape anyway).
- 404 page unchanged.

### 2. Share attribution (`?ref=`)

- **Capture:** the Activity share button (`shareUrlFor` in
  `webapp/client/app.js`) appends `?ref=<sharer wallet>` when the user is
  signed in (wallet already known client-side). Wallets are public on-chain;
  no new information is leaked. No opaque-code indirection (machinery for
  cosmetic gain).
- **Card tags stay ref-less:** `og:url` / `canonical` continue to point at
  the bare `/nft/<number>` URL so X dedupes card variants correctly.
- **Logging:** `handle_nft_card` validates `ref` as a classic XRPL address
  (shape check only) and appends a row to a new `share_clicks` table
  (app DB, self-migrating like other stores): `nft_number`, `ref_wallet`
  (nullable), `is_bot` (UA contains `Twitterbot`/`facebookexternalhit` etc.),
  `user_agent` (truncated), `clicked_at`. Logging is best-effort — a DB
  failure never breaks the card response.
- **Forward the ref:** when redirecting, `?ref=<wallet>` is appended to the
  `SHARE_FORWARD_URL` target; the webapp client stashes a valid-shaped ref
  in `localStorage` on load. That's the whole client change — consuming it
  at mint time is out of scope.

### 3. Follow-up (separate issue, not in this change)

Full mint attribution: client sends the stashed ref when starting a mint;
service records `referrer` on the mint → per-wallet conversion metrics /
future rewards. Touches the mint API + DB; deserves its own review.

## Error handling

- JS disabled → fallback link renders.
- Malformed `ref` → ignored (logged as NULL), card still serves.
- `share_clicks` insert failure → logged, response unaffected.

## Testing

Extend `webapp/test_smoke.py`:
- `SHARE_FORWARD_URL` set → response still contains per-NFT
  `twitter:image` / `twitter:card` tags AND the `location.replace` script +
  fallback link; no HTTP redirect status.
- Unset → no script in body (today's behavior).
- `?ref=` valid → `share_clicks` row with the wallet; garbage ref → row with
  NULL ref; bot UA → `is_bot=1`.
- `og:url`/`canonical` never include `ref`.
- Config-parsing tests mirroring the `PUBLIC_SHARE_BASE_URL` ones.
- Client: `shareUrlFor` appends `ref` only when a wallet is present.

## Ops

Set `SHARE_FORWARD_URL=https://build.letseffinggo.com` in prod `.env`,
restart `lfg-activity`. Staging may point at its own host or stay unset.
