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
- [In-app Marketplace](#in-app-marketplace)
- [Milady Body + Animated Layers](#milady-body--animated-layers)
- [Ledger History + Leaderboards](#ledger-history--leaderboards)
- [On-chain NFT Index](#on-chain-nft-index)
- [NFT Generation & Rules](#nft-generation--rules)
- [Trait Rules Engine + Body Affinity](#trait-rules-engine--body-affinity)
- [Mainnet Launch Hardening](#mainnet-launch-hardening)

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

> Currently **feature-flagged off** in production (`ECONOMY_ENABLED=0`) while
> review findings ([#178](https://github.com/Team-Hamsa/LFG/issues/178)–[#184](https://github.com/Team-Hamsa/LFG/issues/184)) are fixed;
> go-live tracked in [#185](https://github.com/Team-Hamsa/LFG/issues/185).

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
