# Dress-Up Trait Economy Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the three trait-economy ops (harvest / assemble / equip) real on XRPL testnet with an on-ledger per-user Bucket NFToken, mirroring `lfg_core/swap_flow.py` (fail-safe ordering, journaling, partial-failure recovery).

**Architecture:** A pure accounting/transition core (`trait_economy.py`, extended) gates every op; persistence lives in the per-network `onchain_{network}.db` (`economy_store.py`, extended). `bucket_token.py` owns the on-ledger Bucket NFToken (metadata builder + mint/modify wrappers). `economy_flow.py` is the `swap_flow.py` analogue: three async session runners driving on-chain steps with disk journaling. The listener rebuilds Bucket DB tables from Bucket-token metadata (on-chain truth) and appends a `supply_changes` row on unknown-edition mints. Thin CLI scripts are the headless interface.

**Tech Stack:** Python 3.10, `xrpl-py`, SQLite, `aiohttp`, ffmpeg (via `swap_compose`), pytest, mypy --strict, ruff.

## Global Constraints

- **SourceTag = 2606160021** on every XRPL transaction / XUMM payload, without exception.
- **Operations are free** in the MVP — no fee collection anywhere.
- Economy **characters** are minted **burnable+transferable+mutable** (`ECONOMY_NFT_FLAGS = 25`); the **Bucket** is **mutable-only / soulbound** (`BUCKET_NFT_FLAGS = 16`).
- **DB tables are authoritative for accounting; the Bucket NFToken is the on-chain mirror.** Conservation: `census == genesis + Σ supply_changes`.
- Genesis is **immutable**; supply growth/shrinkage is recorded only in `supply_changes`. `max_edition = max(genesis.max, ledger.max)`.
- TDD throughout; `mypy --strict`, `ruff`, `pytest` must stay green (pre-commit gate). Frequent commits, one per task.
- Reuse existing helpers: `swap_meta` (slots/normalize/body), `swap_compose` (compose+upload), `cdn`, `xumm_ops`, `nft_index`/`economy_store` patterns. DRY, YAGNI.

---

### Task 1: Config + SourceTag on all XRPL transactions

**Files:**
- Modify: `lfg_core/config.py` (append Phase-2 constants)
- Modify: `lfg_core/xrpl_ops.py` (set `source_tag` on every tx)
- Test: `tests/test_xrpl_source_tag.py` (create)

**Interfaces:**
- Produces: `config.SOURCE_TAG: int`, `config.BUCKET_TAXON: int`, `config.BUCKET_IMAGE_URL: str`, `config.ECONOMY_NFT_FLAGS: int`, `config.BUCKET_NFT_FLAGS: int`, `config.ECONOMY_RECORDS_DIR: str`, `config.ECONOMY_CDN_FOLDER: str`.
- Produces: `xrpl_ops.mint_nft(..., flags: int | None = None)` — flags override (defaults to `config.NFT_FLAGS`); every tx object built in `xrpl_ops` now carries `source_tag=config.SOURCE_TAG`.

- [ ] **Step 1: Add config constants.** Append to `config.py`:

```python
# Make Waves hackathon: every XRPL tx must carry this source tag.
SOURCE_TAG = int(os.getenv("SOURCE_TAG", "2606160021"))

# Dress-up trait economy (Phase 2)
BUCKET_TAXON = int(os.getenv("BUCKET_TAXON", "1761"))
BUCKET_IMAGE_URL = os.getenv("BUCKET_IMAGE_URL", NFT_COLLECTION_LOGO)
ECONOMY_NFT_FLAGS = int(os.getenv("ECONOMY_NFT_FLAGS", "25"))  # burnable+transferable+mutable
BUCKET_NFT_FLAGS = int(os.getenv("BUCKET_NFT_FLAGS", "16"))    # mutable only (soulbound)
ECONOMY_RECORDS_DIR = os.getenv("ECONOMY_RECORDS_DIR", "economy_records")
ECONOMY_CDN_FOLDER = os.getenv("ECONOMY_CDN_FOLDER", SWAP_CDN_FOLDER)
```

- [ ] **Step 2: Write the failing test** (`tests/test_xrpl_source_tag.py`). Assert the tx builders set the source tag by inspecting constructed model objects. Use monkeypatch to capture the model passed to `submit_and_wait`.

