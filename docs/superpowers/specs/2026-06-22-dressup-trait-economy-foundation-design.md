# NFT Dress-Up Game — Phase 1: Trait Economy Foundation

Date: 2026-06-22 · Branch: `feat/onchain-nft-index` (Phase 1 work to branch from here)

## Context: the dress-up game (full initiative)

The LFG collection becomes a **trait economy**. Instead of NFTs being fixed,
their *traits* become the atomic assets and assembled NFTs are "complete sets"
that must always stay complete. Users dress up their NFTs by moving traits
around; the supply of editions and the rarity of every trait churn constantly as
a result.

### The mechanic

- **Bodies are the scarce edition slots.** There are ~3,535 bodies; each carries
  a permanent edition number and is the locked identity anchor of a character.
  Total live editions can never exceed the body count. The collection churns
  between near-0 and full as bodies are assembled/disassembled.
- Every **live character NFToken** must hold a **full set of traits** (its body +
  one asset in each of the other slots) to exist on-chain.
- Each user owns a **Bucket** — a per-user, mutable on-ledger object that holds
  their loose, unequipped traits (and any loose bodies).

Three atomic on-ledger operations underlie everything:

| Op | On-chain effect | Collection size |
|----|-----------------|-----------------|
| **Harvest / disassemble** | Burn an assembled character → *all* its assets incl. body drop into the owner's Bucket | ↓ (edition dies) |
| **Assemble / rebirth** | Take a body + a full asset set from the Bucket → mint that body's edition | ↑ (edition reborn) |
| **Equip / re-skin** | Move a loose asset from the Bucket onto a live character; the displaced asset returns to the Bucket | unchanged |

"Dress up A using a trait from B" = **harvest B** (B burned, B's assets → Bucket)
then **equip** the wanted asset onto A. Later phase: a **hybrid** escape hatch
that mints a loose Bucket trait out into a standalone tradeable NFToken, and
deposits one back.

### Two system-wide invariants

1. **Completeness** — every live character NFToken always holds exactly one asset
   in each of the 9 slots (its body + 8 others; empty slots hold a `"None"`
   asset).
2. **Conservation** — asset instances are never silently created or destroyed;
   they only move between Buckets ↔ characters ↔ standalone trait-tokens via
   explicit ops. The only supply changes are explicit body mint/burn.

### Decomposition (each phase = its own spec → plan → build)

- **Phase 1 — Foundational model + index + auditor** *(this spec; no on-ledger
  writes, no UI)*: define the asset/Bucket/body→edition data model and the two
  invariants; extend the on-chain index to represent them; build a
  conservation/completeness auditor; freeze a clean genesis baseline.
- **Phase 2 — On-ledger Bucket + core flows (testnet)**: Bucket NFToken lifecycle
  + harvest / assemble / equip flows with partial-failure recovery (mirroring
  `lfg_core/swap_flow.py`).
- **Phase 3 — Dress-up UI in the Discord Activity**: the game screen — character,
  Bucket palette, preview, confirm, XUMM signing.
- **Phase 4 — Hybrid tradeable traits**: extract-to-NFToken / deposit-back.

**MVP product decisions:** operations are **free** during the MVP (revisit
pricing once the loop works). The on-ledger trait representation is the **hybrid**
model (logical Bucket now, optional extract-to-NFToken later).

---

## Phase 1 scope

Phase 1 delivers the **canonical accounting model and verification tooling** for
the trait economy, validated against today's live collection (where Buckets are
empty). It performs **no on-ledger writes** and ships **no UI**. It is the
bedrock every later phase audits against.

Deliverables:

1. A pure data model + functions for the asset universe, body ledger, genesis,
   and the two invariants.
2. Schema additions to the per-network index DB for genesis + (empty) live-state
   tables.
3. A one-time **reconciliation + genesis-freeze** tool that turns today's messy
   live chain into a clean, frozen baseline, with a Markdown report.
