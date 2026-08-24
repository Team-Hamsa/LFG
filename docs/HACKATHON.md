# Hackathon Build Log

A record of everything designed, built, and merged for **LFG** during the
[XRPL Make Waves Hackathon](https://xrpl.org/) sprint (since June 21, 2026). Each
item links the PRs and issues that landed the work.

Every XRPL transaction and Xaman (XUMM) signing payload the app builds carries
`SourceTag 2606160021`, the project's assigned Make Waves source tag — that is
how transaction volume is credited to this entry.

> Line-of-code growth stats (the self-updating hackathon LoC bar) live in the
> project [README](../README.md); they are regenerated on every push to `main`.

## Contents

- [Shared-Services Spine](#shared-services-spine)
- [Telegram Integration](#telegram-integration)
- [Dress-up Trait Economy](#dress-up-trait-economy)
- [Trait Shop](#trait-shop)
- [In-app Marketplace](#in-app-marketplace)
- [Bulk Minting](#bulk-minting)
- [Milady Body + Animated Layers](#milady-body--animated-layers)
- [Ledger History + Leaderboards](#ledger-history--leaderboards)
- [On-chain NFT Index](#on-chain-nft-index)
- [NFT Generation & Rules](#nft-generation--rules)
- [Trait Rules Engine + Body Affinity](#trait-rules-engine--body-affinity)
- [Mainnet Launch Hardening](#mainnet-launch-hardening)
- [Standalone Web Surface](#standalone-web-surface)
- [X Integration](#x-integration)
- [Sponsored Free Mint](#sponsored-free-mint)
- [BRIX Daily Drip](#brix-daily-drip)
- [Staging/Prod Stacks + Ops Automation](#stagingprod-stacks--ops-automation)
- [Payment & Signing Safety](#payment--signing-safety)
- [Identity, Profiles & Rarity](#identity-profiles--rarity)
- [Living README](#living-readme)
- [Merged changelog](#merged-changelog)

---

## Shared-Services Spine

**Issues [#43](https://github.com/Team-Hamsa/LFG/issues/43) / [#53](https://github.com/Team-Hamsa/LFG/issues/53).**
**PRs [#76](https://github.com/Team-Hamsa/LFG/pull/76), [#78](https://github.com/Team-Hamsa/LFG/pull/78), [#79](https://github.com/Team-Hamsa/LFG/pull/79), [#80](https://github.com/Team-Hamsa/LFG/pull/80), [#81](https://github.com/Team-Hamsa/LFG/pull/81), [#82](https://github.com/Team-Hamsa/LFG/pull/82).**

One `lfg_service` backend now serves every surface through a shared Surface SDK:
the REST/WS backend (Plan 1), the `LFGServiceClient` SDK (Plan 2), the Discord
bot migration (Plan 3), and the new Telegram surface (Plan 4).

## Telegram Integration

**PRs #81–#83, #92–#98.**

- Full Telegram bot: registration, minting, and a chat-style **trait swapper via inline keyboards** (#96).
- **Telegram Mini App** (feature-flagged) serving the Activity inside Telegram with signed-`initData` auth (#98).
- Xaman-verified `/register` on both Discord and Telegram (#83).
- Unified wallet-keyed **cross-surface accounts** with display handles (#94), minted-artwork announcements (#92, #95), and a cross-surface event **firehose** announcing swaps and economy actions everywhere (#97).

## Dress-up Trait Economy

**Issue [#46](https://github.com/Team-Hamsa/LFG/issues/46) — PRs #62, #67, #71, #105, #106.**

A full on-ledger trait economy in four phases:

- **Phase 1** — supply model, genesis reconciliation, conservation auditor [#62](https://github.com/Team-Hamsa/LFG/pull/62).
- **Phase 2** — on-ledger ops: **Harvest** (burn a character → its traits drop into your Closet), **Assemble** (body + full trait set → re-mint), **Equip** (`NFTokenModify` a loose trait onto a live character) [#67](https://github.com/Team-Hamsa/LFG/pull/67).
- **Phase 3** — **Dressing Room UI** in the Discord Activity: visual composer with canvas + roster [#71](https://github.com/Team-Hamsa/LFG/pull/71).
- **Phase 4** — **tradeable trait tokens**: Extract a Closet trait as a standalone transferable NFToken (7% royalty) and Deposit it back, creating a secondary market for individual traits [#106](https://github.com/Team-Hamsa/LFG/pull/106).
- The per-user **Closet** is a soulbound mutable NFToken with standalone issuance [#105](https://github.com/Team-Hamsa/LFG/pull/105).

> **Live on mainnet since 2026-07-21** (`ECONOMY_ENABLED=1`,
> [#185](https://github.com/Team-Hamsa/LFG/issues/185)) after the go-live review findings
> ([#178](https://github.com/Team-Hamsa/LFG/issues/178)–[#184](https://github.com/Team-Hamsa/LFG/issues/184)) were fixed.
> Since then: the **blank-harvest modify-in-place model** (Harvest strips a character to a
> canonical blank via `NFTokenModify` instead of burning it; Assemble re-dresses your own
> blank — collection size unchanged) ([#309](https://github.com/Team-Hamsa/LFG/pull/309),
> [#319](https://github.com/Team-Hamsa/LFG/pull/319)), fire-and-forget stacked + batch
> harvests ([#307](https://github.com/Team-Hamsa/LFG/pull/307), [#379](https://github.com/Team-Hamsa/LFG/pull/379)),
> batched Build saves — one `NFTokenModify` per Save ([#313](https://github.com/Team-Hamsa/LFG/pull/313)),
> a Closet-accept backstop sweep ([#436](https://github.com/Team-Hamsa/LFG/pull/436)), and a
> no-re-freeze supply audit that records out-of-band burns as shrinkage with nightly
> reconcile + audit crons ([#352](https://github.com/Team-Hamsa/LFG/pull/352), [#429](https://github.com/Team-Hamsa/LFG/pull/429)).

## Trait Shop

**Issue [#217](https://github.com/Team-Hamsa/LFG/issues/217).**

A BRIX-denominated, on-demand trait mint. Pick any (slot, value) and buy a freshly
minted trait token straight into your Closet — no matching Extract/List seller needed.

- **Rarity-based pricing**: `price = clamp(SHOP_BASE_BRIX / smoothed_share, min, max)` —
  scarcer traits (lower rarity share, plus a realized-sales feedback term) cost more BRIX.
- **BRIX burned by construction**: payment is a destination-locked, `Expiration`-bounded
  BRIX `NFTokenOffer` sell offer straight from the issuer-minted token to the buyer —
  same native-offer model as the marketplace, no escrow.
- **Not supply-neutral** (unlike Extract/Deposit): each purchase mints a new trait token
  and writes a `supply_changes` growth row, with a matching decrement on any revert/expiry,
  so the conservation invariant still holds. A periodic sweep expires stale offers and
  retries stuck settlements.
- Feature-flagged off in production alongside the rest of the trait economy (`ECONOMY_ENABLED=0`).

## In-app Marketplace

**Issue [#44](https://github.com/Team-Hamsa/LFG/issues/44) — PRs #129, #132, #134, #139.**

- XRP-denominated marketplace for characters and trait tokens, built entirely on
  native `NFTokenOffer` sell offers — no escrow, no custodial holding ([#129](https://github.com/Team-Hamsa/LFG/pull/129)).
- Derived `market_listings` index kept current three ways: the live tx listener,
  finalize-writes from the List/Buy/Cancel session state machines, and an
  idempotent backfill sweep.
- Fail-closed buys: the sell offer is re-verified on-ledger immediately before
  the Xaman payload is built, and the signer is checked against the session wallet.
- Sold traits settle automatically back into the buyer's Closet, with a retry
  sweep backstopping restarts.
- **Xaman push delivery** ([#135](https://github.com/Team-Hamsa/LFG/issues/135), [#139](https://github.com/Team-Hamsa/LFG/pull/139)) —
  returning users get sign requests pushed straight to the Xaman app instead of
  rescanning a QR (QR/deep link always returned as fallback).

## Bulk Minting

**Issue [#215](https://github.com/Team-Hamsa/LFG/issues/215).**

Mint N editions behind a single payment instead of running the mint flow N times —
a durable batch job (`BulkMintJob`) that reuses the single-mint machinery per unit.

- **One K× payment** (LFGO-vs-XRP detection identical to single mint, at
  `unit_price × quantity`), then a loop mints each unit and offers it back.
- **Crash-resumable**: the whole job is persisted (atomic tmp-file + `os.replace`) after
  every unit; on restart, `paid`/`fulfilling` jobs re-attach and resume from the last unit —
  no double-charge and no double-mint (a minted-but-unoffered unit is re-offered, never re-minted).
- **Supply-capped**: quantity is clamped to `min(requested, BULK_MINT_MAX, headroom)` against
  `MAX_COLLECTION_SIZE` before payment; a cap-race loss converts to a redeemable `mint_credits`
  row rather than a lost payment.
- Endpoints `POST /api/mint/bulk` + status/active/cancel; API-wired (no Activity UI yet).

## Milady Body + Animated Layers

**PRs #171, #174.**

- Fifth body type (**milady**) registered end-to-end: art, trait config,
  affinity matrix, swap matrix ([#171](https://github.com/Team-Hamsa/LFG/pull/171)).
- **Animated trait layers**: transparent-GIF body values compose into video
  NFTs (MP4 + PNG thumbnail); `scripts/make_animated_layer.py` (ffmpeg → gifski)
  produces compliant 1080×1080 alpha-preserving GIFs ([#174](https://github.com/Team-Hamsa/LFG/pull/174)).

## Ledger History + Leaderboards

**Not in original scope — PRs #118–#121.**

- Per-network **ledger history database**: raw `account_tx` archive with derived NFT and BRIX events, resumable backfill (95k+ mainnet txs), and live dual-write from the index listeners (#118, #119).
- Public `GET /api/leaderboard` with **8 boards** — most NFTs held, most swaps, most builds, most-swapped NFTs, **BRIX richlist**, LP richlist, BRIX earned, and NFT rarity — with rolling time windows and a "me" rank lookup.
- Activity **Leaderboard UI** with a two-tier category/board selector (#120, #121).
- Nightly BRIX/LP balance snapshots for trend charts.

## On-chain NFT Index

**Not in original scope — PRs #59, #60.**

Per-network SQLite index of every live NFToken (the chain holds multiple tokens
per edition), kept fresh by pm2 listeners on the clio tx stream, plus a
layer-coverage auditor and Bithomp CSV importer.

## NFT Generation & Rules

- **Ape face compose rule** — nose injection + melt-ape masking, fixing face traits on ape bodies ([#38](https://github.com/Team-Hamsa/LFG/issues/38)) (#110).
- **Seasonal trait manifest** — sidecar `layers/seasons.json` (1,167 traits across 3 seasons) with Season 3 excluded from minting (#115–#117).

## Trait Rules Engine + Body Affinity

**Issues [#40](https://github.com/Team-Hamsa/LFG/issues/40), [#28](https://github.com/Team-Hamsa/LFG/issues/28), [#30](https://github.com/Team-Hamsa/LFG/issues/30) — PRs #122, #123, #126–#128.**

Trait legality is no longer an accident of directory layout — a single validated
`trait_config.yaml` drives mint, swap, and economy:

- **Body-affinity audit** — derived the per-value body-compatibility matrix from the full 3,535-edition mint history (per-edition deduped, burned included), with a human-review report gate ([#122](https://github.com/Team-Hamsa/LFG/pull/122)). Closed #28 by proving the "legacy exclusion rules" never existed in code.
- **Rules engine** — declarative `trait_config.yaml` (layer z-order, per-value z-overrides absorbing TOP_TRAITS, owner-confirmed affinity, swap matrix, exclusion machinery) with strict load-time validation and a pre-commit/CI validation CLI ([#123](https://github.com/Team-Hamsa/LFG/pull/123)).
- **Mint + compose integration** — affinity-filtered selection that fails loud on over-constrained layers; compose ordering flows through config z-values; 200-mint property test ([#127](https://github.com/Team-Hamsa/LFG/pull/127)).
- **Cross-body trait swapping** — Ape↔Skeleton headwear/clothing, Straight↔Curved everything-but-clothing, universal Accessory/Back; enforced per-trait at the API, mirrored in the UI, and applied identically to economy equip/assemble ([#128](https://github.com/Team-Hamsa/LFG/pull/128)). Closes #30.
- **Shared trait layers** — byte-identical universal art (52 Backgrounds + 4 Backs) physically deduplicated into `layers/shared/` via an idempotent verify-then-move migration with atomic seasons-manifest rewrite ([#126](https://github.com/Team-Hamsa/LFG/pull/126)).

## Mainnet Launch Hardening

- Regular-key signing for the issuer (`SIGNING_ACCOUNT` override) (#112).
- `ECONOMY_ENABLED` flag to launch with the trait economy off (#113).
- Bithomp import filtered by collection issuer; census reconciled to 3,535 clean editions (#111).
- **Mainnet BRIX/XRP AMM pool live** and quoting for the trait-swap fee path; testnet pool tooling (`scripts/testnet_amm_setup.py`) closes [#26](https://github.com/Team-Hamsa/LFG/issues/26).
- **Mainnet cutover executed 2026-07-10** — audits passing (3,535 live editions,
  zero drift), local-first image archive, network-aware app database
  ([#167](https://github.com/Team-Hamsa/LFG/pull/167)) so testnet mints can't poison the mainnet edition counter.

## Standalone Web Surface

**Issue [#240](https://github.com/Team-Hamsa/LFG/issues/240) — PR [#242](https://github.com/Team-Hamsa/LFG/pull/242).**

The same vanilla-JS Activity now runs as a plain website at
[build.letseffinggo.com](https://build.letseffinggo.com): GitHub Pages serves the
client, the prod API answers cross-origin behind a `WEB_ALLOWED_ORIGINS` allowlist,
and a fourth auth arm (`platform="web"`) bootstraps a session from a XUMM SignIn —
the wallet *is* the user id, so every `@require_wallet` flow works unchanged.

- Mascot favicons, home-screen icons and a social share card ([#246](https://github.com/Team-Hamsa/LFG/pull/246)).
- Animated MP4 editions play as `<video>` everywhere — Activity, Telegram, grids ([#251](https://github.com/Team-Hamsa/LFG/pull/251), [#249](https://github.com/Team-Hamsa/LFG/pull/249), [#378](https://github.com/Team-Hamsa/LFG/pull/378)); native VP9-alpha WebM trait layers ([#296](https://github.com/Team-Hamsa/LFG/pull/296), [#297](https://github.com/Team-Hamsa/LFG/pull/297)) with a pre-push 1080×1080 guardrail ([#295](https://github.com/Team-Hamsa/LFG/pull/295)).
- **Session resume** after the Discord mobile webview kills the Activity on app-switch to Xaman — mint, swap, market, economy and shop flows all reattach ([#216](https://github.com/Team-Hamsa/LFG/issues/216), [#376](https://github.com/Team-Hamsa/LFG/pull/376)); mobile-primary Xaman deep links on every sign request ([#380](https://github.com/Team-Hamsa/LFG/pull/380)).
- Pending-offers tray: claim any outstanding gift/Closet/trait offer anytime ([#300](https://github.com/Team-Hamsa/LFG/pull/300), [#327](https://github.com/Team-Hamsa/LFG/pull/327), [#423](https://github.com/Team-Hamsa/LFG/pull/423)).

## X Integration

**Issue [#41](https://github.com/Team-Hamsa/LFG/issues/41) — PRs [#245](https://github.com/Team-Hamsa/LFG/pull/245), [#255](https://github.com/Team-Hamsa/LFG/pull/255), [#258](https://github.com/Team-Hamsa/LFG/pull/258), [#274](https://github.com/Team-Hamsa/LFG/pull/274), [#398](https://github.com/Team-Hamsa/LFG/pull/398).**

- Brand-account **auto-post on mint** (`run_x.py`) with a UTC-month post budget, dedup, and an admin runtime toggle; flag-gated (`X_ENABLED`), driven by server-side mint terminal events over the cross-surface firehose.
- **Share-on-X** buttons plus a per-NFT OG card page (branded PNG render, JS click-through forwarding so the X crawler stays on the card) and `?ref` share attribution ([#258](https://github.com/Team-Hamsa/LFG/pull/258), [#274](https://github.com/Team-Hamsa/LFG/pull/274), [#277](https://github.com/Team-Hamsa/LFG/pull/277)–[#280](https://github.com/Team-Hamsa/LFG/pull/280)).
- **Share-link mint attribution** — stashed ref recorded on mint, conversion metrics ([#393](https://github.com/Team-Hamsa/LFG/pull/393)); a share-intent beacon for exact giveaway eligibility ([#420](https://github.com/Team-Hamsa/LFG/pull/420)).
- Per-user **OAuth2 PKCE "Share from my account"** with Fernet-encrypted tokens at rest ([#252](https://github.com/Team-Hamsa/LFG/issues/252), [#398](https://github.com/Team-Hamsa/LFG/pull/398)).

## Sponsored Free Mint

**PR [#328](https://github.com/Team-Hamsa/LFG/pull/328) and follow-ons.**

A SourceTag-sponsored free-mint campaign: a wallet that has *never* submitted a
SourceTag-carrying transaction can claim one sponsored mint. Eligibility is
answered only from a history archive that has proven itself complete — every
reservation fails closed until an operator certifies the baseline.

- Durable burn worker + claim/debt tables, `/admin` Start/Stop, readiness audit; first campaign ran 2026-08-17 (34/100 slots, clean).
- **Archive continuity**: bounded gap catch-up after a listener restart instead of a full re-certification ([#353](https://github.com/Team-Hamsa/LFG/pull/353)), auto-recertify on campaign start ([#341](https://github.com/Team-Hamsa/LFG/pull/341)), self-healing auto catch-up on (re)subscribe ([#404](https://github.com/Team-Hamsa/LFG/pull/404)), full-baseline attestation ([#350](https://github.com/Team-Hamsa/LFG/pull/350)).
- Hardening from live incidents: one undeliverable claim must never disable admission ([#387](https://github.com/Team-Hamsa/LFG/pull/387), [#418](https://github.com/Team-Hamsa/LFG/pull/418)); destination-wallet pre-flight before any spend ([#432](https://github.com/Team-Hamsa/LFG/pull/432)); listener watchdog + bounded awaits ([#346](https://github.com/Team-Hamsa/LFG/pull/346)).

## BRIX Daily Drip

**Issue [#48](https://github.com/Team-Hamsa/LFG/issues/48) — PRs [#406](https://github.com/Team-Hamsa/LFG/pull/406), [#407](https://github.com/Team-Hamsa/LFG/pull/407), [#421](https://github.com/Team-Hamsa/LFG/pull/421).**

Holders earn **1 BRIX per unlisted live NFT per UTC day**, accrued in the DB and
paid on-chain by a dedicated distributor wallet only when explicitly claimed
(`/api/brix/claim`, Discord `/claim`).

- Double-accrual and double-claim are **structurally impossible** — enforced by sqlite primary keys and a partial unique index, not app logic; recovery confirms a claim only from the on-ledger memo and never guesses.
- **Epoch-accurate accrual from the archive** ([#421](https://github.com/Team-Hamsa/LFG/pull/421)): listed/unlisted state is replayed from `nft_events` to each epoch close — zero per-token RPCs — and an epoch is *deferred*, never silently zero-paid, unless the archive certifies. Gap reimbursement tooling backfilled 1.15M BRIX across 401 wallets on mainnet (2026-08-22).

## Staging/Prod Stacks + Ops Automation

- **Two pm2 stacks, branch-driven** (`main` = staging/testnet, `deploy` = prod/mainnet) with a polling deployer and a confirmed-fast-forward `promote.sh` ([#223](https://github.com/Team-Hamsa/LFG/issues/223), [#230](https://github.com/Team-Hamsa/LFG/pull/230)); abandoned pre-money sessions expire so the deployer drain can complete ([#435](https://github.com/Team-Hamsa/LFG/pull/435)).
- Nightly `backfill_market` drift sweep ([#375](https://github.com/Team-Hamsa/LFG/pull/375)), economy reconcile+audit crons with a Discord webhook ([#352](https://github.com/Team-Hamsa/LFG/pull/352)), public-edge funnel health monitor ([#344](https://github.com/Team-Hamsa/LFG/pull/344)), listener end-to-end harness ([#374](https://github.com/Team-Hamsa/LFG/pull/374)).
- **Living SourceTag badge** — tagged volume, unique wallets, XRP payment volume in/out/other pushed nightly to `main` via the Contents API ([#321](https://github.com/Team-Hamsa/LFG/pull/321), [#417](https://github.com/Team-Hamsa/LFG/pull/417), [#422](https://github.com/Team-Hamsa/LFG/pull/422)).
- Test suite isolated from the deployed `.env` (`LFG_SKIP_DOTENV`, [#368](https://github.com/Team-Hamsa/LFG/pull/368)); worktree-aware pre-push gate ([#373](https://github.com/Team-Hamsa/LFG/pull/373)).

## Payment & Signing Safety

- XUMM payloads **pinned to the session wallet** — any other Xaman wallet signing a mint/swap/buy is refused ([#314](https://github.com/Team-Hamsa/LFG/pull/314), [#348](https://github.com/Team-Hamsa/LFG/pull/348)); open payloads are cancelled on session cancel ([#360](https://github.com/Team-Hamsa/LFG/pull/360)).
- XUMM **429 rate-limit spiral** stopped — 429 detection, event-driven status, a 15-minute expire on every builder ([#254](https://github.com/Team-Hamsa/LFG/pull/254), [#260](https://github.com/Team-Hamsa/LFG/pull/260)).
- Payments consumed by tx hash so duplicates/late payments become **mint credits**, never lost money ([#197](https://github.com/Team-Hamsa/LFG/pull/197)); authoritative synchronous **headroom reservation** for the collection cap ([#267](https://github.com/Team-Hamsa/LFG/pull/267)).
- Trait economy denominated in the real BRIX pair, not LFGO ([#308](https://github.com/Team-Hamsa/LFG/pull/308)); BRIX trait listings with an XRP→BRIX AMM on-ramp ([#248](https://github.com/Team-Hamsa/LFG/pull/248)) and the Shop's XRP fallback via AMM buyback ([#243](https://github.com/Team-Hamsa/LFG/pull/243)).
- **`SigningProvider` seam** with enforced provenance ([#433](https://github.com/Team-Hamsa/LFG/pull/433)) and a pre-submit `simulate` pre-flight on every backend-signed transaction — deterministic failures are refused before a fee is burned ([#438](https://github.com/Team-Hamsa/LFG/pull/438)).
- Marketplace: known-broker external listings ([#281](https://github.com/Team-Hamsa/LFG/pull/281)), native **buy offers / bids** ([#285](https://github.com/Team-Hamsa/LFG/pull/285), [#287](https://github.com/Team-Hamsa/LFG/pull/287)), browse build-out ([#284](https://github.com/Team-Hamsa/LFG/pull/284)); **burn-to-mint** — burn M own NFTs for M cap-exempt fresh mints ([#397](https://github.com/Team-Hamsa/LFG/pull/397)).

## Identity, Profiles & Rarity

- First-class **user profiles** above per-platform identities ([#396](https://github.com/Team-Hamsa/LFG/pull/396)) and a `wallet_links` history + same-human bucket resolver ([#395](https://github.com/Team-Hamsa/LFG/pull/395)).
- Rarity **share-ceiling cap** on trait weights ([#394](https://github.com/Team-Hamsa/LFG/pull/394)); harvest burns now raise Shop prices ([#306](https://github.com/Team-Hamsa/LFG/pull/306)); the Shop ships **off by default** — the project never sells traits, users trade them ([#410](https://github.com/Team-Hamsa/LFG/pull/410)).

## Living README

The README maintains itself: auto-generated badge row ([#361](https://github.com/Team-Hamsa/LFG/pull/361)), roadmap block synced from `roadmap`-labelled issues ([#367](https://github.com/Team-Hamsa/LFG/pull/367)), feature-flag table generated from config defaults ([#370](https://github.com/Team-Hamsa/LFG/pull/370)), architecture diagram generated from code ([#371](https://github.com/Team-Hamsa/LFG/pull/371)), repository-layout and demo-GIF staleness guards in CI ([#369](https://github.com/Team-Hamsa/LFG/pull/369), [#372](https://github.com/Team-Hamsa/LFG/pull/372)) — and, as of this section, the merged changelog below.

## Merged changelog

Every pull request merged during the sprint, newest first. This block is
regenerated by `.github/workflows/build-log-sync.yml` on every merge into
`main` (plus nightly), so it can never fall behind the narrative above —
edit the sections above by hand, never this block.

<!-- changelog:start -->
_247 pull requests merged since 2026-06-21. Regenerated automatically on every merge — see `scripts/build_log_sync.py`._

### August 2026 — 75 merged

- 2026-08-24 · [#442](https://github.com/Team-Hamsa/LFG/pull/442) feat(brix): in-Activity BRIX TrustSet flow (closes #441)
- 2026-08-24 · [#434](https://github.com/Team-Hamsa/LFG/pull/434) feat(brix): Activity BRIX drip card (#48)
- 2026-08-23 · [#437](https://github.com/Team-Hamsa/LFG/pull/437) feat(market): Buy now on externally-listed NFTs via auto-calculated clearing price
- 2026-08-23 · [#439](https://github.com/Team-Hamsa/LFG/pull/439) feat(docs): self-updating hackathon build log — merged-PR changelog + narrative back-fill
- 2026-08-23 · [#433](https://github.com/Team-Hamsa/LFG/pull/433) refactor(signing): extract a SigningProvider seam with enforced provenance (#399)
- 2026-08-23 · [#438](https://github.com/Team-Hamsa/LFG/pull/438) feat(xrpl): pre-submit simulate pre-flight before backend-signed submits
- 2026-08-23 · [#435](https://github.com/Team-Hamsa/LFG/pull/435) fix(service): expire abandoned pre-money sessions so the deployer drain can complete
- 2026-08-23 · [#436](https://github.com/Team-Hamsa/LFG/pull/436) fix(closet): backstop sweep reconciles pending_accept Closets missed by the listener (#382)
- 2026-08-23 · [#430](https://github.com/Team-Hamsa/LFG/pull/430) fix(economy): stop the listener keying a Closet row to the issuer (#383)
- 2026-08-23 · [#431](https://github.com/Team-Hamsa/LFG/pull/431) docs(README): document the mainnet regular-key signing model
- 2026-08-23 · [#432](https://github.com/Team-Hamsa/LFG/pull/432) feat(mint): pre-flight the destination wallet before any spend (#388, #408)
- 2026-08-23 · [#429](https://github.com/Team-Hamsa/LFG/pull/429) fix(listener): stop the legacy-harvest-upgrade race from poisoning supply_changes
- 2026-08-22 · [#425](https://github.com/Team-Hamsa/LFG/pull/425) fix(history): credit authorized-minter mints to tx.Account, not Issuer
- 2026-08-22 · [#422](https://github.com/Team-Hamsa/LFG/pull/422) feat(metrics): split tagged XRP Payment volume in/out/other
- 2026-08-22 · [#409](https://github.com/Team-Hamsa/LFG/pull/409) fix(build): let blank characters be selected in the GO picker
- 2026-08-22 · [#416](https://github.com/Team-Hamsa/LFG/pull/416) perf(brix): parallelize nightly drip listing lookups (#411)
- 2026-08-22 · [#417](https://github.com/Team-Hamsa/LFG/pull/417) fix(metrics): keep project wallets excluded across a config repoint (#413, #414)
- 2026-08-22 · [#419](https://github.com/Team-Hamsa/LFG/pull/419) fix(closet): Closet screen self-heals a listener-missed accept
- 2026-08-22 · [#421](https://github.com/Team-Hamsa/LFG/pull/421) feat(brix): epoch-accurate accrual from the archive + gap reimbursement (#411, #412)
- 2026-08-22 · [#418](https://github.com/Team-Hamsa/LFG/pull/418) fix(sponsored): one undeliverable claim must not disable free-mint admission
- 2026-08-22 · [#423](https://github.com/Team-Hamsa/LFG/pull/423) fix(pending-offers): resolve soulbound Closet offers as kind=closet
- 2026-08-21 · [#420](https://github.com/Team-Hamsa/LFG/pull/420) feat(share): beacon Share-on-X presses for exact giveaway eligibility
- 2026-08-19 · [#410](https://github.com/Team-Hamsa/LFG/pull/410) feat(shop): stop selling traits from the project by default
- 2026-08-19 · [#407](https://github.com/Team-Hamsa/LFG/pull/407) feat(brix): daily-drip claim flow, endpoints, and Discord /claim (#48)
- 2026-08-19 · [#406](https://github.com/Team-Hamsa/LFG/pull/406) feat(brix): daily drip accrual store, evaluation, and audit (#48)
- 2026-08-18 · [#404](https://github.com/Team-Hamsa/LFG/pull/404) feat(listener): self-heal sponsored-mint continuity — auto bounded catch-up on (re)subscribe (#402)
- 2026-08-18 · [#403](https://github.com/Team-Hamsa/LFG/pull/403) perf(backfill): skip pre-gap burned tokens in bounded catch-up (#381)
- 2026-08-18 · [#401](https://github.com/Team-Hamsa/LFG/pull/401) Tag @letseffinggo in Share-on-X post text
- 2026-08-18 · [#398](https://github.com/Team-Hamsa/LFG/pull/398) feat(x): per-user OAuth2 PKCE — share from my account (#252)
- 2026-08-18 · [#397](https://github.com/Team-Hamsa/LFG/pull/397) feat(mint): burn-to-mint — burn M own NFTs for M cap-exempt fresh mints
- 2026-08-18 · [#396](https://github.com/Team-Hamsa/LFG/pull/396) feat(identity): first-class user profiles above per-platform identities
- 2026-08-18 · [#395](https://github.com/Team-Hamsa/LFG/pull/395) feat(identity): wallet_links history + same-human bucket resolver
- 2026-08-18 · [#394](https://github.com/Team-Hamsa/LFG/pull/394) feat(rarity): share-ceiling cap on trait weights
- 2026-08-18 · [#393](https://github.com/Team-Hamsa/LFG/pull/393) feat(mint): share-link mint attribution + conversion metrics (#273)
- 2026-08-18 · [#392](https://github.com/Team-Hamsa/LFG/pull/392) feat(events): publish per-unit mint.completed for bulk-mint units
- 2026-08-18 · [#390](https://github.com/Team-Hamsa/LFG/pull/390) fix(closet): surface transient Closet-mint failures as retryable instead of dead 502
- 2026-08-18 · [#389](https://github.com/Team-Hamsa/LFG/pull/389) fix(xrpl): retry malformed result-less confirm-poll bodies before failing closed
- 2026-08-18 · [#387](https://github.com/Team-Hamsa/LFG/pull/387) fix(sponsored): one undeliverable claim must not disable admission
- 2026-08-17 · [#384](https://github.com/Team-Hamsa/LFG/pull/384) fix(builder): resolve /api/layer like a real compose
- 2026-08-17 · [#380](https://github.com/Team-Hamsa/LFG/pull/380) Mobile-primary Xaman deep links on every sign request
- 2026-08-17 · [#379](https://github.com/Team-Hamsa/LFG/pull/379) feat(economy): batch harvest — multi-select GO picker + stacked server harvests
- 2026-08-17 · [#378](https://github.com/Team-Hamsa/LFG/pull/378) Animated-trait grid strategy: static grids, badged tiles, lazy detail-view video (#298)
- 2026-08-17 · [#377](https://github.com/Team-Hamsa/LFG/pull/377) feat(index): carry the metadata video URL through onchain_nfts so cache-miss roster records stay animated
- 2026-08-17 · [#376](https://github.com/Team-Hamsa/LFG/pull/376) Session resume for swap/market/economy/shop flows after Activity relaunch
- 2026-08-16 · [#375](https://github.com/Team-Hamsa/LFG/pull/375) Scheduled nightly backfill_market sweep (#288)
- 2026-08-16 · [#374](https://github.com/Team-Hamsa/LFG/pull/374) test(listener): end-to-end harness for the _listen websocket loop
- 2026-08-16 · [#371](https://github.com/Team-Hamsa/LFG/pull/371) feat(assets): generate the README architecture diagram from code
- 2026-08-16 · [#373](https://github.com/Team-Hamsa/LFG/pull/373) fix(gate): resolve pre-push venv via scripts/venv-python so worktrees run the real gate
- 2026-08-16 · [#370](https://github.com/Team-Hamsa/LFG/pull/370) feat(readme): generate feature-flag table from config defaults
- 2026-08-16 · [#372](https://github.com/Team-Hamsa/LFG/pull/372) feat(ci): demo-GIF staleness guard for README walkthroughs
- 2026-08-16 · [#369](https://github.com/Team-Hamsa/LFG/pull/369) feat(ci): guard the README repository-layout tree against drift
- 2026-08-16 · [#368](https://github.com/Team-Hamsa/LFG/pull/368) Isolate the pytest suite from the deployed .env (LFG_SKIP_DOTENV) (#323)
- 2026-08-16 · [#367](https://github.com/Team-Hamsa/LFG/pull/367) feat(readme): roadmap block synced from roadmap-labelled issues
- 2026-08-16 · [#366](https://github.com/Team-Hamsa/LFG/pull/366) Reverify hardening: shared endpoint authority, account-drift refusal, asyncio.run (#342)
- 2026-08-16 · [#365](https://github.com/Team-Hamsa/LFG/pull/365) refactor(sponsored-mint): de-hardcode operator wallets from readiness audit
- 2026-08-16 · [#364](https://github.com/Team-Hamsa/LFG/pull/364) test(mint): pin the sponsored prepared-mint resume path, then extract the shared post-mint tail
- 2026-08-16 · [#363](https://github.com/Team-Hamsa/LFG/pull/363) refactor(sponsored-mint): remove unreachable helpers and re-anchor the archive-scan canary
- 2026-08-16 · [#362](https://github.com/Team-Hamsa/LFG/pull/362) perf(listener): batch the eligibility-archive commit off the serial stream loop
- 2026-08-16 · [#361](https://github.com/Team-Hamsa/LFG/pull/361) feat(readme): auto-generated badge row
- 2026-08-16 · [#360](https://github.com/Team-Hamsa/LFG/pull/360) feat(xumm): cancel the open XUMM payload on mint/bulk/swap session cancel
- 2026-08-16 · [#359](https://github.com/Team-Hamsa/LFG/pull/359) fix(data): correction tooling for misspelled 'Iridescent Skeleton' Body value (#301)
- 2026-08-16 · [#358](https://github.com/Team-Hamsa/LFG/pull/358) fix(swap): precheck issuer BRIX trustline before burn, fall back to XRP (#166)
- 2026-08-15 · [#341](https://github.com/Team-Hamsa/LFG/pull/341) feat(sponsored): auto-recertify the eligibility archive on campaign start (#340)
- 2026-08-15 · [#344](https://github.com/Team-Hamsa/LFG/pull/344) feat(ops): public-edge funnel health-check monitor
- 2026-08-15 · [#321](https://github.com/Team-Hamsa/LFG/pull/321) feat(metrics): living SourceTag badge — tagged volume + unique wallets
- 2026-08-15 · [#357](https://github.com/Team-Hamsa/LFG/pull/357) fix(market): serve a resolvable trait image URL in Mine
- 2026-08-07 · [#343](https://github.com/Team-Hamsa/LFG/pull/343) fix(swap): log layer pre-check failures so missing-layer swaps are visible server-side
- 2026-08-07 · [#353](https://github.com/Team-Hamsa/LFG/pull/353) feat(history): bounded gap catch-up so a listener restart doesn't force a full re-certification
- 2026-08-06 · [#352](https://github.com/Team-Hamsa/LFG/pull/352) feat(economy): stop re-freezing genesis — record out-of-band burns as shrinkage, automate reconcile+audit
- 2026-08-06 · [#351](https://github.com/Team-Hamsa/LFG/pull/351) fix(economy): honest Build UI reconcile for fail-closed equip outcomes
- 2026-08-06 · [#350](https://github.com/Team-Hamsa/LFG/pull/350) fix(sponsored-mint): certification must attest the full baseline source sweep (#331)
- 2026-08-06 · [#349](https://github.com/Team-Hamsa/LFG/pull/349) fix(mint): persist the image-archive staging token on sponsored claims so resumed mints promote their art
- 2026-08-06 · [#348](https://github.com/Team-Hamsa/LFG/pull/348) fix(mint): fail loudly on payment signed by non-session wallet
- 2026-08-06 · [#346](https://github.com/Team-Hamsa/LFG/pull/346) fix(listener): watchdog + bounded awaits so a wedged stream reconnects
- 2026-08-03 · [#339](https://github.com/Team-Hamsa/LFG/pull/339) fix(sponsored): burn worker dies on first idle poll under Python 3.10

### July 2026 — 141 merged

- 2026-07-31 · [#338](https://github.com/Team-Hamsa/LFG/pull/338) fix(archive): never record an unclearable unbounded continuity gap
- 2026-07-31 · [#328](https://github.com/Team-Hamsa/LFG/pull/328) feat(mint): SourceTag-sponsored free mint campaign
- 2026-07-24 · [#326](https://github.com/Team-Hamsa/LFG/pull/326) fix(economy): refresh GO-grid thumbnails after assemble/harvest via inline index stamp
- 2026-07-24 · [#327](https://github.com/Team-Hamsa/LFG/pull/327) fix(offers): show trait art + slot/value for extracted-trait pending offers
- 2026-07-24 · [#324](https://github.com/Team-Hamsa/LFG/pull/324) Log the harvest accept-payload failure; correct a stale journal-status comment
- 2026-07-24 · [#325](https://github.com/Team-Hamsa/LFG/pull/325) fix(scripts): webm converter skips the derived .thumbs/ preview tier
- 2026-07-24 · [#295](https://github.com/Team-Hamsa/LFG/pull/295) Guardrail: fail pre-push on non-1080x1080 trait layers
- 2026-07-22 · [#297](https://github.com/Team-Hamsa/LFG/pull/297) feat(scripts): batch GIF/MP4 → VP9-alpha WebM trait-layer converter
- 2026-07-22 · [#320](https://github.com/Team-Hamsa/LFG/pull/320) Fix wrong thumbnails: shared image URLs and stale archived art after economy ops
- 2026-07-22 · [#319](https://github.com/Team-Hamsa/LFG/pull/319) Assemble builder UI: pick your blank, body, and traits (Phase B)
- 2026-07-22 · [#309](https://github.com/Team-Hamsa/LFG/pull/309) feat(economy): blank-harvest modify-in-place model (Phase A)
- 2026-07-22 · [#314](https://github.com/Team-Hamsa/LFG/pull/314) fix(payments): pin the XUMM payment payload to the session wallet
- 2026-07-22 · [#317](https://github.com/Team-Hamsa/LFG/pull/317) test(conftest): pin the shop/bulk-UI env defaults like the economy ones
- 2026-07-22 · [#318](https://github.com/Team-Hamsa/LFG/pull/318) fix(closet): keep harvested "None" traits visible in the Closet
- 2026-07-22 · [#313](https://github.com/Team-Hamsa/LFG/pull/313) Build panel: batch trait changes behind one Save
- 2026-07-21 · [#312](https://github.com/Team-Hamsa/LFG/pull/312) fix(tests): make config-default assertions independent of the ambient .env
- 2026-07-21 · [#310](https://github.com/Team-Hamsa/LFG/pull/310) fix(swap): unique CDN stem per swap so post-swap previews aren't cached art
- 2026-07-21 · [#308](https://github.com/Team-Hamsa/LFG/pull/308) fix(market/shop): denominate trait economy in the real BRIX pair, not TOKEN_* (LFGO)
- 2026-07-21 · [#307](https://github.com/Team-Hamsa/LFG/pull/307) Fire-and-forget stacked harvests
- 2026-07-21 · [#306](https://github.com/Team-Hamsa/LFG/pull/306) fix(shop): harvest burns now raise trait prices — rarity live-count sees economy burns
- 2026-07-21 · [#304](https://github.com/Team-Hamsa/LFG/pull/304) Layer thumbnail tier: fix broken animated trait previews (Diamond/Irridescent)
- 2026-07-21 · [#302](https://github.com/Team-Hamsa/LFG/pull/302) fix(activity): hide Dressing Room UI behind the Closet gate
- 2026-07-21 · [#303](https://github.com/Team-Hamsa/LFG/pull/303) fix(web): trait-layer images blank on the standalone web surface
- 2026-07-21 · [#294](https://github.com/Team-Hamsa/LFG/pull/294) fix(nft_index): app DB without LFG table no longer crashes reconcile
- 2026-07-21 · [#299](https://github.com/Team-Hamsa/LFG/pull/299) fix(shop): payment-path detection must use the shop offer's currency pair
- 2026-07-21 · [#300](https://github.com/Team-Hamsa/LFG/pull/300) Pending-offers tray: claim any outstanding gift offer anytime (#218)
- 2026-07-21 · [#296](https://github.com/Team-Hamsa/LFG/pull/296) feat(layers): native VP9-alpha WebM trait layers
- 2026-07-20 · [#293](https://github.com/Team-Hamsa/LFG/pull/293) fix(activity): bump client cache-buster so #290 pay-page UI loads
- 2026-07-20 · [#291](https://github.com/Team-Hamsa/LFG/pull/291) Trait Shop: fix broken catalog thumbnails + viewport-filling Activity grid
- 2026-07-20 · [#292](https://github.com/Team-Hamsa/LFG/pull/292) feat(shop): client-side catalog filtering — slot chips, live search, sort (#217 follow-up)
- 2026-07-20 · [#290](https://github.com/Team-Hamsa/LFG/pull/290) feat(mint): move bulk-mint quantity onto the Mint pay page (#215 UX revision)
- 2026-07-20 · [#287](https://github.com/Team-Hamsa/LFG/pull/287) feat(marketplace): bids UI + dev-mode mock parity + bid cancel (#283)
- 2026-07-20 · [#289](https://github.com/Team-Hamsa/LFG/pull/289) Genesis-growth reconciler: fix "character has no known genesis edition" for listener-missed mints
- 2026-07-20 · [#285](https://github.com/Team-Hamsa/LFG/pull/285) feat(marketplace): native buy offers (bids) — backend (#283)
- 2026-07-20 · [#286](https://github.com/Team-Hamsa/LFG/pull/286) fix(index): repoint index images to CDN + upsert clobber-guard
- 2026-07-20 · [#284](https://github.com/Team-Hamsa/LFG/pull/284) feat(marketplace): browse UX build-out — rarity sort, pagination, detail view, listed-by-me (#203)
- 2026-07-20 · [#282](https://github.com/Team-Hamsa/LFG/pull/282) fix(market): show edition # (not hex) + working image for listings with unparseable metadata
- 2026-07-20 · [#281](https://github.com/Team-Hamsa/LFG/pull/281) feat(marketplace): surface known-broker external listings in browse, read-only (#131)
- 2026-07-19 · [#280](https://github.com/Team-Hamsa/LFG/pull/280) fix(share): tweet copy LFG + 🧱; card title doubles as click-through CTA
- 2026-07-19 · [#279](https://github.com/Team-Hamsa/LFG/pull/279) fix(share): scale share-card number to fill the column (align with logo)
- 2026-07-19 · [#278](https://github.com/Team-Hamsa/LFG/pull/278) fix(share): card fonts never loaded — embed as data URIs + force-load before screenshot
- 2026-07-19 · [#277](https://github.com/Team-Hamsa/LFG/pull/277) fix(share): card renderer dies under pm2 — strip NODE_CHANNEL_* from subprocess env
- 2026-07-18 · [#274](https://github.com/Team-Hamsa/LFG/pull/274) feat(share): X share-card click-through forwarding + share attribution (#41 follow-on)
- 2026-07-18 · [#247](https://github.com/Team-Hamsa/LFG/pull/247) Build panel UX: labeled GO picker, compatibility filtering, server-side assemble prefill
- 2026-07-18 · [#272](https://github.com/Team-Hamsa/LFG/pull/272) Bulk mint UI in the Activity behind BULK_MINT_UI_ENABLED (#215 follow-up)
- 2026-07-18 · [#269](https://github.com/Team-Hamsa/LFG/pull/269) fix(mint): relocate Infernal Wings from Accessory to Back layer (#268 follow-up)
- 2026-07-18 · [#271](https://github.com/Team-Hamsa/LFG/pull/271) fix(user_db): guard conn.close() so a failed connect surfaces the real error
- 2026-07-17 · [#270](https://github.com/Team-Hamsa/LFG/pull/270) fix(mint): one canonical trait dict for compose and metadata, enforced
- 2026-07-17 · [#267](https://github.com/Team-Hamsa/LFG/pull/267) feat(supply): authoritative synchronous headroom reservation for the collection cap
- 2026-07-17 · [#266](https://github.com/Team-Hamsa/LFG/pull/266) fix(webapp): OG card image must be fetchable http(s) — legacy ipfs metadata fallback (#41)
- 2026-07-17 · [#265](https://github.com/Team-Hamsa/LFG/pull/265) fix(swap): persist remints past the burn, confirm offers before failing, guard stale pointers
- 2026-07-17 · [#264](https://github.com/Team-Hamsa/LFG/pull/264) fix(mint,swap): fail fast when the XUMM payment payload was never created
- 2026-07-17 · [#261](https://github.com/Team-Hamsa/LFG/pull/261) fix(bulk-mint): idempotent re-offer + payment/persistence durability hardening
- 2026-07-17 · [#248](https://github.com/Team-Hamsa/LFG/pull/248) feat(market): BRIX-denominated trait listings + XRP→BRIX AMM on-ramp (#239)
- 2026-07-17 · [#259](https://github.com/Team-Hamsa/LFG/pull/259) fix(listener,history): ignore tec-failed transactions in event derivation and index applies
- 2026-07-17 · [#260](https://github.com/Team-Hamsa/LFG/pull/260) fix(xumm): stop open-payload pileup behind the 'Max payloads exceeded' swap failures
- 2026-07-17 · [#258](https://github.com/Team-Hamsa/LFG/pull/258) feat(webapp): Share-on-X buttons + OG card page — X integration PR-3 (#41)
- 2026-07-17 · [#256](https://github.com/Team-Hamsa/LFG/pull/256) feat(discord): skip global command sync when the tree is unchanged
- 2026-07-17 · [#249](https://github.com/Team-Hamsa/LFG/pull/249) fix(telegram): send animated MP4 NFTs as playing videos, not static posters
- 2026-07-17 · [#255](https://github.com/Team-Hamsa/LFG/pull/255) feat(x): admin runtime toggle + server-side mint terminal events — X integration PR-2 (#41)
- 2026-07-17 · [#257](https://github.com/Team-Hamsa/LFG/pull/257) fix(telegram): include Ed25519 signature field in initData HMAC validation
- 2026-07-17 · [#254](https://github.com/Team-Hamsa/LFG/pull/254) Stop the XUMM 429 rate-limit spiral: detect 429s, event-driven payload status, sign-in creation guard
- 2026-07-17 · [#251](https://github.com/Team-Hamsa/LFG/pull/251) feat(activity): play animated MP4 NFTs as \<video\> instead of frozen stills
- 2026-07-17 · [#245](https://github.com/Team-Hamsa/LFG/pull/245) feat(x-bot): brand-account auto-post on mint — X integration PR-1 (#41)
- 2026-07-17 · [#246](https://github.com/Team-Hamsa/LFG/pull/246) Mascot favicons, homescreen icons, and social share card for the web surface
- 2026-07-17 · [#243](https://github.com/Team-Hamsa/LFG/pull/243) feat(shop): XRP payment fallback via AMM buyback (#238)
- 2026-07-16 · [#244](https://github.com/Team-Hamsa/LFG/pull/244) Build UI nitpicks: rename, back button, default GO, picker overlay, hide broken tiles
- 2026-07-16 · [#242](https://github.com/Team-Hamsa/LFG/pull/242) Standalone web surface: the Activity as a website at build.letseffinggo.com (#240)
- 2026-07-16 · [#237](https://github.com/Team-Hamsa/LFG/pull/237) fix(discord): retry command sync with Entry Point command on 50240 (#236)
- 2026-07-16 · [#230](https://github.com/Team-Hamsa/LFG/pull/230) ops: staging/prod stack split with branch-driven deploys (#223)
- 2026-07-16 · [#233](https://github.com/Team-Hamsa/LFG/pull/233) fix(nft-index): sticky is_burned — stale imports must never resurrect burned tokens
- 2026-07-16 · [#197](https://github.com/Team-Hamsa/LFG/pull/197) Consume payments by tx hash so duplicates/late payments become mint credits
- 2026-07-15 · [#213](https://github.com/Team-Hamsa/LFG/pull/213) feat(xumm): harden push delivery — observability, rotating token capture, honest UI, full coverage (#212)
- 2026-07-15 · [#225](https://github.com/Team-Hamsa/LFG/pull/225) feat(bulk-mint): pay once, durably mint+offer N NFTs (#215)
- 2026-07-15 · [#229](https://github.com/Team-Hamsa/LFG/pull/229) fix(index): clobber guard for failed metadata fetches + drop IPFS fetching
- 2026-07-15 · [#222](https://github.com/Team-Hamsa/LFG/pull/222) feat(shop): Trait Shop — BRIX-priced project trait listings (#217)
- 2026-07-15 · [#216](https://github.com/Team-Hamsa/LFG/pull/216) fix(activity): mint session resume + swap fee-QR regenerate/cancel
- 2026-07-14 · [#214](https://github.com/Team-Hamsa/LFG/pull/214) feat(dashboard): cancel and reschedule boosts (#205)
- 2026-07-13 · [#202](https://github.com/Team-Hamsa/LFG/pull/202) feat(dashboard): standalone rarity admin dashboard (v1)
- 2026-07-13 · [#201](https://github.com/Team-Hamsa/LFG/pull/201) fix(swap): allow one-sided None trait swaps
- 2026-07-13 · [#200](https://github.com/Team-Hamsa/LFG/pull/200) feat(swap): log trait-swap rejections server-side
- 2026-07-12 · [#195](https://github.com/Team-Hamsa/LFG/pull/195) chore: relocate loose root helpers into packages + tidy root
- 2026-07-12 · [#194](https://github.com/Team-Hamsa/LFG/pull/194) feat(activity): /api/health + drain-before-restart hook (no more mid-mint kills)
- 2026-07-12 · [#192](https://github.com/Team-Hamsa/LFG/pull/192) docs: redesign repo landing page (hero, living dashboard, demo grid, declutter)
- 2026-07-12 · [#191](https://github.com/Team-Hamsa/LFG/pull/191) fix(market): reject expired sell offers + close listing on tecEXPIRED (#183)
- 2026-07-12 · [#189](https://github.com/Team-Hamsa/LFG/pull/189) fix(economy): per-owner Closet read-modify-write lock (#180)
- 2026-07-12 · [#190](https://github.com/Team-Hamsa/LFG/pull/190) fix(economy): hardening bundle — extract/equip/mirror_pending + economy backfill (#184)
- 2026-07-12 · [#188](https://github.com/Team-Hamsa/LFG/pull/188) fix(economy): indeterminate on-chain outcome taxonomy, no blind resubmit (#179)
- 2026-07-12 · [#186](https://github.com/Team-Hamsa/LFG/pull/186) fix(economy): issuer-gate apply_economy_tx + foreign supply_changes purge script (#178)
- 2026-07-12 · [#187](https://github.com/Team-Hamsa/LFG/pull/187) fix(economy): assert ECONOMY_NETWORK==XRPL_NETWORK + default ECONOMY_ENABLED=0 (#182)
- 2026-07-12 · [#193](https://github.com/Team-Hamsa/LFG/pull/193) fix(rarity): Laplace-smooth trait shares so new bodies don't snowball (milady identical mints)
- 2026-07-11 · [#177](https://github.com/Team-Hamsa/LFG/pull/177) fix(market): invalidate browse cache on listing write/close
- 2026-07-11 · [#167](https://github.com/Team-Hamsa/LFG/pull/167) fix(db): network-aware app DB path (testnet mints poisoned the mainnet edition counter)
- 2026-07-11 · [#176](https://github.com/Team-Hamsa/LFG/pull/176) fix(rarity): weighted_pick denominator spans the whole body/category population (ape Star-eyes bug)
- 2026-07-11 · [#175](https://github.com/Team-Hamsa/LFG/pull/175) fix(ape): mask the injected nose on melt/xray bodies
- 2026-07-11 · [#174](https://github.com/Team-Hamsa/LFG/pull/174) feat(scripts): gifski pipeline for animated trait layers
- 2026-07-11 · [#171](https://github.com/Team-Hamsa/LFG/pull/171) feat(traits): register milady as a fifth body type
- 2026-07-11 · [#172](https://github.com/Team-Hamsa/LFG/pull/172) fix(mint): fresh mints upload to foldered CDN layout \<edition\>/\<edition\>_0.*
- 2026-07-11 · [#170](https://github.com/Team-Hamsa/LFG/pull/170) feat(swap): legacy apes get faces on their first swap (#168)
- 2026-07-11 · [#169](https://github.com/Team-Hamsa/LFG/pull/169) fix(images): update the local image archive after confirmed swaps/mints
- 2026-07-11 · [#165](https://github.com/Team-Hamsa/LFG/pull/165) fix(activity): roster never fetches metadata inline — synthesize misses from the index
- 2026-07-11 · [#164](https://github.com/Team-Hamsa/LFG/pull/164) fix(activity): path-less ipfs CID image URLs + app.js cache-bust to v15
- 2026-07-11 · [#162](https://github.com/Team-Hamsa/LFG/pull/162) fix(activity): serve swapper roster + all NFT images from local data
- 2026-07-10 · [#161](https://github.com/Team-Hamsa/LFG/pull/161) perf(activity): 256px WebP thumbnails for roster/grid tiles
- 2026-07-10 · [#160](https://github.com/Team-Hamsa/LFG/pull/160) fix(leaderboard): count legacy swaps from the burn side (burned tokens are the swap record)
- 2026-07-10 · [#158](https://github.com/Team-Hamsa/LFG/pull/158) feat(images): evolution-history archive — recompose every prior version of each edition
- 2026-07-10 · [#159](https://github.com/Team-Hamsa/LFG/pull/159) fix(history): cross-network rederive resolves issuers from the target index
- 2026-07-10 · [#157](https://github.com/Team-Hamsa/LFG/pull/157) fix(leaderboard): count legacy burn+remint swaps as swaps; gate builds on the assemble memo
- 2026-07-10 · [#156](https://github.com/Team-Hamsa/LFG/pull/156) fix(activity): local-first image archive — rebuild missing collection art, serve from disk (#153)
- 2026-07-10 · [#155](https://github.com/Team-Hamsa/LFG/pull/155) fix(mint): mint as the NFT collection issuer, not the LFGO token issuer
- 2026-07-10 · [#154](https://github.com/Team-Hamsa/LFG/pull/154) fix(activity): mainnet IPFS roster — image-proxy gateway allowlist + uri_hex metadata cache (#153)
- 2026-07-10 · [#151](https://github.com/Team-Hamsa/LFG/pull/151) fix(economy): phase-aware _sync_then_persist — distinguish ledger-commit vs DB-mirror failure (#107)
- 2026-07-10 · [#150](https://github.com/Team-Hamsa/LFG/pull/150) chore(marketplace): hardening/polish minors from the #130 follow-up bucket
- 2026-07-10 · [#149](https://github.com/Team-Hamsa/LFG/pull/149) fix(marketplace): surface bad-price errors in buy flow instead of a dead click (#133)
- 2026-07-10 · [#148](https://github.com/Team-Hamsa/LFG/pull/148) fix(activity): back/cancel on the mint pay screen, releasing the mint lock (#141)
- 2026-07-10 · [#147](https://github.com/Team-Hamsa/LFG/pull/147) fix(swap): reject swapping empty (None) trait slots (#146)
- 2026-07-09 · [#144](https://github.com/Team-Hamsa/LFG/pull/144) feat(memos): provenance Memos on every XRPL transaction (#54)
- 2026-07-09 · [#145](https://github.com/Team-Hamsa/LFG/pull/145) fix(traits): let apes mint their own face traits (#38)
- 2026-07-09 · [#143](https://github.com/Team-Hamsa/LFG/pull/143) feat(marketplace): MARKET_ENABLED flag to gate the marketplace off for MVP launch
- 2026-07-08 · [#140](https://github.com/Team-Hamsa/LFG/pull/140) audit(traits): definitive DB→local-file reconciliation + ape structural backfill (#137)
- 2026-07-07 · [#139](https://github.com/Team-Hamsa/LFG/pull/139) feat(xumm): push sign requests to Xaman via user_token (#135)
- 2026-07-06 · [#134](https://github.com/Team-Hamsa/LFG/pull/134) fix(market): QR encodes Xaman deep link, not XUMM's qr_png image url
- 2026-07-06 · [#138](https://github.com/Team-Hamsa/LFG/pull/138) fix(swap): skip self-directed replacement offer for issuer-recipient swaps (#136)
- 2026-07-06 · [#132](https://github.com/Team-Hamsa/LFG/pull/132) fix(market): render whole-XRP prices in fixed-point, not scientific notation
- 2026-07-06 · [#129](https://github.com/Team-Hamsa/LFG/pull/129) feat: in-app P2P marketplace — characters + trait tokens (#44)
- 2026-07-05 · [#126](https://github.com/Team-Hamsa/LFG/pull/126) feat: physical layers/shared/ for universal trait values
- 2026-07-05 · [#128](https://github.com/Team-Hamsa/LFG/pull/128) feat: cross-body trait swapping per compatibility matrix (#30)
- 2026-07-05 · [#127](https://github.com/Team-Hamsa/LFG/pull/127) feat: mint + compose consume the trait rules engine (#40)
- 2026-07-05 · [#123](https://github.com/Team-Hamsa/LFG/pull/123) feat: trait rules engine — config, queries, validation (#40)
- 2026-07-04 · [#122](https://github.com/Team-Hamsa/LFG/pull/122) feat: body-affinity audit — derive historical trait/body matrix (#28)
- 2026-07-04 · [#121](https://github.com/Team-Hamsa/LFG/pull/121) fix(leaderboard): bump asset cache-busters missed in #120
- 2026-07-04 · [#120](https://github.com/Team-Hamsa/LFG/pull/120) feat(leaderboard): two-tier category/board selector
- 2026-07-04 · [#119](https://github.com/Team-Hamsa/LFG/pull/119) fix(history): survive clio rate limits in history backfill
- 2026-07-04 · [#118](https://github.com/Team-Hamsa/LFG/pull/118) feat: ledger history database + Activity leaderboard (#48-adjacent)
- 2026-07-03 · [#117](https://github.com/Team-Hamsa/LFG/pull/117) feat(seasons): rebuild manifest from all-seasons premiere CSV (#114)
- 2026-07-03 · [#116](https://github.com/Team-Hamsa/LFG/pull/116) fix(seasons): disable_season pre-inserts disabled rows for unseen traits
- 2026-07-03 · [#115](https://github.com/Team-Hamsa/LFG/pull/115) feat(seasons): season sidecar manifest + Season 3 mint exclusion (#114)
- 2026-07-02 · [#110](https://github.com/Team-Hamsa/LFG/pull/110) feat(compose): ape face rule — nose injection + melt-ape masking
- 2026-07-02 · [#113](https://github.com/Team-Hamsa/LFG/pull/113) feat(service): ECONOMY_ENABLED flag — launch with the Closet/trait economy off
- 2026-07-02 · [#112](https://github.com/Team-Hamsa/LFG/pull/112) feat(xrpl_ops): SIGNING_ACCOUNT override for mainnet regular-key signing
- 2026-07-02 · [#111](https://github.com/Team-Hamsa/LFG/pull/111) fix(import_bithomp_csv): filter Bithomp CSV rows by collection issuer

### June 2026 — 31 merged

- 2026-06-28 · [#109](https://github.com/Team-Hamsa/LFG/pull/109) fix(xrpl): default nft_info/nft_exists to a clio endpoint
- 2026-06-28 · [#108](https://github.com/Team-Hamsa/LFG/pull/108) fix(closet): self-heal pending Closet with no offer_id so the accept QR re-shows
- 2026-06-27 · [#106](https://github.com/Team-Hamsa/LFG/pull/106) feat(traits): Dress-up Phase 4 — tradeable trait NFTokens (extract/deposit) (#66)
- 2026-06-27 · [#105](https://github.com/Team-Hamsa/LFG/pull/105) feat(closet): standalone Closet issuance + Bucket→Closet rename
- 2026-06-27 · [#104](https://github.com/Team-Hamsa/LFG/pull/104) fix(economy): ensure Bucket exists on-ledger before harvest burn (#101)
- 2026-06-27 · [#103](https://github.com/Team-Hamsa/LFG/pull/103) fix(activity): guard layer requests against incomplete NFT metadata (#100)
- 2026-06-26 · [#102](https://github.com/Team-Hamsa/LFG/pull/102) fix(swap+discord): self-issuer BRIX burn no-op + guild-scoped command sync (#99)
- 2026-06-26 · [#98](https://github.com/Team-Hamsa/LFG/pull/98) feat(telegram): Mini App auth + launch (Part A, feature-flagged) (#89)
- 2026-06-26 · [#97](https://github.com/Team-Hamsa/LFG/pull/97) feat(firehose): announce swap + economy interactions across surfaces (#91)
- 2026-06-26 · [#96](https://github.com/Team-Hamsa/LFG/pull/96) feat(telegram): chat-style trait swapper via inline keyboards (#88)
- 2026-06-26 · [#95](https://github.com/Team-Hamsa/LFG/pull/95) feat(announce): show the minter's handle instead of "a user" (#85)
- 2026-06-26 · [#94](https://github.com/Team-Hamsa/LFG/pull/94) feat(accounts): unified wallet-keyed accounts — inverse lookup, display handles, cross-surface link (#90)
- 2026-06-26 · [#93](https://github.com/Team-Hamsa/LFG/pull/93) feat(surfaces): button-driven UX — Telegram inline keyboard + Discord register button (#87)
- 2026-06-26 · [#92](https://github.com/Team-Hamsa/LFG/pull/92) feat(surfaces): show minted NFT artwork in announcements + mint result (#84, #86)
- 2026-06-26 · [#83](https://github.com/Team-Hamsa/LFG/pull/83) feat: Xaman-verified /register for Discord + Telegram bots
- 2026-06-26 · [#82](https://github.com/Team-Hamsa/LFG/pull/82) fix(telegram): launch shim for the double-import bug (+ config test hygiene)
- 2026-06-26 · [#81](https://github.com/Team-Hamsa/LFG/pull/81) feat(telegram): Telegram surface (Spine Plan 4 of 4)
- 2026-06-25 · [#80](https://github.com/Team-Hamsa/LFG/pull/80) feat(service): platform-aware spine (Plan 4a — Telegram prerequisite)
- 2026-06-25 · [#79](https://github.com/Team-Hamsa/LFG/pull/79) feat(discord): Discord bot migration onto lfg_service (Spine Plan 3 of 4)
- 2026-06-25 · [#78](https://github.com/Team-Hamsa/LFG/pull/78) feat(sdk): Surface SDK (Spine Plan 2 of 4) — shared lfg_service client
- 2026-06-25 · [#76](https://github.com/Team-Hamsa/LFG/pull/76) feat(service): Shared-Services Spine (Plan 1 of 4) — lfg_service backend
- 2026-06-24 · [#74](https://github.com/Team-Hamsa/LFG/pull/74) feat(mint): re-enable burnable flag on the minter (harvestable mints)
- 2026-06-24 · [#73](https://github.com/Team-Hamsa/LFG/pull/73) feat(activity): restore Trait Swapper as a third home-screen button
- 2026-06-24 · [#72](https://github.com/Team-Hamsa/LFG/pull/72) fix(activity): Harvest/Assemble do nothing — replace native window.confirm with in-app dialog
- 2026-06-24 · [#71](https://github.com/Team-Hamsa/LFG/pull/71) Dress-up Phase 3: Dressing Room UI in the Discord Activity (#65)
- 2026-06-23 · [#70](https://github.com/Team-Hamsa/LFG/pull/70) fix(economy): wire apply_economy_tx into the live listener (#68)
- 2026-06-23 · [#69](https://github.com/Team-Hamsa/LFG/pull/69) fix(economy): drop TransferFee on non-transferable mints (soulbound Bucket) + testnet E2E drivers
- 2026-06-23 · [#67](https://github.com/Team-Hamsa/LFG/pull/67) feat: dress-up trait economy — Phase 2 (on-ledger Bucket + harvest/assemble/equip, testnet)
- 2026-06-23 · [#62](https://github.com/Team-Hamsa/LFG/pull/62) feat: dress-up trait economy — Phase 1 (foundation: model + index + auditor)
- 2026-06-23 · [#59](https://github.com/Team-Hamsa/LFG/pull/59) feat: CDN layer-coverage auditor (live on-chain) for NFT swaps
- 2026-06-22 · [#60](https://github.com/Team-Hamsa/LFG/pull/60) feat: live per-nft_id on-chain NFT index (backfill + listener) + auditor repoint
<!-- changelog:end -->
