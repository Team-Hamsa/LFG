# QR / deep-link routing on mobile — investigation & design (#27)

**Issue:** [#27 — Investigate user-agent / callback routing for QR codes scanned on mobile](https://github.com/Team-Hamsa/LFG/issues/27)
**Type:** INVESTIGATE. The centerpiece is root-cause analysis; the fix design below is
conditional on the reproduction matrix (Task 1 of the plan). The issue explicitly allows
"document why not feasible" as an outcome — and for its UA-redirect question that IS the
outcome (§6).

**Staleness warning:** #27 was filed 2026-06-12, *before* the shared-services spine
refactor (#76–#83), the Discord Activity, the Telegram surface, and the return_url work
(issue #14). The flow the reporter saw no longer exists in that form. Every claim below
is against today's code (branch `feat/shared-layer-dirs`, cross-referenced with
`origin/main`); the original report may be describing behavior that has already been
fixed or relocated. **The first plan task is therefore a reproduction matrix, not a fix.**

Honesty convention: statements about our code are **VERIFIED** (file:line cited).
Statements about iOS/Android/camera/Xaman behavior are **ASSUMPTION** unless marked
otherwise, and are only resolvable by the device matrix (plan Task 1).

---

## 1. Flow inventory — every QR / deep-link we show users (VERIFIED)

### 1.1 What a XUMM payload gives us

`_create_xumm_payload` (`lfg_core/xumm_ops.py:142-165`) POSTs txjson to the XUMM
platform API and returns exactly three things:

| key | source | value |
|---|---|---|
| `qr_url` | `data["refs"]["qr_png"]` (xumm_ops.py:159) | **XUMM-hosted** QR PNG for the payload |
| `xumm_url` | `data["next"]["always"]` (xumm_ops.py:160) | `https://xumm.app/sign/<uuid>` sign link |
| `uuid` | payload uuid (xumm_ops.py:161) | polled via `get_payload_status` (xumm_ops.py:224-245; reads `meta.opened/signed/expired`) |

We do **not** read `next.no_push_msg_key`, `refs.websocket_status`, or the
`pushed` flag from the response. SourceTag is stamped centrally for all
non-SignIn txjson (xumm_ops.py:147-149).

### 1.2 QR PNG sources — two, and only two

1. **XUMM-hosted `refs.qr_png`** — used directly as an embed/photo image:
   accept-offer QRs when `accept_qr_url` is present (`lfg_core/mint_flow.py:350`,
   `lfg_core/swap_flow.py:283`; rendered at `surfaces/discord_bot/mint_view.py:65-71`,
   `surfaces/telegram_bot/mint_view.py:71-74`, `surfaces/telegram_bot/swap_view.py:237-240`),
   and the trustline QR (`surfaces/discord_bot/trustline.py:67`, shown via
   `surfaces/discord_bot/views.py:69`).
2. **Locally rendered** `generate_qr_png` (`lfg_core/xumm_ops.py:97-114`, qrcode lib +
   mascot logo, issue #19) — served by the service at `POST /api/qr`
   (`lfg_service/app.py:1026-1032`) and by the Activity backend at
   `/api/qr.png?d=<data>` (`webapp/client/app.js:78-80`). Used for: payment QRs on
   both bot surfaces (`surfaces/discord_bot/mint_view.py:36-46`), accept-QR fallback
   when the hosted URL is absent, sign-in QRs, and all Activity QRs
   (`webapp/client/app.js:390-391, 540, 810, 846`).

**Key point:** the *renderer* differs but the **encoded string** is the same deeplink
either way. The QR image source is not a root-cause candidate; the URL inside the QR is.

### 1.3 What each QR actually encodes

| Surface / flow | QR encodes | file:line | URL form |
|---|---|---|---|
| Mint payment (Discord, Telegram, Activity) | `session.payment_link` = payload `xumm_url` when the XUMM API call succeeds | `lfg_core/mint_flow.py:104-116` | `https://xumm.app/sign/<uuid>` |
| Mint payment **fallback** (XUMM API down / prepare cancelled) | `generate_static_payment_link` | `lfg_core/mint_flow.py:104, 127-135`; link built at `lfg_core/xumm_ops.py:41-52` | `https://xaman.app/detect/<txjson-hex>` |
| Swap fee payment | payload `xumm_url`, detect link only if XUMM down | `lfg_core/swap_flow.py:302-312` | sign / detect fallback |
| NFT offer accept (mint, swap, closet, assemble, extract) | `accept_deeplink` = payload `xumm_url` | `lfg_core/mint_flow.py:341-351`, `lfg_core/swap_flow.py:268-284` | `https://xumm.app/sign/<uuid>` |
| Trustline (Discord, bot-local) | payload `xumm_url` | `surfaces/discord_bot/trustline.py:67-68` | `https://xumm.app/sign/<uuid>` |
| SignIn / register (Activity + bots) | `signin_link` = payload `xumm_url` | `lfg_service/app.py:954-964`, `webapp/client/app.js:536-554` | `https://xumm.app/sign/<uuid>` |
| CLI economy ops (extract/assemble) | **no QR generated** — the `xumm_url` is printed to the terminal (`scripts/economy_extract.py:37-38`, `scripts/economy_assemble.py:60-61`); ops practice is to hand-render a QR (`_q.png`) because CLI payloads come back `pushed:false` | sign link |

**VERIFIED conclusion:** with the sole exception of the *XUMM-API-down fallback*, every
QR and every "Open in Xaman" button in the product encodes
`https://xumm.app/sign/<uuid>` — Xaman's **own** hosted sign URL. We never encode a
`xumm://` custom scheme, and we never encode an LFG-hosted callback URL. There is no
LFG-controlled hop between the camera scan and Xaman.

### 1.4 return_url inventory (post-sign bounce-back)

| Path | return_url | file:line |
|---|---|---|
| Activity mint / swap / signin | `{app: discord://-/channels/<g>/<c>, web: https://discord.com/channels/<g>/<c>}` from client guild/channel ctx | `lfg_core/xumm_ops.py:125-139`; wired at `lfg_service/app.py:705-712, 731, 843, 954`; client sends ctx at `webapp/client/app.js:88-95` |
| Discord bot trustline | `{web: "https://letseffinggo.com/"}` only — no `app` key | `surfaces/discord_bot/trustline.py:55` |
| Discord/Telegram bot mint & Telegram swap | **none** (surfaces call the service without guild/channel ctx → `discord_return_url` returns None) | `lfg_core/mint_flow.py:47-54` default None |
| CLI economy ops | none | scripts pass no return_url |

### 1.5 Cross-ref: tx-hygiene spec (origin/main)

`origin/main:docs/superpowers/specs/2026-07-05-tx-hygiene-design.md` (site 7) already
flags `generate_static_payment_link` (xumm_ops.py:41-52) as the **live SourceTag gap**
— the detect link hex-encodes txjson without the Make Waves tag, and its PR-1 proposes
either adding the tag or **routing the fallback through `_create_xumm_payload`
entirely**. This spec's recommendation (§5.3) aligns: shrinking/removing the detect-link
path fixes a deep-link inconsistency *and* the hackathon-credit leak in one move. Any
work here must coordinate with that PR to avoid conflicting edits to xumm_ops.py.

---

## 2. Root-cause taxonomy — what happens when a phone camera scans each URL form

All rows below are **ASSUMPTIONS** about OS/wallet behavior (based on how universal
links / app links / custom schemes are documented to work) until plan Task 1 verifies
them on devices. They are the hypothesis space, ranked.

| # | QR contents | iOS camera | Android camera | Do WE emit it? |
|---|---|---|---|---|
| (a) | `https://xumm.app/sign/<uuid>` | Universal link: if Xaman is installed and has the `xumm.app` association, the camera's "open" action should hand straight to Xaman. If association fails (or user long-presses → Safari), the browser loads Xaman's hosted sign page, which itself shows an "Open in Xaman" button and can fall back to web-sign. | App link: same shape; Chrome may show a disambiguation or open the web page with an intent button. | **Yes — every payload QR (dominant path)** |
| (b) | `https://xaman.app/detect/<hex>` | Same universal-link mechanics but on the `xaman.app` domain; the *detect* page must parse raw txjson client-side. Per the docstring at `lfg_core/xumm_ops.py:179-181` (VERIFIED as our own prior finding), only Xaman understands this format; other wallets and the web page cannot complete it, and the tx-hygiene spec notes it may not carry SourceTag through. | same | **Only as XUMM-API-down fallback** (mint_flow.py:133, swap_flow.py:309) |
| (c) | `xumm://` custom scheme | iOS camera refuses/ignores custom schemes in QRs; only works from within an app that calls openURL. | Similar; camera apps typically won't offer to open it. | **No — never emitted** (verified: no `xumm://` anywhere in the codebase) |
| (d) | post-sign `return_url.app` = `discord://-/channels/...` | Not scanned — invoked *by Xaman* after signing, on the phone. Custom scheme from an installed app is the reliable case; breaks only if Discord isn't installed on the scanning phone (then `return_url.web` in browser). | same | Yes (Activity flows only, §1.4) |

**Most likely root causes of the original report, ranked:**

1. **The "intermediary browser" is Xaman's own hosted sign page** (row a, association
   miss or in-app-browser scan). Since we already encode XUMM's canonical sign URL,
   this hop is XUMM's designed fallback, not an LFG bug — the page's "Open in Xaman"
   button is the vendor-supported recovery. Nothing we host sits in the path
   (VERIFIED §1.3); nothing we could UA-sniff exists (§6).
2. **The scan happened inside an in-app browser/scanner** (Discord's own camera, or a
   QR scanned from a screenshot inside Discord mobile). In-app WebViews are documented
   to break universal-link handoff — the URL renders as a web page instead of
   launching Xaman. This is a client-environment issue; mitigation is copy
   ("scan with your phone's *camera app* or with Xaman's in-app scanner"), not code.
3. **The detect-link fallback** (row b): a worse page, wallet-lock-in, and untagged.
   If the reporter hit a window where the XUMM API errored, they scanned a detect
   link. Low probability but the only path where *our* URL choice differs from
   Xaman's canonical one — and the only code-side fix candidate.
4. **Historic code paths** now deleted (pre-spine `main.py` flows). Cannot reproduce;
   if the matrix can't reproduce on today's builds, this is the closing disposition.

---

## 3. What Xaman/XUMM already provides (and what we use)

- **`next.always`** — the universal sign link; we use it everywhere (§1.1). VERIFIED.
- **`refs.qr_png`** — vendor-hosted QR of that same link; we use it where available
  (§1.2). VERIFIED. Note both our QR and theirs encode the identical URL, so scanning
  behavior is identical.
- **`pushed` / push notifications** — when the signing account has the app paired to
  our XUMM app credentials, the payload API pushes a native notification and **no QR
  scan is needed at all** (the phone-native path that bypasses every camera/browser
  hop). We never read the `pushed` field (VERIFIED: absent from xumm_ops.py:158-162),
  so paired users are always shown a QR they don't need. Ops memory confirms CLI
  payloads return `pushed:false` (unpaired issuer context). **Opportunity, not bug.**
- **`return_url.app` / `return_url.web`** — post-sign redirect; used correctly by the
  Activity (§1.4), partially by trustline (web-only), not at all by the bot-surface
  mint/swap flows. Inconsistency, not breakage: absence just means no bounce-back
  button after signing.
- **xApps / `xumm.app/detect`** — the detect link is a legacy convenience; Xaman's own
  guidance (per our docstring xumm_ops.py:176-181) is that sign-request payloads are
  the canonical flow. ASSUMPTION on vendor guidance details; the docstring is ours.

---

## 4. Reproduction matrix (plan Task 1 — human gate, real devices)

Grid to execute before any code change. Record: what app opens, how many taps,
whether Xaman receives the payload, whether post-sign return works.

| Axis | Values |
|---|---|
| Scanning device | iPhone (recent iOS) · Android (recent) |
| Scanner / action | native camera scan · Xaman in-app scanner · Discord mobile in-app camera/browser · **tap link directly (no scan — mobile display rows)** |
| QR / link content | sign URL (mint payment QR from Activity) · sign URL (Discord bot embed, XUMM-hosted qr_png — trustline) · detect link (force fallback: run with XUMM creds broken on testnet) |
| Display surface — desktop | desktop Discord (Activity) · desktop Discord (bot embed) · Telegram desktop · terminal (CLI op) |
| Display surface — **mobile (tap, not scan)** | **mobile Discord bot embed** ("Open Payment Link"/"Open in XUMM" markdown links, render.py:24, 48) · **mobile Discord Activity** (`openExternal` via `sdk.commands.openExternalLink`, app.js:97-100, 136) · **mobile Telegram** (offer caption link, swap_view.py:247) |
| Post-sign | return_url present (Activity) · absent (bot mint) |

The mobile-display rows exercise root-cause candidate #2 directly: the sign URL is
opened *from within* the Discord/Telegram in-app browser on the same phone that has
Xaman installed — the exact environment where universal-link handoff is documented
(ASSUMPTION) to degrade to a web page instead of launching the app.

Pass criterion per cell (identical for scan and tap): Xaman opens with the request
in ≤2 taps; sign → (where return_url set) lands back in Discord/Telegram. Any cell
that reproduces "stuck on an intermediary browser page with no working handoff" is
the confirmed root cause and selects the corresponding fix in §5.

---

## 5. Design — fixes per finding (conditional on §4)

### 5.1 Always encode the canonical sign URL — already true; make it total
The dominant path is already correct (§1.3). Close the two gaps:

- **F1 — retire the user-facing detect fallback.** In `mint_flow.ensure_payment_fallback`
  (mint_flow.py:127-135) and `swap_flow` (swap_flow.py:306-312), replace
  "detect link when XUMM API is down" with **retry-then-error**: surface "signing
  service unavailable, tap Try Again" instead of a degraded link that (i) only Xaman
  can parse, (ii) drops SourceTag, (iii) has unverified scan behavior.

  **Cross-spec reconciliation (OWNERSHIP STATEMENT).** The tx-hygiene spec
  (origin/main §2.1) prescribes a different disposition for the same function:
  add `SourceTag` inside `generate_static_payment_link` (with routing through
  `_create_xumm_payload` as its own fallback if Xaman strips the tag). Two
  committed specs must not leave divergent end-states for one code path, so:
  - **This spec OWNS the end-state disposition** of the user-facing detect-link
    path: **retired** (retry-then-error). Retirement obsoletes the tag question
    for this path — a link never shown to users cannot leak untagged payments.
  - **tx-hygiene PR-1's tag fix is the interim hotfix** and still lands first if
    it is ready first (the leak is live on mainnet today, while F1 is gated
    behind this spec's reproduction matrix). Sequenced end state: tag now
    (tx-hygiene PR-1), retire later (F1). F1's PR then deletes the tagged
    fallback and retires the corresponding regression assertion (the
    detect-hex SourceTag decode in `tests/test_xumm_source_tag.py`, per
    tx-hygiene §2.3).
  - If F1 lands first, tx-hygiene PR-1 drops its `generate_static_payment_link`
    item entirely (its choke-point/AST enforcement is unaffected).
  - This reconciliation is recorded in both plans' task notes: this plan's
    Task 2, plus a **one-line amendment needed in the tx-hygiene plan on main**
    (its detect-link fix marked interim, superseded by #27/F1 at end state) —
    flagged to the coordinator, who owns the main-branch edit.
- **F2 — no other change**: never introduce `xumm://` or an LFG-hosted callback.

### 5.2 return_url consistency per surface
- **F3 — trustline** (`surfaces/discord_bot/trustline.py:55`): today web-only
  letseffinggo.com. Either drop it (match bot mint) or route trustline through
  `xumm_ops` with a proper Discord return_url; while there, migrate the hand-rolled
  payload POST (trustline.py:52-70) onto `_create_xumm_payload` to de-duplicate.
- **F4 — Telegram surface**: optional `return_url.app = tg://` / `https://t.me/<bot>`
  bounce-back after signing. Nice-to-have; only if the matrix shows users getting
  stranded in Xaman.
- Activity flows: already correct (`discord://` app + `https://discord.com` web,
  §1.4) — no change.

### 5.3 Paired-user push path (opportunity)
- **F5 —** read `pushed` from the payload response in `_create_xumm_payload` and pass
  it through `to_dict()`; surfaces render "Request sent to your Xaman app — check your
  phone" (QR still shown as backup, per XUMM's own UX guidance). Removes the scan
  entirely for the desktop-shows-QR/mobile-signs case, which is the issue's exact
  scenario. Small, testable, high UX value regardless of matrix outcome.

  **Return-contract impact (ADDITIVE ONLY).** Today `_create_xumm_payload` returns
  exactly `{qr_url, xumm_url, uuid}` (xumm_ops.py:158-162). F5 adds `pushed` as an
  **optional fourth key, default absent/False** — never removes or renames the
  existing three. Safety audit (VERIFIED): every consumer accesses the dict by key,
  never by positional/tuple unpacking, so extra keys are inert:
  - `lfg_core/mint_flow.py:115-116` (`payload["xumm_url"]`, `.get("uuid")`),
    `:350-352` (`accept["qr_url"]`, `["xumm_url"]`, `.get("uuid")`)
  - `lfg_core/swap_flow.py:283-284, 307` (key access)
  - `lfg_service/app.py:954-964` (`payload["uuid"]`, `payload["xumm_url"]`)
  - `surfaces/discord_bot/trustline.py:67-68` builds its own dict (not a consumer;
    untouched unless F3 lands)
  - `scripts/economy_extract.py:38` / `scripts/economy_assemble.py:60` (`.get("xumm_url")`)
  - tests stub the functions with three-key dict fakes (`webapp/test_smoke.py:189,
    213, 733, 974-986`, `tests/test_service_signin_platform.py:35`) — they keep
    passing because nothing asserts key-set equality.
  Call sites that **surface** `pushed`: mint/swap session `to_dict()`
  (mint_flow.py:140-159, swap flow result dicts) → Discord/Telegram captions +
  Activity flow panel. Call sites that **ignore** it: signin (app.py:964 returns
  only uuid/signin_link — pairing is unknown pre-signin), trustline (until F3),
  CLI scripts (issuer context, known `pushed:false`).

### 5.4 Copy fix (zero-code)
- **F6 —** embed/caption copy already says "Scan the QR code" (render.py:22, 46);
  add "…with your phone's camera or the scanner inside Xaman" to steer users away
  from in-app browsers (root-cause candidate #2, unfixable in code).

## 6. The issue's UA-aware-redirect question — NOT APPLICABLE (documented non-feasibility)

**VERIFIED:** LFG hosts **no** callback, redirect, or landing page anywhere in the
scan-to-sign path. QR URLs point at `xumm.app`/`xaman.app` (vendor-hosted);
`return_url` points at Discord or letseffinggo.com (§1.4); the only LFG-served
endpoints near this flow are image renderers (`lfg_service/app.py:1026-1032`,
`/api/qr.png` in the Activity) which return PNGs, not redirects. The Telegram Mini App
auth (`POST /api/telegram/auth`) is an in-app token exchange, not a scanned URL.
Therefore "detect the UA of the device that opens the QR callback URL and serve
different redirect logic" has no place to live: the device that opens the URL talks to
Xaman's servers, not ours. Building an LFG redirect shim *in order to* UA-sniff would
insert exactly the intermediary hop the issue complains about, and would break XUMM's
universal-link association. **Recommendation: answer the issue's UA questions as
"not applicable in the current architecture" and close via the matrix + F1/F3/F5/F6.**

## 7. UX impact

- Today's dominant flow is vendor-canonical; worst case is one extra browser page with
  a working "Open in Xaman" button — annoying, not broken (pending matrix confirmation).
- F1 converts a silent-degradation path into an explicit retry (better than a QR that
  scans into a dead end); F5 removes the QR step entirely for paired users; F6 costs
  nothing. No flow gets slower; no new hops are added.
- If the matrix reproduces nothing, the honest disposition is: original report was
  against pre-spine code; document, land F5/F6 as improvements, close #27.