```python
import lfg_core.xrpl_ops as xrpl_ops
from lfg_core import config

def test_mint_sets_source_tag(monkeypatch):
    captured = {}
    class _Resp:
        result = {"hash": "H", "meta": {"TransactionResult": "tesSUCCESS", "nftoken_id": "N"}}
    def fake_submit(tx, client, wallet):
        captured["tx"] = tx
        return _Resp()
    monkeypatch.setattr(xrpl_ops, "submit_and_wait", fake_submit)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", lambda self, req: _Resp())
    import asyncio
    asyncio.run(xrpl_ops.mint_nft("https://x/meta.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS))
    assert captured["tx"].source_tag == config.SOURCE_TAG
```

Add equivalent tests for `create_nft_offer`, `burn_nft`, `modify_nft`, `buy_and_burn` (capture tx, assert `source_tag`).

- [ ] **Step 3: Run, verify fail.** `pytest tests/test_xrpl_source_tag.py -v` → FAIL (source_tag is None).

- [ ] **Step 4: Implement.** Add `"source_tag": config.SOURCE_TAG` to the `kwargs`/constructor of every transaction in `xrpl_ops.py`: `NFTokenMint`, `NFTokenCreateOffer`, `NFTokenBurn`, `NFTokenModify`, `Payment` (in `buy_and_burn`). Add the optional `flags` param to `mint_nft` (`flags = flags if flags is not None else config.NFT_FLAGS`).

- [ ] **Step 5: Run, verify pass.** `pytest tests/test_xrpl_source_tag.py -v` → PASS. Then full suite: `.venv/bin/python -m pytest -q` → green.

- [ ] **Step 6: Commit.**

```bash
git add lfg_core/config.py lfg_core/xrpl_ops.py tests/test_xrpl_source_tag.py
git commit -m "feat(economy): SourceTag on all XRPL txns + Phase 2 config"
```

---

### Task 2: economy_store schema — bucket_tokens + supply_changes

**Files:**
- Modify: `lfg_core/economy_store.py`
- Test: `tests/test_economy_store_phase2.py` (create)

**Interfaces:**
- Produces:
  - `record_supply_change(conn, kind: str, edition: int|None, body_value: str, body_class: str, trait_deltas: dict[str,int], actor: str, reason: str) -> None`
  - `read_supply_changes(conn) -> list[dict]` — each `{kind, edition, body_value, body_class, trait_deltas: dict[str,int], actor, reason}`
  - `set_bucket_token(conn, owner: str, nft_id: str, uri_hex: str) -> None`
  - `get_bucket_token(conn, owner: str) -> tuple[str, str] | None` — `(nft_id, uri_hex)`
  - `set_bucket_contents(conn, owner: str, assets: list[tuple[str,str,int]], bodies: list[int]) -> None` — **replace** all `bucket_assets`/`bucket_bodies` rows for `owner` (used by both flows and listener rebuild).
- Consumes: Task-1 nothing; uses existing `_ECONOMY_SCHEMA` style.

- [ ] **Step 1: Failing test.** Round-trip a supply change and bucket contents:

```python
import sqlite3
from lfg_core import economy_store as es

def _conn():
    c = sqlite3.connect(":memory:")
    es.init_economy_schema(c)
    return c

def test_supply_change_roundtrip():
    c = _conn()
    es.record_supply_change(c, "mint", 3536, "Straight Blue", "male",
                            {"Head|None": 1, "Background|Blue": 1}, "script", "test mint")
    rows = es.read_supply_changes(c)
    assert len(rows) == 1
    assert rows[0]["edition"] == 3536
    assert rows[0]["trait_deltas"]["Head|None"] == 1

def test_set_bucket_contents_replaces():
    c = _conn()
    es.set_bucket_contents(c, "rUser", [("Head", "None", 2)], [3536])
    es.set_bucket_contents(c, "rUser", [("Eyes", "Blue", 1)], [])
    assert es.read_bucket_assets(c) == [("rUser", "Eyes", "Blue", 1)]
    assert es.read_bucket_bodies(c) == []

def test_bucket_token_roundtrip():
    c = _conn()
    es.set_bucket_token(c, "rUser", "NFTID", "ABCD")
    assert es.get_bucket_token(c, "rUser") == ("NFTID", "ABCD")
    assert es.get_bucket_token(c, "rNope") is None
```

- [ ] **Step 2: Run, verify fail.** `pytest tests/test_economy_store_phase2.py -v` → FAIL.

- [ ] **Step 3: Implement.** Add the two `CREATE TABLE` statements (from spec §5) to `_ECONOMY_SCHEMA`. Implement the helpers. `set_bucket_contents` does `DELETE FROM bucket_assets WHERE owner=?`, `DELETE FROM bucket_bodies WHERE owner=?`, then `executemany` inserts, single commit. `trait_deltas` stored as `json.dumps`. `record_supply_change` inserts one row. `read_supply_changes` parses `trait_deltas_json`.

