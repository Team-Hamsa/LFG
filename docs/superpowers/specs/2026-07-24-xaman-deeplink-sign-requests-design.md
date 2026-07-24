# Deep-link all Xaman sign requests (mobile-primary, no forced QR) — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #142

## Problem

A user on the same mobile device the app is running on should be able to **tap
straight into the Xaman app** on a sign request, never scanning a QR with a
second phone. The maintainer clarified the intent in the issue comment: *"about
surfacing a sign request directly in Xaman, not requiring the user to scan a QR
at all."*

Two of the three mechanisms this needs already exist — the issue was filed
before they landed, so most of its original scope is stale:

- **Deep links are already threaded to every surface.** Every payload builder
  returns `xumm_url` (`data["next"]["always"]`, the universal `https://xumm.app/
  sign/<uuid>` link) from `_post_xumm_payload` (`lfg_core/xumm_ops.py`), and the
  flows carry it as `payment_link` / `accept_deeplink` / `xumm_url`. The Activity
  client wires it to an "Open in Xaman" button on mint, swap, market list/buy/
  cancel, extract, closet, assemble and trait-sell (`webapp/client/app.js`
  lines 646-647, 1015-1016, 1120-1121, 1687-1688, 1786-1787, 3338/3353/3371/
  3394). The Discord bot embeds carry the same as markdown links ("Open Payment
  Link" / "Open in XUMM" / "Open in Xaman", `surfaces/discord_bot/render.py`
  lines 36/61/88).
