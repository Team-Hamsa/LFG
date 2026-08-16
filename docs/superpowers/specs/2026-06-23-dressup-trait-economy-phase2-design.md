# NFT Dress-Up Game — Phase 2: On-ledger Bucket + harvest/assemble/equip (testnet)

Date: 2026-06-23 · Branch: `feat/dressup-trait-economy-phase2` · Issue: #64 · Milestone: Dress-Up Trait Economy

Builds on Phase 1 (merged, commit `7669c26`): the pure accounting core
(`lfg_core/trait_economy.py`), the genesis + empty live-state tables
(`lfg_core/economy_store.py`), the per-`nft_id` index + listener
(`lfg_core/nft_index.py`, `nft_listener.py`), and the trait-economy auditor
(`scripts/audit_trait_economy.py`).

## Goal

Make the three atomic trait-economy operations real on-chain, **testnet-first**,
mirroring the `lfg_core/swap_flow.py` state-machine pattern (fail-safe ordering,
on-disk journaling, partial-failure recovery). Operations are **free** in the
MVP. Every XRPL transaction carries `SourceTag 2606160021`.

| Op | On-chain effect | Supply |
|----|-----------------|--------|
| **Harvest** | Burn an assembled character → its 8 assets + body drop into the owner's Bucket | ↓ |
| **Assemble** | Take a body + a full asset set from the Bucket → mint that body's edition | ↑ |
| **Equip** | Move a loose asset from the Bucket onto a live character; the displaced asset returns to the Bucket | = |

## Locked design decisions (from brainstorming)

1. **Authority model — issuer wallet (re-enable burnable).** Economy characters
   are minted **burnable + transferable + mutable** so the issuer wallet (`SEED`)
   can burn (harvest), mint+offer (assemble) and modify-in-place (equip) with its
   existing authority — closest to `swap_flow`. *Transition caveat:* a character
   that was previously swapped to mutable-only (non-burnable) cannot be
   issuer-burned, so it is **equip-only** until re-minted as burnable. On testnet
   we control flags, so all test characters are minted burnable; the caveat is
   surfaced as a precondition error, never a silent failure.

2. **Bucket — one on-ledger NFToken per wallet.** Mutable, **non-transferable**
   (soulbound: flags = `tfMutable` only), dedicated `BUCKET_TAXON`. Minted on
   first use (mint → offer → user-accept). Its URI points to a metadata JSON
   enumerating the loose assets + bodies. Updated via `NFTokenModify` after every
   op. The DB tables remain the authoritative accounting; the Bucket NFToken is
   the on-chain mirror (and is itself reconcilable — see §4).

3. **Supply accounting — frozen genesis + `supply_changes` ledger.** Genesis
   stays immutable. An append-only `supply_changes` table logs each *intentional*
   supply change (new-edition mint / permanent burn) with its per-`(slot,value)`
   trait deltas and body delta. Conservation becomes
   `census == genesis + Σ supply_changes`; any delta **not** in the ledger is
   still flagged as silent drift. `max_edition = max(genesis.max, ledger.max)`.

4. **Free MVP, SourceTag everywhere.** No fee collection. All NFToken/Payment
   transactions set `SourceTag = 2606160021` (closes part of #61).

## Conservation is invariant under normal play

The three ops only **move** assets; none creates or destroys one, and a *reborn*
edition's body was already in genesis (it went to the Bucket on harvest and
returns on assemble). So genesis stays valid through all harvest/assemble/equip
activity. Supply changes **only** on a brand-new-edition mint (a body not in
genesis, e.g. #3536) or a permanent burn (true destruction, distinct from
harvest-to-Bucket) — and those are exactly the events recorded in
`supply_changes`. A user-facing "mint a brand-new edition" feature is **out of
scope** (see §8); Phase 2 ships only the *accounting capability* (the ledger +
dynamic `max_edition` + the listener appending a row when it sees an
unknown-edition mint) so growth never reads as drift.

## 1. Module layout (mirrors the swap stack)