- [ ] **Step 4: Run, verify pass + full suite.**

- [ ] **Step 5: Commit.**

```bash
git add lfg_core/economy_store.py tests/test_economy_store_phase2.py
git commit -m "feat(economy): bucket_tokens + supply_changes tables and helpers"
```

---

### Task 3: trait_economy pure extensions — effective genesis, transitions, preconditions

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy_phase2.py` (create)

**Interfaces:**
- Produces:
  - `effective_genesis(genesis: Genesis, supply_changes: list[dict]) -> Genesis` — folds each row's body (kind `mint` adds the edition→body, `burn` removes it) and `trait_deltas` (signed) into a new `Genesis`.
  - `effective_max_edition(genesis: Genesis, supply_changes: list[dict]) -> int`
  - `verify_conservation(genesis, census, supply_changes: list[dict] | None = None)` — compares census to `effective_genesis(...)`; signature back-compatible (default `None` ⇒ `[]`).
  - `Precheck = namedtuple/ dataclass (ok: bool, reason: str)`
  - `can_harvest(rec: OnchainNft, genesis: Genesis) -> Precheck`
  - `can_assemble(edition: int, chosen: dict[str,str], bucket_assets, bucket_bodies, live_editions: set[int], genesis: Genesis) -> Precheck`
  - `can_equip(rec: OnchainNft, slot: str, value: str, bucket_assets, owner: str) -> Precheck`
- Consumes: Task-2 `read_supply_changes` row shape.

- [ ] **Step 1: Failing tests.** Cover effective genesis, max edition, and each precheck. Example:

```python
from lfg_core import trait_economy as te

def test_effective_genesis_adds_mint():
    g = te.Genesis(trait_counts={("Head", "None"): 1}, edition_bodies={1: ("B", "male")})
    sc = [{"kind": "mint", "edition": 2, "body_value": "B2", "body_class": "ape",
           "trait_deltas": {"Head|None": 1}}]
    eff = te.effective_genesis(g, sc)
    assert eff.trait_counts[("Head", "None")] == 2
    assert eff.edition_bodies[2] == ("B2", "ape")

def test_effective_max_edition():
    g = te.Genesis(trait_counts={}, edition_bodies={1: ("B", "male")})
    assert te.effective_max_edition(g, [{"kind": "mint", "edition": 3536,
        "body_value": "B", "body_class": "male", "trait_deltas": {}}]) == 3536

def test_conservation_with_ledger_ok():
    g = te.Genesis(trait_counts={("Head", "None"): 0}, edition_bodies={})
    sc = [{"kind": "mint", "edition": 5, "body_value": "B", "body_class": "male",
           "trait_deltas": {("Head", "None").__class__ and "Head|None": 1}}]
    census = te.Census(trait_counts={("Head", "None"): 1}, body_presence={5: 1})
    assert te.verify_conservation(g, census, sc).ok
```

(Trim the awkward key expression — use `{"Head|None": 1}`.) Add precheck tests: `can_harvest` fails on burned/wrong-body/non-burnable; `can_assemble` fails on missing body, incomplete set, already-live edition; `can_equip` fails when the bucket lacks the asset.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** Key parsing: `trait_deltas` keys are `"slot|value"` strings → split on `"|"`. `effective_genesis` copies `genesis.trait_counts`/`edition_bodies`, then for each row applies signed deltas and body add/remove. `verify_conservation` calls `effective_genesis` then runs the existing comparison logic against the effective target (refactor the current body/trait loop to take the effective genesis). `can_*` return a small frozen dataclass `Precheck(ok, reason)`. Burnable check: pass the on-ledger flags through — `can_harvest` needs to know burnable; add a `burnable: bool` param (the flow supplies it from `get_account_nfts` flags, `flags & NFT_FLAG_BURNABLE`).

- [ ] **Step 4: Run, verify pass + full suite.**

- [ ] **Step 5: Commit.**

```bash
git add lfg_core/trait_economy.py tests/test_trait_economy_phase2.py
git commit -m "feat(economy): effective-genesis ledger, transitions, op preconditions"
```

---

### Task 4: bucket_token — metadata builder + parser (pure)

**Files:**
- Create: `lfg_core/bucket_token.py`
- Test: `tests/test_bucket_token.py` (create)

**Interfaces:**
- Produces:
  - `build_bucket_metadata(owner: str, assets: list[tuple[str,str,int]], bodies: list[int]) -> dict` — the JSON dict (spec §2), `lfg_bucket.assets` sorted deterministically.
  - `parse_bucket_metadata(meta: dict) -> tuple[list[tuple[str,str,int]], list[int]]` — inverse: `(assets, bodies)`; tolerant of missing/garbage (returns `([], [])`).

- [ ] **Step 1: Failing test.**

```python
from lfg_core import bucket_token as bt