4. A **trait-economy auditor** that verifies completeness + conservation and
   writes a Markdown report (mirrors `scripts/audit_collection_integrity.py`).

## Existing code this builds on

- `lfg_core/nft_index.py` — per-network `onchain_{network}.db`, `onchain_nfts`
  table (one row per `nft_id`: `nft_number`, `owner`, `is_burned`, `mutable`,
  `body`, `attributes_json`, `ledger_index`, …), `live_nfts()`,
  `collection_anomalies()`.
- `lfg_core/swap_meta.py` — `TRAIT_ORDER` (the 9 slots), `normalize_attributes`
  (fills every slot, `"None"` for absent), `detect_body` (value → class:
  male/female/ape/skeleton), `extract_nft_number`, `season_for_number`.
- `scripts/audit_collection_integrity.py` — the pattern to mirror: pure anomaly
  function in the core + a CLI that prints + writes a Markdown report + nonzero
  exit on findings.

## 1. Asset model

An **asset** is a `(slot, value)` pair over the 9 `TRAIT_ORDER` slots. Two kinds:

- **Body slot** — identity-bound. Each body instance *is* an edition; tracked
  individually as `edition → (body_value, body_class)`. A body is never `"None"`,
  never pooled, never interchangeable — rebirthing edition *N* requires *N*'s own
  body.
- **8 non-body slots** (`Background, Back, Clothing, Mouth, Eyebrows, Eyes, Head,
  Accessory`) — pooled, fungible instances counted by `(slot, value)`. **`"None"`
  is a real, slot-typed, conserved asset** (e.g. `(Head, "None")` is a tradeable
  thing). A slot only ever holds an asset of its own slot type.

Structural invariant: every live character holds exactly one asset per slot;
total system assets `= 9 × (#body slots)`, forever.

## 2. Genesis (immutable reference)

Frozen once from the **reconciled** live index at t0 and stored in the
per-network `onchain_{network}.db`:

- `trait_genesis(slot TEXT, value TEXT, genesis_count INTEGER, PRIMARY KEY(slot,
  value))` — per non-body `(slot, value)` count, including every `(slot,"None")`.
- `edition_bodies(edition INTEGER PRIMARY KEY, body_value TEXT, body_class TEXT)`
  — the canonical body ledger (one row per genesis edition).
- A small `genesis_meta(key, value)` row set recording the freeze timestamp,
  source network, reconciliation summary, and `#body slots` for sanity checks.

Genesis is the conservation yardstick; it is **never mutated** after freeze. (A
re-freeze is an explicit, deliberate, destructive operation, not part of normal
operation.)

## 3. Reconciliation (one-time, pre-launch)

A pass over the live index that turns today's chain into a clean genesis and
emits `reports/trait-economy-reconciliation-<network>-<ts>.md`:

- **Duplicate editions** (>1 live token for one edition): keep one canonical
  token per edition — rule: **prefer the mutable token, tie-break by highest
  `ledger_index` (newest)**; record the rest as reconciled-out (listed in the
  report; not part of genesis).
- **Missing editions** (no live token): recorded as non-existent bodies →
  excluded from `edition_bodies` (cannot be reborn in the MVP). Listed.
- **Unparsed-name / out-of-range** live tokens: excluded from genesis and listed.

The frozen genesis is computed from the reconciled live set: bodies from each
canonical token, and per-slot `(value)` counts (incl. `"None"`) from the
canonical tokens' normalized attributes.

## 4. Live-state tables (foundation for later phases; empty at t0)

Created now so the auditor's conservation sum is complete from day one; populated
only in Phase 2+:

- `bucket_assets(owner TEXT, slot TEXT, value TEXT, count INTEGER, PRIMARY
  KEY(owner, slot, value))` — loose non-body traits per user.
- `bucket_bodies(owner TEXT, edition INTEGER PRIMARY KEY)` — bodies held loose in
  a Bucket (an edition's body is either on a live character or in exactly one
  Bucket).