- **`lfg_core/economy_flow.py`** — the `swap_flow.py` analogue. Three async flow
  runners (`run_harvest`, `run_assemble`, `run_equip`) driving a small session
  object to a terminal state, plus shared journaling/recovery helpers. Journals
  every on-chain step to `ECONOMY_RECORDS_DIR` so a partial op is recoverable.
- **`lfg_core/bucket_token.py`** — Bucket NFToken concerns: compose the bucket
  metadata JSON from `(bucket_assets, bucket_bodies)`, mint-on-first-use
  (mint+offer+accept), and `modify` the URI after a contents change. Pure
  metadata builder + thin XRPL wrappers (injectable for tests).
- **`lfg_core/economy_store.py`** (extend) — add `bucket_tokens` and
  `supply_changes` tables + their read/write helpers (see §5).
- **`lfg_core/trait_economy.py`** (extend) — `verify_conservation` folds in
  `supply_changes`; `effective_max_edition(genesis, supply_changes)`; pure
  `apply_harvest/apply_assemble/apply_equip(census, …)` transition helpers and
  precondition predicates, fully unit-testable on fixtures.
- **`lfg_core/nft_listener.py`** (extend) — apply Bucket-token and supply-change
  events to the new tables (see §4).
- **`lfg_core/xrpl_ops.py`** (extend) — set `SourceTag` on every transaction; add
  the small ops the flows need (typed flags for burnable mints; bucket
  mint/modify reuse `mint_nft`/`modify_nft`).
- **`scripts/economy_harvest.py`, `economy_assemble.py`, `economy_equip.py`** —
  thin CLI drivers (the headless Phase-2 interface). REST/WS wiring is deferred
  to Phase 3.

## 2. Bucket NFToken lifecycle

- **Identity:** dedicated `BUCKET_TAXON` (distinct from the character taxon),
  flags = `tfMutable` (soulbound: not transferable by the holder; the issuer can
  still place the initial offer). One bucket per owner, tracked in `bucket_tokens`.
- **Metadata JSON** (authored by us; the on-chain record of contents):
  ```json
  {
    "schema": "<NFT_SCHEMA_URL>",
    "name": "LFG Bucket — <owner>",
    "description": "Loose traits and bodies held by <owner>.",
    "image": "<BUCKET_IMAGE_URL>",
    "external_link": "<EXTERNAL_WEBSITE_URL>",
    "lfg_bucket": {
      "assets": [{"slot": "Head", "value": "None", "count": 3}, ...],
      "bodies": [3536, 12, ...]
    }
  }
  ```
  A **static placeholder image** is used in the MVP (no per-content composition).
- **mint-on-first-use:** the first time an op must deposit into a user who has no
  bucket → compose+upload metadata, mint the bucket token (issuer), offer it to
  the user, wait for XUMM accept, record in `bucket_tokens`.
- **update:** every contents change → recompose metadata JSON, upload, and
  `NFTokenModify` the bucket token URI in place (id stable).

## 3. The three flows (fail-safe ordering + journaled recovery)

Each flow validates preconditions against the **pure core** before touching the
chain, journals before/after each on-chain step, and orders the irreversible step
to minimise the user's exposure window.

### Harvest (↓)
1. **Preconditions:** character is live, owned by the user, **burnable**, and its
   body matches `edition_bodies[edition]`; load its 8 non-body assets + body.
2. Ensure the user's Bucket exists (mint-on-first-use; reversible — an empty
   bucket simply sits in the wallet).
3. Journal `harvesting`.
4. **Burn the character (issuer authority) — IRREVERSIBLE.** The edition dies.
5. DB (one transaction): `+`8 assets to `bucket_assets`, `+`body to
   `bucket_bodies`.
6. Recompose + `NFTokenModify` the bucket token URI.
7. Journal `complete`.
*Recovery:* if step 5/6 fails after the burn, the journal records the burn and
the asset list; a recovery pass re-applies the DB update + bucket modify
idempotently. The listener also reconciles the burn independently (the character
leaves `live_nfts`).