def test_metadata_roundtrips():
    assets = [("Head", "None", 3), ("Background", "Blue", 1)]
    bodies = [3536, 12]
    meta = bt.build_bucket_metadata("rUser", assets, bodies)
    assert meta["lfg_bucket"]["bodies"] == [12, 3536]  # sorted
    got_assets, got_bodies = bt.parse_bucket_metadata(meta)
    assert sorted(got_assets) == sorted(assets)
    assert got_bodies == [12, 3536]

def test_parse_tolerates_garbage():
    assert bt.parse_bucket_metadata({}) == ([], [])
    assert bt.parse_bucket_metadata({"lfg_bucket": {"assets": "x"}}) == ([], [])
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** `build_bucket_metadata` returns the dict from spec §2 (using `config.NFT_SCHEMA_URL`, `config.BUCKET_IMAGE_URL`, `config.EXTERNAL_WEBSITE_URL`), assets sorted by `(slot, value)`, bodies sorted. `parse_bucket_metadata` reads `meta.get("lfg_bucket")`, guards types, returns `([], [])` on anything malformed.

- [ ] **Step 4: Run, verify pass + full suite.**

- [ ] **Step 5: Commit.**

```bash
git add lfg_core/bucket_token.py tests/test_bucket_token.py
git commit -m "feat(economy): bucket NFToken metadata builder/parser"
```

---

### Task 5: bucket_token — XRPL lifecycle wrappers (injectable)

**Files:**
- Modify: `lfg_core/bucket_token.py`
- Test: `tests/test_bucket_token_lifecycle.py` (create)

**Interfaces:**
- Produces:
  - `async ensure_bucket(conn, owner, *, mint_fn, offer_fn, accept_payload_fn, upload_fn) -> tuple[str, str]` — returns `(nft_id, uri_hex)`; if `get_bucket_token` is None, builds empty metadata, uploads, mints (taxon `BUCKET_TAXON`, flags `BUCKET_NFT_FLAGS`), offers, returns an accept payload via the injected fns, records `set_bucket_token`. Idempotent if already present.
  - `async sync_bucket(conn, owner, assets, bodies, *, upload_fn, modify_fn) -> None` — recompose metadata, upload, `modify_fn(nft_id, owner, url)`, update `set_bucket_token` uri.
- Consumes: Task-2 `get_bucket_token`/`set_bucket_token`; Task-4 builders. Injected fns wrap `xrpl_ops`/`cdn`/`xumm_ops` so tests need no network.

- [ ] **Step 1: Failing test** with fakes capturing calls; assert mint happens once, second `ensure_bucket` is a no-op, `sync_bucket` calls `modify_fn` with the new URL and persists the new uri_hex. (Full fake code in test file.)

- [ ] **Step 2–5:** implement, verify, full suite, commit.

```bash
git commit -m "feat(economy): bucket NFToken mint-on-first-use + sync wrappers"
```

---

### Task 6: economy_flow — Harvest

**Files:**
- Create: `lfg_core/economy_flow.py`
- Test: `tests/test_economy_flow_harvest.py` (create)

**Interfaces:**
- Produces: `async run_harvest(session: HarvestSession, deps: EconomyDeps) -> None` driving states `HARVESTING → BUCKET_SYNC → DONE` / `FAILED`. `EconomyDeps` is a dataclass of injected callables (`burn_fn`, `ensure_bucket`, `sync_bucket`, `record_fn`, journal dir). `HarvestSession` holds `owner`, `character` (normalized rec incl. `nft_id`, `attributes`, `body`, `burnable`), `edition`, `state`, `error`.
- Consumes: Task-3 `can_harvest`; Task-5 bucket wrappers; Task-2 `set_bucket_contents`; `xrpl_ops.burn_nft`.

