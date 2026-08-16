# Dress-Up Phase 3 — Dressing Room UI (Design)

- **Date:** 2026-06-24
- **Issue:** [#65](https://github.com/Team-Hamsa/LFG/issues/65) · Milestone: Dress-Up Trait Economy · Part of [#46](https://github.com/Team-Hamsa/LFG/issues/46)
- **Depends on:** Phase 2 (on-ledger harvest/assemble/equip flows, merged #67/#69/#70)
- **Status:** Approved (brainstorm), pending implementation plan

## 1. Summary

Phase 3 delivers the **Dressing Room** — the dress-up game screen inside the existing
Discord Activity webapp (`webapp/`), replacing the Trait Swapper UI (`swap-panel`).
A unified canvas shows the active character composited live; a Bucket palette lets the
user equip loose traits; the roster strip switches characters or starts an assemble.
The three Phase 2 economy ops (equip / harvest / assemble) are wired to new HTTP
endpoints with XUMM signing only where the chain requires it.

The work is **backend endpoints + frontend UI**: Phase 2 shipped as CLI scripts, so the
"Phase 2 endpoints" the issue references do not exist yet and are part of this phase.

## 2. Stack constraints (why this is built the way it is)

- The Activity webapp is **vanilla JS with no build step**: `webapp/server.py` (aiohttp)
  serves `webapp/client/{index.html, app.js, style.css, vendor/}` as static files. No
  React/Tailwind/shadcn, no bundler. Iteration = edit a client file → refresh.
  Implication: build the Dressing Room **directly in the existing vanilla pattern** and
  design system (sticker cards, Fredoka/Inter, the `/api/img` proxy idiom). v0.dev output
  (React/Tailwind/shadcn) does not drop in and is not used as a code source.
- The Activity runs in a **Discord iframe with a strict CSP**: cross-origin `<img>`/fetch
  is blocked, which is why finished composites already route through the same-origin
  `/api/img` proxy. Any layer asset the client needs must likewise be **served
  same-origin**.
- **Layers are CDN-canonical.** `lfg_core/layer_store.py` resolves any layer by
  `(body, trait_type, value)`; `LAYER_SOURCE=cdn` (prod default) lists/downloads from
  BunnyCDN storage into a local `.layer_cache/`; `LAYER_SOURCE=local` reads `layers/`
  (dev). Either way the server already has each file and is the single source of truth —
  the Dressing Room reuses `layer_store`, it does not introduce a second source.
- Layer files may be `.png`, `.gif`, or `.mp4` (some traits are animated).

## 3. Interaction model

**Unified Dressing Room**, single screen:

```
┌─────────────────────────── Dressing Room ───────────────────────────┐
│  Wallet rXXX…   [Mint]                                               │
├──────────────────────────────────┬──────────────────────────────────┤
│        CANVAS (active char)       │   BUCKET  (palette)              │
│   ┌──────────────────────────┐    │   filter: [All ▾][Hat][Eyes]…    │
│   │   layered composite       │    │   ┌──┐┌──┐┌──┐┌──┐  (xN counts) │
│   │   (instant client stack)  │    │   │Ht││Ey││Mo││Cl│              │
│   └──────────────────────────┘    │   incompatible assets dimmed     │
│   #3537 · male · live             │                                  │
│   [🔥 Harvest]                     │                                  │
├──────────────────────────────────┴──────────────────────────────────┤
│  Roster:  [#3537•][#3540][#3552]  …  [ ＋ Assemble new ]              │
└──────────────────────────────────────────────────────────────────────┘
```

- **Roster strip** of the user's live economy characters; tap a tile → load into canvas.
- Tap a **compatible** Bucket trait → that slot swaps (immediate per-swap equip);
  displaced trait returns to the Bucket.
- **`＋ Assemble new`** tile → canvas becomes an empty-body builder.
- **Harvest** is a guarded button on the active character.

### Body-compatibility gating

Layers are body-specific and `lfg_core/trait_economy.can_equip` already enforces slot/
value validity for the character's body. The palette **mirrors `can_equip`**: assets that
cannot go on the active character's body are **dimmed/disabled** — no new rule, the UI
only reflects the existing economy check.

## 4. Commit granularity — immediate per-swap

Each trait click commits on-chain immediately via the unchanged Phase 2 `run_equip`
(one `NFTokenModify` per swap, Bucket reconciled per swap). No batching; Phase 2 is reused
as-is.

- **In-flight lock (required):** an `NFTokenModify` takes a ledger close to confirm, so the
  tapped slot is locked while its equip is in flight to prevent rapid clicks queuing
  conflicting modifies. The instant client-side stack is the **optimistic** state shown
  while the modify confirms; on failure the toast reports it and the canvas reverts (Phase 2
  already rolls the token/Bucket back).

## 5. Live preview — hybrid compositing

1. **Instant (client):** on each click the browser stacks the body's layer files in z-order
   from `GET /api/layer?…`. `.gif`/`.mp4` traits stack as `<img>`/`<video>`.
2. **Debounced (server):** a throttled `POST /api/economy/preview` runs the real `makeNft`
   for pixel fidelity.
3. **Authoritative (commit):** the image `makeNft` writes to CDN at commit is the truth,
   shown via the existing `/api/img` proxy.

## 6. Flows + signing

On-chain authority is the issuer's (economy characters minted burnable+mutable, flags 25);
the user consents in-app (authenticated Discord + registered wallet). A XUMM QR appears
only where the chain delivers a token to the user.

| Flow | On-chain | User signing |
|---|---|---|
| **Equip** | issuer `NFTokenModify` (in place) | none |
| **Harvest** | issuer burns character → assets to Bucket | none for the burn; **one-time** XUMM accept of the soulbound Bucket on first-ever harvest |
| **Assemble** | issuer mints new edition + `NFTokenCreateOffer` | **XUMM accept** of the new character offer |

- **Harvest** is guarded by a destructive-action confirm ("This burns #NNNN permanently;
  its parts go to your Bucket").
- **Assemble** Commit is disabled until a body + a complete asset set are selected; then it
  reuses the existing QR + poll UI for the offer accept.

## 7. Endpoints (new; modeled on the existing mint/swap handlers in `server.py`)

| Endpoint | Auth | Returns / does |
|---|---|---|
| `GET /api/economy` | auth+wallet | `{ characters:[{nft_id,edition,body,attributes,image_url,mutable}], bucket:{assets:[{slot,value,count}], bodies:[…]} }` |
| `GET /api/layer?body=&trait=&value=` | public (same-origin) | one layer file via `layer_store` (CDN-cached); CSP-safe |
| `GET /api/layers/manifest?body=` | public | z-order (and available values) per body so the client stacks correctly |
| `POST /api/equip` `{nft_id,slot,value}` | auth+wallet | start `EquipSession`; poll `GET /api/equip/{id}` |
| `POST /api/harvest` `{nft_id}` | auth+wallet | start `HarvestSession`; poll status; first-time returns Bucket XUMM accept |
| `POST /api/assemble` `{body,assets}` | auth+wallet | start `AssembleSession`; poll status; returns char-offer XUMM accept |
| `POST /api/economy/preview` `{body,attrs}` | auth+wallet | debounced server `makeNft` composite for fidelity |

- Server-side **re-verification** mirrors the swap handlers: never trust client-supplied
  ownership/trait data; re-load wallet/economy state and re-run `can_equip` /
  assemble preconditions before acting.
- Status polling reuses the `make_status_handler` session pattern and in-memory session
  dicts already in `server.py`.
- The economy flows are driven via `lfg_core/economy_flow.py` `EconomyDeps`, wired to the
  real `xrpl_ops`/`cdn`/`xumm_ops`/`bucket_token` exactly as the Phase 2 CLI drivers do.

## 8. Dev harness — the fast local loop

Goal: iterate the full UI locally with one refresh, no Discord/XRPL/XUMM, no deploy.

- **`WEBAPP_DEV_MODE=1`** — an env flag, **off in pm2 prod and never shipped**. When set:
  - issues a dev session token without Discord OAuth (the client already has a degraded
    no-`frame_id` mode for UI work);
  - routes the economy endpoints to an in-memory **`MockEconomy`** fixture (canned
    characters + Bucket; equip/harvest/assemble mutate the mock and return fake-success
    without touching XRPL/XUMM);
  - paired with `LAYER_SOURCE=local` to serve real layer art from `layers/`.
  - One command: `WEBAPP_DEV_MODE=1 LAYER_SOURCE=local python -m webapp.server` → open
    `http://localhost:8176/` → full Dressing Room on fake data.
- **Live reload** — a dev-only SSE channel plus a file watcher on `client/`; a `<script>`
  injected **only** in dev mode reloads the tab on save. Edit `style.css` → save → tab
  refreshes → see the real paper-doll. Sub-second.
- **`MockEconomy` is the shared fixture:** it is both the dev-mode data source and the
  pytest fixture for the new endpoint tests — it pays for itself twice.

## 9. Error handling

Phase 2 journals every partial failure (e.g. `minted_no_offer`, drain-failed, revert).
The UI surfaces these through the existing dismissing **toasts** with the actionable
message and journal id ("minted but offer failed — contact an admin to re-offer, journal
<id>"), never a silent stuck state. Equip failures revert the optimistic canvas.

## 10. Testing

- **Backend pytest** for the new endpoints (auth gating, server-side re-verification,
  validation, session lifecycle), reusing `webapp/test_smoke.py` patterns. The economy
  flows themselves are already covered by Phase 2 tests.
- **`MockEconomy`** drives the endpoint tests and the dev harness from one fixture.
- **Manual:** the `WEBAPP_DEV_MODE` harness is the manual test surface for the UI;
  testnet end-to-end (real XUMM/XRPL) validates the live wiring before ship.

## 11. Out of scope / non-goals

- No batched/multi-slot equip (immediate per-swap only).
- No React/Tailwind toolchain or v0.dev-generated code in the app.
- No new layer source of truth; CDN + `layer_store` remain canonical.
- No change to Phase 2 economy semantics or supply accounting.

## 12. Decisions captured

| # | Decision |
|---|---|
| Workflow | Build in-repo (vanilla) + `WEBAPP_DEV_MODE` mock harness + live-reload; v0.dev not used as code |
| Interaction | Unified Dressing Room (canvas + Bucket palette + roster strip) |
| Preview | Hybrid: instant client stack + debounced server `makeNft` + authoritative at commit |
| Commit | Immediate per-swap equip (Phase 2 unchanged) + in-flight slot lock |
| Roster | Roster strip + canvas; `＋ Assemble new` tile |
| Gating | Palette mirrors `can_equip` (dim incompatible assets) |
| Signing | Equip/harvest frictionless (issuer); assemble + first-Bucket → XUMM accept |
| Fixture | `MockEconomy` shared by dev harness and endpoint tests |