### Assemble (↑, rebirth)
1. **Preconditions:** the edition is currently dead (no live character); the
   user's Bucket holds that edition's body **and** one chosen asset per non-body
   slot (the chosen `(slot,value)` set, validated against `bucket_assets`).
2. Compose the character image+metadata from the chosen assets (reuse
   `swap_compose`); upload to CDN.
3. Journal `assembling`.
4. **Mint the edition character** (issuer, burnable+transferable+mutable,
   character taxon, edition number in metadata) — *reversible: burn it back.*
5. DB (one transaction): `−`body from `bucket_bodies`, `−`chosen assets from
   `bucket_assets`; recompose + `NFTokenModify` the bucket token.
6. Offer the character to the user + XUMM accept.
7. Journal `complete`.
*Recovery:* fail at step 5 ⇒ burn the freshly-minted character back, Bucket
untouched. Fail at step 6 (offer/accept) ⇒ the minted token waits in the issuer
wallet for a re-offer; the Bucket is already drained, so the state is consistent
(no asset loss) — the journal carries the `nft_id` for re-offering.

### Equip (=)
1. **Preconditions:** character is live, owned, **mutable**; the Bucket holds the
   incoming asset `(slot, value)`; the displaced asset = the character's current
   value in that slot.
2. Compose the new character image+metadata (swap the one slot); upload.
3. Journal `equipping`.
4. **`NFTokenModify` the character URI** to the new metadata — *reversible:
   modify back to the old URI.*
5. DB (one transaction): `−`incoming asset, `+`displaced asset in `bucket_assets`;
   recompose + `NFTokenModify` the bucket token.
6. Journal `complete`.
*Recovery:* fail at step 5 ⇒ revert the character modify to its old URI, Bucket
untouched.

## 4. Listener role — the Bucket NFToken is the on-chain truth the DB mirrors

A raw `NFTokenBurn` event cannot tell us *which* assets went where — that routing
is application logic. So the **Bucket NFToken's metadata is the authoritative
on-chain record of a Bucket's contents** (we author it on every modify). The flow
writes the DB optimistically for immediacy; the **listener provides eventual
consistency** without a journal dependency:

- On a **Bucket-token** Mint/Modify (taxon = `BUCKET_TAXON`): fetch its metadata
  and **rebuild that owner's `bucket_assets` + `bucket_bodies` rows** from the
  `lfg_bucket` block. Update `bucket_tokens`.
- On a **character** mint whose edition is unknown (no genesis body **and** not
  already in `supply_changes`): append a `supply_changes` row recording the new
  body + its 8 assets (kind `mint`). This is the listener applying legitimate
  growth so it never reads as drift. (Permanent burns are recorded by the
  admin/script that performs them, not inferred from a burn event, since a burn
  event is indistinguishable from a harvest.)
- Character mint/accept/burn/modify continue to update `onchain_nfts` exactly as
  today.

Net: the whole system is reconcilable purely from chain —
`live characters + Σ bucket-token contents (+ trait_tokens, Phase 4)
== genesis + Σ supply_changes`. The auditor (extended) verifies this.

## 5. Schema additions (`economy_store.py`, same `onchain_{network}.db`)