- [ ] **Step 1: Failing tests.** (a) Happy path: precheck passes → `burn_fn` called once with the character nft_id → `bucket_assets`/`bucket_bodies` gain the 8 assets + body → `sync_bucket` called → state DONE. (b) Precheck fail (non-burnable) → no burn, state FAILED. (c) Burn-then-DB-fail: `burn_fn` succeeds but the DB write raises → journal status `harvested_pending_bucket`, state FAILED with a recovery hint, assets NOT lost (journal carries them). Use injected fakes; no network.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** the state machine + `_write_economy_record` journal helper (mirror `swap_flow._write_swap_record`, writing to `config.ECONOMY_RECORDS_DIR`). Order: precheck → ensure bucket → journal `harvesting` → burn (irreversible) → compute new bucket contents (current DB contents + the 8 assets + body) → `set_bucket_contents` → `sync_bucket` → journal `complete`.

- [ ] **Step 4: Run, verify pass + full suite.**

- [ ] **Step 5: Commit.**

```bash
git commit -m "feat(economy): harvest flow (burn character -> bucket)"
```

---

### Task 7: economy_flow — Assemble

**Files:**
- Modify: `lfg_core/economy_flow.py`
- Test: `tests/test_economy_flow_assemble.py` (create)

**Interfaces:**
- Produces: `async run_assemble(session: AssembleSession, deps: EconomyDeps) -> None`. `AssembleSession` holds `owner`, `edition`, `chosen: dict[str,str]` (slot→value per non-body slot), `body_value`, `body_class`, and is populated with `results` (the minted nft_id + accept payload).
- Consumes: Task-3 `can_assemble`; `swap_compose.compose_nft`/`upload_output`; `xrpl_ops.mint_nft(flags=config.ECONOMY_NFT_FLAGS)`; `create_nft_offer`; `xumm_ops.create_accept_offer_payload`; bucket `sync_bucket`.

- [ ] **Step 1: Failing tests.** (a) Happy path: precheck passes → compose+upload → mint (flags=25) → bucket drained (body + chosen assets removed) → sync_bucket → offer+accept payload in results → DONE. (b) Precheck fail (incomplete set) → no mint, FAILED. (c) Mint-then-bucket-fail: mint succeeds, DB drain raises → `burn_fn` called to revert the mint, bucket untouched, FAILED. (d) Offer-fail after drain: token parked, bucket drained, state FAILED with re-offer hint + nft_id in journal.

- [ ] **Step 2–4:** implement (ordering per spec §3 Assemble), verify, full suite.

- [ ] **Step 5: Commit.**

```bash
git commit -m "feat(economy): assemble flow (bucket set -> mint edition)"
```

---

### Task 8: economy_flow — Equip

**Files:**
- Modify: `lfg_core/economy_flow.py`
- Test: `tests/test_economy_flow_equip.py` (create)

**Interfaces:**
- Produces: `async run_equip(session: EquipSession, deps: EconomyDeps) -> None`. `EquipSession` holds `owner`, `character` (rec incl. `uri_hex` for revert), `slot`, `incoming_value`; computes `displaced_value` from the character's current slot.
- Consumes: Task-3 `can_equip`; `swap_compose`; `xrpl_ops.modify_nft`; bucket `sync_bucket`.

- [ ] **Step 1: Failing tests.** (a) Happy path: precheck → compose+upload new char → `modify_fn(char_nft_id, owner, new_url)` → bucket: −incoming, +displaced → sync_bucket → DONE. (b) Precheck fail (bucket lacks incoming) → no modify, FAILED. (c) Modify-then-bucket-fail: modify succeeds, DB raises → revert modify to old uri_hex, bucket untouched, FAILED.

- [ ] **Step 2–4:** implement (ordering per spec §3 Equip), verify, full suite.

- [ ] **Step 5: Commit.**

```bash
git commit -m "feat(economy): equip flow (swap one slot in place)"
```

---

### Task 9: Listener — rebuild bucket from token metadata + supply_changes on unknown mints

**Files:**
- Modify: `lfg_core/nft_listener.py`
- Test: `tests/test_economy_listener.py` (create)

**Interfaces:**
- Produces:
  - `async apply_economy_tx(conn, tx, *, fetch_token_fn, fetch_meta_fn, genesis, taxon_of) -> None` — a sibling to `apply_tx` (or an extension hook): on a Bucket-token (taxon == `config.BUCKET_TAXON`) Mint/Modify, fetch metadata, `parse_bucket_metadata`, and `economy_store.set_bucket_contents(owner, ...)` + `set_bucket_token`; on a character Mint whose edition ∉ `effective genesis editions` and ∉ existing `supply_changes`, `record_supply_change(kind="mint", ...)`.