- **Push delivery is live** (#135/#212). `_create_xumm_payload` accepts a stored
  `user_token`, returns `pushed` and a refined `push` state (`sent`/`failed`/
  `None`), and the client renders it honestly via `signText(push, base)`
  (`app.js:627`). This is #27's F5 — **already implemented**, do not re-design.

**What is still missing (the real #142 delta):**

1. **No mobile-primary presentation and no auto-open.** `showFlow(...)`
   (`app.js:633`) — the single choke-point for the mint/swap flow panel —
   *always* shows `flow-qr` when `qrData` is present and *always* shows
   `flow-link-btn` when `link` is present, with the QR as the visual primary and
   the deep-link button below it. There is **no** mobile/touch detection anywhere
   in `app.js` (grep: no `matchMedia`, no `pointer: coarse`, no UA sniff) and
   **no** auto-open. So a mobile user with Xaman installed still sees a QR they
   cannot scan (no second device) and must hunt for the smaller "Open in Xaman"
   button. That is exactly the "forced QR" the issue is about.
2. **The Discord trustline is the last user surface still hand-rolling its
   payload.** `surfaces/discord_bot/trustline.py` POSTs to XUMM directly and reads
   `response_data["refs"]["qr_png"]` / `["next"]["always"]` itself (lines 72-73)
   instead of going through `_create_xumm_payload`. It therefore misses central
   `SourceTag` stamping, provenance memos (#54), push delivery (#135), the
   `push` state, and the 15-minute `expire` default — and it can only ever show a
   QR. (This is #27's F3, designed but never implemented.)
3. **CLI economy scripts** (`scripts/economy_extract.py` / `economy_deposit.py` /
   `economy_assemble.py`) print the `xumm_url` to the terminal with no push (no
   identity context). Per CLAUDE.md these are **deliberately** QR/terminal-only —
   in scope only to be documented as an explicit deferral.

## Constraints discovered

- **SourceTag = 2606160021 + provenance memos on every tx.**
  `_create_xumm_payload` stamps both centrally for all non-`SignIn` txjson
  (`lfg_core/xumm_ops.py`). The hand-rolled trustline POST bypasses this — folding
  it back onto `_create_xumm_payload` is what *restores* the invariant, so this
  change strengthens hygiene rather than threatening it.
- **No-custody / delivery-only.** This is a *presentation* change: which
  affordance is primary and whether we auto-invoke the OS deep link. It touches no
  txjson, no signing, no ledger semantics. `push` is delivery-only (CLAUDE.md).
- **The deep link must stay canonical (`next.always`).** Never emit a `xumm://`
  custom scheme (iOS/Android cameras and in-app openers refuse it) and never
  route through an LFG-hosted redirect (#27 §6 — no such hop exists and adding one
  would break XUMM's universal-link association). Auto-open must call
  `openExternal(link)` with the exact `xumm_url` already in hand.
- **Sandboxed Activity iframe.** `openExternal` (`app.js:217`) routes through the
  Discord SDK's `sdk.commands.openExternalLink` when set (`externalOpener`,
  `app.js:372`), else `window.open`. `window.confirm/alert` are silent no-ops in
  the iframe — the QR-fallback disclosure must be in-DOM, never a native dialog.
- **Poll-driven re-render.** Flow panels re-render on every status poll. Auto-open
  must fire **at most once per unique payload**, keyed like the existing swap
  dedup `${s.id}:${s.payment_link}` (`app.js:1673`) — otherwise every poll would
  re-launch Xaman.
- **Surface platform is known, UA is not needed.** The client already knows its
  host: Discord Activity (`externalOpener` set via SDK), Telegram
  (`params`/telegram init), and standalone web (`platform="web"`). Mobile-ness is
  best detected with `matchMedia('(pointer: coarse)')` (touch primary input)
  rather than UA sniffing.
- **Desktop still needs the QR.** The desktop-shows-QR → phone-scans case is the
  legitimate cross-device path and must remain the default on desktop.

## Design

Three independent seams. Seam A is the heart of the issue; B closes the last
user-surface holdout; C is a documentation deferral.

### Seam A — mobile-primary sign delivery in the Activity client

Add a small presentation helper and a mobile predicate; route the existing
sign-panel renderers through it. **No new payload data is needed** — `link`,
`qrData` and `push` already flow into every renderer.

- **`isCoarsePointer()`** (new, `app.js`): returns
  `window.matchMedia && window.matchMedia('(pointer: coarse)').matches`. Cache the
  MediaQueryList once. This is the "same-device Xaman is plausible" signal.
- **`applySignDelivery(els, { link, qrData, push })`** (new, `app.js`): the single
  place that decides QR-vs-deep-link primacy. Given the panel's QR `<img>`, the
  "Open in Xaman" button, and an (new) "Show QR" disclosure control:
  - `push === 'sent'` → lead with the Xaman app (copy already handled by
    `signText`); collapse the QR behind the disclosure, keep the deep-link button
    visible as a secondary tap.
  - else `isCoarsePointer()` (mobile / touch) → **deep-link button is primary**
    (full-width, top), QR collapsed behind a "Show QR to sign on another device"
    `<details>`/toggle. Fire **auto-open once** per unique `link` via
    `maybeAutoOpen(link)` (see below) — a single `openExternal(link)` on first
    render, guarded so re-renders and returns-from-Xaman don't re-launch.
  - else (desktop, no push) → **today's behavior unchanged**: QR primary, deep-link
    button secondary.
- **`maybeAutoOpen(link)`** (new, `app.js`): dedup set keyed on the raw `link`
  string; opens at most once per payload. Auto-open is **opt-outable** and
  **best-effort** — if `externalOpener`/`window.open` is blocked, the primary
  visible button is the guaranteed path. (Maintainer decision below on whether
  auto-open is on by default or gated behind one tap.)
- **Wire the renderers.** `showFlow(...)` (`app.js:633`) is the primary target
  (mint pay, mint accept, swap fee, swap accept all pass through it via
  `flow-qr`/`flow-link-btn`). The market/economy inline panels
  (`app.js` ~1006-1121, 2108-2130, 2329, 2588, 3338-3394) each build their own
  `open` button + QR `<img>` — refactor each to call `applySignDelivery` on those
  same elements so the behavior is uniform across every sign surface. No backend
  change; `showPanel`/`showFlow` signatures are unchanged apart from the internal
  wiring.
- **Markup:** add a `<details class="qr-fallback">`-style disclosure wrapper (or a
  `hidden`-toggled block + "Show QR" link) around `flow-qr` in
  `webapp/client/index.html`, and the parallel wrappers in the market/economy
  panels. Any `app.js` change **bumps the `?v=` cache-buster** on the `app.js`
  `<script>` in `index.html` in the same commit (CLAUDE.md), and any ES-module
  import touched bumps its `?v=` in lockstep.

### Seam B — fold the Discord trustline onto `_create_xumm_payload`

Replace the hand-rolled POST in `surfaces/discord_bot/trustline.py` (lines ~52-75)
with a call to `lfg_core.xumm_ops._create_xumm_payload` (a `TrustSet` txjson),
so the trustline sign request gains, for free:
- central `SourceTag` + provenance memos (`memos.build_memos_json`,
  `action="trustset"`, `platform="discord-bot"`);
- the `expire` default (15 min) and, where an identity/`user_token` is resolvable
  for the interacting user, **push delivery** + a `push` state;
- the same `xumm_url` deep link surfaced as a first-class "Open in Xaman" markdown
  link in the trustline embed (render helper already exists in `render.py`).

The `TrustSet` txjson keeps its current LFGO limit/currency shape; only the POST
plumbing moves. Discord embeds cannot auto-open (no in-client deep-link launcher),
so the win here is the **deep link + push**, not auto-open.

### Seam C — CLI economy scripts (explicit deferral)

Document in the spec/plan and issue that `scripts/economy_extract.py`,
`economy_deposit.py`, and `economy_assemble.py` remain terminal-only by design
(no identity/push context) — satisfying the issue's "covered or explicitly
deferred" acceptance line. No code change.

### On-ledger tx shape

No transaction shape changes. The only new payload path is Seam B routing the
existing `TrustSet` through `_create_xumm_payload`, which by construction stamps
`SourceTag = 2606160021` and the provenance `Memos` array (`action=trustset`).
Seam A builds no payloads at all.

## Out of scope

- The XUMM push mechanism itself (#135/#212 — already shipped; this design only
  *presents* its `push` state more prominently on mobile).
- Post-sign return/callback routing (#27 return_url work; #27 is the owner).
- Retiring the `xaman.app/detect` XUMM-API-down fallback (#27 F1 / tx-hygiene).
- CLI economy push delivery (deliberate deferral, Seam C).
- Any `xumm://` custom scheme or LFG-hosted redirect shim (rejected, #27 §6).

## Open questions / decisions for maintainer

1. **Auto-open on by default, or one-tap?** Auto-invoking `openExternal(link)` on
   mobile is the most literal reading of "no QR scan at all," but an
   unexpected app-switch on page load can feel jarring and can race the poll
   loop. Recommendation: **auto-open ON for mint/swap *payment* and *accept*
   screens** (the user explicitly asked to sign), **OFF (prominent button only)**
   for screens reached passively. Confirm.
2. **Coarse-pointer vs. explicit surface flag.** `matchMedia('(pointer: coarse)')`
   is simple and host-agnostic; do we also want to force mobile-primary whenever
   the Telegram Mini App / mobile-web session is detected regardless of pointer?
3. **Desktop deep-link demotion.** Keep desktop exactly as today (QR primary), or
   also show a more prominent "Open in Xaman (if installed here)" on desktop?
   Recommendation: leave desktop unchanged to protect the cross-device path.
4. **Trustline push scope (Seam B).** Is a `user_token` reliably resolvable in the
   Discord trustline button context, or should Seam B land as SourceTag/memos +
   deep-link only, with push as a follow-up?
5. **Scope trim.** Is Seam A alone enough to close #142, with Seam B split to its
   own issue (it is really #27 F3)?

## Testing

- **Unit (client, if a JS test harness is in play) / logic review:**
  `applySignDelivery` primacy table — assert (a) `push==='sent'` collapses QR
  regardless of pointer, (b) coarse pointer promotes the deep-link button and
  collapses QR, (c) fine pointer + no push leaves QR primary (today's behavior).
  `maybeAutoOpen` fires `openExternal` exactly once for a repeated `link` and not
  at all for a `null` link.
- **Unit (Python, Seam B):** `tests/` test that the trustline flow calls
  `_create_xumm_payload` and that the resulting txjson carries
  `SourceTag == config.SOURCE_TAG` and a `Memos` entry with `action=trustset`
  (mirror the existing SourceTag/memos invariant tests). Env-guard preamble at
  module top.
- **Regression:** `webapp/test_smoke.py` and the mint/swap/market suites — the
  three-key→four-key `_create_xumm_payload` contract is already in place; assert
  no renderer regressed and that desktop QR-primary is preserved.
- **Manual smoke (human gate, real devices):**
  - iPhone + Android with Xaman installed: open the Activity on the phone, start a
    mint → confirm the deep link is primary and (per decision #1) auto-opens Xaman
    with the correct payload; confirm "Show QR" still reveals a scannable code.
  - Desktop Activity: confirm QR is still primary and scannable from a second
    phone.
  - Paired account: confirm `push==='sent'` leads with "approve in your Xaman
    app" and the QR is collapsed.
  - Discord trustline button on mobile: confirm the "Open in Xaman" link launches
    Xaman with a `TrustSet` request carrying SourceTag + memos.