- `trait_tokens(nft_id TEXT PRIMARY KEY, owner TEXT, slot TEXT, value TEXT)` —
  standalone extracted trait NFTokens (Phase 4).

Live characters continue to come from `onchain_nfts` (live, reconciled).

Naming: the per-user holding object is the **Bucket** (the "Closet" alias is
dropped).

## 5. Invariants & auditor

Pure core in `lfg_core/trait_economy.py`, CLI in
`scripts/audit_trait_economy.py` (mirrors `audit_collection_integrity.py`):

**Completeness** (over live characters):
- every live character has exactly one asset per slot (no missing/duplicate
  slot — already guaranteed by `normalize_attributes`, re-verified defensively);
- its body matches `edition_bodies[edition]`;
- flags wrong-body, orphan bodies (a live edition with no genesis body row), and
  any slot anomaly.

**Conservation**:
- for every non-body `(slot, value)`:
  `live_characters + bucket_assets + trait_tokens == genesis_count`;
- for bodies: each genesis edition's body is in **exactly one** place — a live
  `onchain_nfts` character *or* exactly one `bucket_bodies` row — never both,
  never neither-while-claimed;
- flags any created / destroyed / duplicated asset (the drift, per `(slot,value)`
  and per edition).

Output: a Markdown report + nonzero exit on any violation. At t0 (Buckets and
trait_tokens empty) conservation reduces to `live == genesis` and passes by
construction — which is exactly the proof that the accounting is correct.

## 6. Code layout & testing

- `lfg_core/trait_economy.py` — **pure** (no I/O): `build_genesis(records) ->
  Genesis`, `asset_census(characters, bucket_assets, bucket_bodies, trait_tokens)
  -> Census`, `verify_conservation(genesis, census) -> ConservationReport`,
  `verify_completeness(characters, genesis) -> CompletenessReport`, plus
  body-ledger and dedupe helpers. Reuses `swap_meta` for slot/value normalization
  and body-class detection.
- Schema additions next to `nft_index.py` (same `_SCHEMA` style / same DB file).
- `scripts/freeze_genesis.py` — reconcile the live index, write the
  reconciliation report, freeze genesis (idempotent; refuses to overwrite an
  existing frozen genesis without an explicit `--force`).
- `scripts/audit_trait_economy.py` — load genesis + live + bucket/trait_token
  state, run both verifications, print + write the Markdown report, nonzero exit
  on drift.
- TDD throughout. The pure core is fully unit-testable on fixtures (duplicate
  editions, missing editions, `"None"` slots, conservation drift injected into a
  synthetic bucket). End-to-end check: freeze genesis from the real 5.5k-token
  mainnet index and confirm the auditor reports zero drift at t0.

## Out of scope (Phase 1)

- Any XRPL write (mint/burn/modify), Bucket NFToken, XUMM signing — Phase 2.
- The Discord Activity UI — Phase 3.
- Standalone tradeable trait NFTokens (extract/deposit) — Phase 4. (`trait_tokens`
  table is created empty now only so conservation accounting is complete.)
- Fees / pricing — deferred; MVP ops are free.
- Live-listener updates to the new tables — Phase 2 (the listener learns to apply
  harvest/assemble/equip when those flows exist).

## Open questions resolved during brainstorming

- On-chain model: **hybrid** (logical Bucket + later extract-on-demand).
- Body lifecycle: body → Bucket on burn; **editions can be reborn**; bodies are
  unique & identity-bound (never pooled).
- `"None"`: a **real, conserved asset**.
- Genesis: **today's live chain, reconciled first**.
- Economics: **free during MVP**.
- Dedupe rule: prefer mutable, tie-break newest `ledger_index`.
- Genesis storage: same `onchain_{network}.db`. Module: `lfg_core/trait_economy.py`.
</content>
</invoke>