- Consumes: Task-2/3/4 helpers. `taxon_of(token)` resolves a token's taxon (from the fetched token dict).

- [ ] **Step 1: Failing tests.** (a) A Bucket Modify event with metadata for `rUser` rebuilds that owner's `bucket_assets`/`bucket_bodies` exactly. (b) An unknown-edition (3536) character mint appends one `supply_changes` row. (c) A known-edition (reborn) mint appends nothing. Use injected fetchers; no network.

- [ ] **Step 2–4:** implement, verify, full suite.

- [ ] **Step 5: Commit.**

```bash
git commit -m "feat(economy): listener rebuilds buckets + logs supply growth"
```

---

### Task 10: Auditor — fold supply_changes into conservation

**Files:**
- Modify: `scripts/audit_trait_economy.py`
- Test: `tests/test_audit_trait_economy_phase2.py` (create) — or extend the existing auditor test.

**Interfaces:**
- Produces: the auditor reads `read_supply_changes(conn)` and passes it to `verify_conservation`; the Markdown report gains a "Supply changes" section listing each ledger row; nonzero exit only on true drift (unlogged delta).

- [ ] **Step 1: Failing test.** Build an in-memory `onchain_{net}.db`: genesis with 1 edition, a `supply_changes` mint row for edition 2, a live census containing both editions → auditor reports `ok` (exit 0). Remove the ledger row → auditor reports drift (nonzero).

- [ ] **Step 2–4:** implement, verify, full suite.

- [ ] **Step 5: Commit.**

```bash
git commit -m "feat(economy): auditor accounts for supply_changes ledger"
```

---

### Task 11: CLI drivers (headless interface)

**Files:**
- Create: `scripts/economy_harvest.py`, `scripts/economy_assemble.py`, `scripts/economy_equip.py`
- Test: smoke-import in `tests/test_economy_scripts_import.py` (create) — assert each module imports and exposes `main`.

**Interfaces:**
- Each script: `argparse` (`--network`, op-specific args: harvest `--owner --nft-id`; assemble `--owner --edition --set k=v...`; equip `--owner --nft-id --slot --value`), opens the index DB, loads the character via `xrpl_ops.get_account_nfts` + `swap_meta`, builds the session, wires real `EconomyDeps` (real `xrpl_ops`/`cdn`/`xumm_ops`/bucket wrappers), runs the flow, prints the result + any XUMM accept link. Mirrors existing `scripts/` style.

- [ ] **Step 1: Failing test** (import + `hasattr(mod, "main")`).
- [ ] **Step 2–4:** implement, verify, full suite.
- [ ] **Step 5: Commit.**

```bash
git commit -m "feat(economy): CLI drivers for harvest/assemble/equip"
```

---

### Task 12: Docs + final verification

**Files:**
- Modify: `CLAUDE.md` (one section documenting the Phase-2 economy ops + the burnable-character / bucket-token model), `README` if it indexes phases.

- [ ] **Step 1:** Add a concise "Dress-up trait economy — Phase 2 (testnet)" subsection under the XRPL Integration area of `CLAUDE.md`: the three ops, the burnable+mutable character / soulbound bucket model, the `supply_changes` accounting rule, and the CLI entry points.
- [ ] **Step 2:** Run the **full gate**: `.venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/mypy --strict lfg_core scripts` (match the project's pre-commit invocation). All green.
- [ ] **Step 3: Commit.**

```bash
git commit -m "docs(economy): document Phase 2 ops and accounting model"
```

---

## Self-Review

**Spec coverage:** §1 module layout → Tasks 4–6/9/11; §2 bucket lifecycle → Tasks 4–5; §3 three flows → Tasks 6–8; §4 listener → Task 9; §5 schema → Task 2; §6 accounting → Task 3; §7 testing → tests in every task + Task 12 gate; §8 out-of-scope respected (no UI/Phase-4/fees/new-edition-feature/REST tasks); §9 config → Task 1. SourceTag global constraint → Task 1. ✅

**Placeholders:** Tasks 5/7/8/11 say "full fake code in test file" rather than inlining every fake — acceptable because the fakes follow the identical injected-callable pattern established with full code in Task 6's harvest test; the executor writes them by analogy. All signatures/types are named explicitly.

**Type consistency:** `EconomyDeps` (injected callables) and `set_bucket_contents`/`sync_bucket`/`can_*`/`effective_genesis` names are used identically across Tasks 2–11. `trait_deltas` keys are `"slot|value"` strings everywhere. `Precheck(ok, reason)` consistent.