```sql
CREATE TABLE IF NOT EXISTS bucket_tokens (
    owner      TEXT PRIMARY KEY,
    nft_id     TEXT,
    uri_hex    TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS supply_changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT,    -- 'mint' (supply +) | 'burn' (supply −)
    edition          INTEGER,
    body_value       TEXT,
    body_class       TEXT,
    trait_deltas_json TEXT,   -- {"Head|None": 1, "Background|Blue": 1, ...}
    actor            TEXT,    -- who/what applied it (script, listener)
    reason           TEXT,
    applied_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

`bucket_assets`, `bucket_bodies`, `trait_tokens` already exist from Phase 1.

## 6. Accounting changes (`trait_economy.py`)

- `effective_genesis(genesis, supply_changes) -> Genesis` — genesis with each
  ledger row's body and trait deltas folded in (the moving conservation target).
- `verify_conservation(genesis, census, supply_changes=[])` — compares census to
  the effective genesis; reports drift exactly as before when the ledger is empty
  (back-compatible).
- `effective_max_edition(genesis, supply_changes) -> int` — replaces the hard
  3535 cap for new mints.
- Pure transition helpers used by both the flows and the tests:
  `apply_harvest(census, character)`, `apply_assemble(census, edition, chosen)`,
  `apply_equip(census, character, incoming)` — each returns the post-op census,
  so a test can assert conservation holds across a simulated op sequence.
- Precondition predicates: `can_harvest`, `can_assemble`, `can_equip` returning a
  typed result `(ok: bool, reason: str)`.

## 7. Testing strategy (TDD throughout)

- **Pure core** (`tests/test_trait_economy_phase2.py`): preconditions
  (missing body, incomplete set, wrong slot, non-burnable character, already-live
  edition); census-after-op for each of the three transitions; conservation with
  a populated `supply_changes` ledger; `effective_max_edition`; the back-compat
  zero-ledger case.
- **Bucket metadata** (`tests/test_bucket_token.py`): metadata round-trips
  (compose → parse → identical census contribution); empty bucket; `"None"`
  assets preserved.
- **Flows** (`tests/test_economy_flow.py`): drive each flow with **injected
  `xrpl_ops`/`cdn` fakes**, asserting (a) the happy path's on-chain call sequence
  and DB end-state, and (b) every partial-failure branch — burn-then-DB-fail
  (journal replay), mint-then-offer-fail (token parked, bucket drained),
  modify-then-DB-fail (character reverted). No network.
- **Listener** (`tests/test_economy_listener.py`): a Bucket Modify rebuilds
  `bucket_assets`/`bucket_bodies` from metadata; an unknown-edition character mint
  appends a `supply_changes` row; a known/reborn edition does **not**.
- **Testnet E2E** (`scripts/`, manual, documented in the plan): harvest →
  assemble (rebirth the same edition) → equip on a live testnet character, with
  `audit_trait_economy.py` green at every step; then an admin new-edition mint →
  `supply_changes` row → auditor still green.
- `mypy --strict`, `ruff`, full `pytest` green (pre-commit gate).

## 8. Out of scope (Phase 2)

- The Discord Activity **UI** — Phase 3.
- Standalone **tradeable trait NFTokens** (extract-to-NFToken / deposit-back) —
  Phase 4. `trait_tokens` stays empty; it is included in conservation only so the
  accounting is complete.
- **Fees / pricing** — free MVP.
- A **user-facing "mint a brand-new edition" feature.** The accounting *supports*
  growth (the `supply_changes` ledger + dynamic `max_edition` + the listener), but
  the act of minting a brand-new edition is an admin/script concern, not a Phase-2
  core flow.
- **REST/WS endpoint** wiring for the flows — deferred to Phase 3; Phase 2 exposes
  the flows as async functions + CLI drivers.

## 9. Config additions (`config.py`)

```
SOURCE_TAG            = int(os.getenv("SOURCE_TAG", "2606160021"))
BUCKET_TAXON          = int(os.getenv("BUCKET_TAXON", "1761"))
BUCKET_IMAGE_URL      = os.getenv("BUCKET_IMAGE_URL", NFT_COLLECTION_LOGO)  # static placeholder
ECONOMY_NFT_FLAGS     = int(os.getenv("ECONOMY_NFT_FLAGS", "25"))  # burnable+transferable+mutable
BUCKET_NFT_FLAGS      = int(os.getenv("BUCKET_NFT_FLAGS", "16"))   # mutable (soulbound)
ECONOMY_RECORDS_DIR   = os.getenv("ECONOMY_RECORDS_DIR", "economy_records")
ECONOMY_CDN_FOLDER    = os.getenv("ECONOMY_CDN_FOLDER", SWAP_CDN_FOLDER)
```

Characters reuse `SWAP_ISSUER_ADDRESS` + `SWAP_TAXON`; buckets use the same
issuer with `BUCKET_TAXON`.
