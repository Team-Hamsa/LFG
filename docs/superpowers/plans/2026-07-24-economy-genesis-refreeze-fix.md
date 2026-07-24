# Stop re-freezing genesis — burn-shrinkage accounting + automated reconcile/audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make genesis a one-time immutable baseline that never needs re-freezing.
Close the mint/burn asymmetry so an out-of-band character burn records a
compensating `-1` shrinkage row (idempotently, without double-counting flow-owned
burns, and never for a harvested blank whose assets survive in the Closet), add a
reconcile sweep for historical burns, and automate nightly reconcile + audit under
pm2 cron with a drift alert that distinguishes benign swap substitution from a real
conservation leak.

**Architecture:** Four independent seams —
1. **Store** (`lfg_core/economy_store.py`): self-migrating `nft_id` column on
   `supply_changes`, `record_supply_change(nft_id=...)`, `supply_change_exists_for_nft`.
2. **Accounting core** (`lfg_core/trait_economy.py`): pure `burn_shrinkage_deltas`.
3. **Listener + reconcile** (`lfg_core/nft_listener.py`, `lfg_core/nft_index.py`,
   `lfg_core/supply_reconcile.py`, `scripts/reconcile_supply_shrinkage.py`).
4. **Automation** (`scripts/economy_nightly_reconcile.py`, `scripts/audit_trait_economy.py`
   webhook, `ecosystem.*.config.js`, docs).

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; sqlite. No client JS changes.

## Global Constraints

- **SourceTag `2606160021` + provenance memos** must be preserved on every
  on-ledger tx. **This feature builds NO new transactions** — it is pure local-DB
  accounting over the existing on-chain index; do not add any tx path.
- **Pre-push gate** (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass; **never** `--no-verify`.
- Every new `tests/` module importing `lfg_core` at top level MUST carry the
  env-guard preamble (`os.environ.setdefault(...)` for `BUNNY_PULL_ZONE` /
  `LAYER_SOURCE`) before the imports, or full-suite ordering breaks.
- No `app.js`/client change here, so no cache-buster bump is needed.
- Genesis is frozen ONCE; nothing in this change re-freezes. The ledger absorbs
  every legitimate change.

---

### Task 1: Store — `nft_id` idempotency key on `supply_changes`

**Files:**
- Modify: `lfg_core/economy_store.py`
- Test: `tests/test_economy_store_supply_nft_id.py` (new)

**Interfaces:**
- Produces: `record_supply_change(conn, kind, edition, body_value, body_class, trait_deltas, actor, reason, nft_id: str | None = None)`; `supply_change_exists_for_nft(conn, nft_id, kind="burn") -> bool`; `read_supply_changes` dicts gain `"nft_id"`.
- Consumes: existing `init_economy_schema`.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble at top:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "test.example.com")
  os.environ.setdefault("LAYER_SOURCE", "local")
  import sqlite3
  from lfg_core import economy_store

  def _conn():
      c = sqlite3.connect(":memory:")
      economy_store.init_economy_schema(c)
      return c

  def test_record_and_lookup_by_nft_id():
      c = _conn()
      economy_store.record_supply_change(
          c, "burn", 3560, "Ape", "ape", {"Eyes|None": -1},
          "listener", "out-of-band burn NFTABC", nft_id="NFTABC")
      assert economy_store.supply_change_exists_for_nft(c, "NFTABC") is True
      assert economy_store.supply_change_exists_for_nft(c, "NFTZZZ") is False
      rows = economy_store.read_supply_changes(c)
      assert rows[-1]["nft_id"] == "NFTABC"

  def test_self_migration_adds_nft_id_column():
      c = sqlite3.connect(":memory:")
      c.execute("CREATE TABLE supply_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "kind TEXT, edition INTEGER, body_value TEXT, body_class TEXT, "
                "trait_deltas_json TEXT, actor TEXT, reason TEXT, "
                "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
      c.commit()
      economy_store.init_economy_schema(c)  # must ALTER-add nft_id, not crash
      cols = {r[1] for r in c.execute("PRAGMA table_info(supply_changes)")}
      assert "nft_id" in cols
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_economy_store_supply_nft_id.py -q` (expect: `record_supply_change` TypeError on `nft_id` / missing `supply_change_exists_for_nft`).
- [ ] **Step 3: Implement** — in `init_economy_schema`, after creating `supply_changes`, add a self-migrating `ALTER TABLE supply_changes ADD COLUMN nft_id TEXT` guarded by a `PRAGMA table_info` check (mirror the market-store self-migrating pattern). Add the optional `nft_id` param to `record_supply_change` (append to INSERT column list/values). Add `supply_change_exists_for_nft`. Add `nft_id` to `read_supply_changes` SELECT + returned dict.
- [ ] **Step 4: Run to verify they pass** — same pytest command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest tests/ -q -k "economy or supply or trait_economy"`.
- [ ] **Step 6: Commit** — `feat(economy): idempotency nft_id column on supply_changes (#322)`.

---

### Task 2: Accounting core — `burn_shrinkage_deltas` (pure)

**Files:**
- Modify: `lfg_core/trait_economy.py`
- Test: `tests/test_trait_economy_burn_shrinkage.py` (new)

**Interfaces:**
- Produces: `burn_shrinkage_deltas(rec: OnchainNft) -> dict[str, int] | None` returning `{"slot|value": -1}` for all `NON_BODY_SLOTS` of a **dressed** char, or `None` for a blank/unreadable char. (Body value/class read by the caller from `rec` via existing `swap_meta.get_attr(rec.attributes, "Body")` / `rec.body`.)

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble, then build `OnchainNft` fixtures (dressed, blank via `trait_economy.blank_attributes()`, empty-attrs):
  ```python
  def test_dressed_char_yields_negative_deltas():
      rec = _dressed_nft()  # attributes with real slot values + Body
      d = trait_economy.burn_shrinkage_deltas(rec)
      assert d is not None and all(v == -1 for v in d.values())
      assert set(d) == {f"{s}|{trait_economy.slot_value(rec, s)}" for s in trait_economy.NON_BODY_SLOTS}

  def test_blank_char_yields_none():
      rec = _blank_nft()  # attributes = blank_attributes()
      assert trait_economy.burn_shrinkage_deltas(rec) is None

  def test_unreadable_char_yields_none():
      rec = _nft(attributes=[])
      assert trait_economy.burn_shrinkage_deltas(rec) is None
  ```
  Add a conservation round-trip test: genesis with edition #3560 `+1`, then apply a burn `-1` via `record_supply_change` → `verify_conservation(genesis, census_without_that_char, changes).ok`.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_trait_economy_burn_shrinkage.py -q`.
- [ ] **Step 3: Implement** — add `burn_shrinkage_deltas`: return `None` if `not rec.attributes` or `is_blank(rec)`; else `{f"{slot}|{slot_value(rec, slot)}": -1 for slot in NON_BODY_SLOTS}`.
- [ ] **Step 4: Run to verify they pass**.
- [ ] **Step 5: Wider suite** — `.venv/bin/python -m pytest tests/ -q -k trait_economy`.
- [ ] **Step 6: Commit** — `feat(economy): pure burn_shrinkage_deltas for dressed-character burns (#322)`.

---

### Task 3: Listener recorder + `nft_by_id` + legacy-flow nft_id stamp

**Files:**
- Modify: `lfg_core/nft_index.py` (add `nft_by_id`), `lfg_core/nft_listener.py` (burn branch), `lfg_core/economy_flow.py` (stamp `nft_id` on legacy harvest `-1`).
- Test: `tests/test_listener_burn_shrinkage.py` (new)

**Interfaces:**
- Produces: `nft_index.nft_by_id(conn, nft_id) -> OnchainNft | None`; extended burn branch in `apply_economy_tx`.
- Consumes: `trait_economy.burn_shrinkage_deltas`, `economy_store.supply_change_exists_for_nft`, `economy_store.record_supply_change(nft_id=...)`.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble; build an in-memory index DB (`nft_index.init_db(":memory:")` + `economy_store.init_economy_schema`), insert a dressed character edition, freeze a genesis containing it, mark it burned (`nft_index.mark_burned`), then drive a burn tx through `apply_economy_tx` with stub `fetch_token_fn`/`fetch_meta_fn` and `genesis=effective_genesis(...)`:
  ```python
  async def test_out_of_band_dressed_burn_records_one_shrinkage(...):
      await nft_listener.apply_economy_tx(conn, burn_tx, fetch_token_fn=..., fetch_meta_fn=..., genesis=g)
      rows = [r for r in economy_store.read_supply_changes(conn) if r["kind"] == "burn"]
      assert len(rows) == 1 and rows[0]["nft_id"] == NFT_ID
      # idempotent:
      await nft_listener.apply_economy_tx(conn, burn_tx, ...)
      assert len([r for r in economy_store.read_supply_changes(conn) if r["kind"]=="burn"]) == 1

  async def test_blank_burn_records_no_shrinkage(...): ...      # blank char -> 0 rows
  async def test_flow_owned_burn_not_double_counted(...): ...   # pre-insert a -1 with nft_id=NFT_ID -> listener adds none
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_listener_burn_shrinkage.py -q` (expect: missing `nft_by_id` / no shrinkage row written).
- [ ] **Step 3: Implement** —
  - `nft_index.nft_by_id`: `SELECT * FROM onchain_nfts WHERE nft_id=?`, map via `_row_to_nft`, return `None` if absent (returns burned rows too).
  - `apply_economy_tx` burn branch: after `delete_trait_token(conn, nft_id)`, when `genesis is not None`, `rec = nft_index.nft_by_id(conn, nft_id)`; if `rec` and `rec.nft_number is not None` and `rec.nft_number in genesis.edition_bodies` and not `economy_store.supply_change_exists_for_nft(conn, nft_id)`: `deltas = trait_economy.burn_shrinkage_deltas(rec)`; if `deltas` is not None, `record_supply_change(conn, "burn", rec.nft_number, swap_meta.get_attr(rec.attributes,"Body") or "", rec.body, deltas, "listener", f"out-of-band burn {nft_id}", nft_id=nft_id)`. Keep inside the existing per-`nft_id` try/except. (Do NOT `continue` before this runs.)
  - `economy_flow.py:453` legacy harvest `-1`: add `nft_id=rec.nft_id`.
- [ ] **Step 4: Run to verify they pass**.
- [ ] **Step 5: Wider suite** — `.venv/bin/python -m pytest tests/ -q -k "listener or economy or trait_economy"`.
- [ ] **Step 6: Commit** — `feat(economy): record out-of-band character burns as supply shrinkage (#322)`.

---

### Task 4: Reconcile sweep for historical burns

**Files:**
- Modify: `lfg_core/supply_reconcile.py` (add `reconcile_shrinkage`).
- Create: `scripts/reconcile_supply_shrinkage.py` (CLI mirror of `reconcile_supply_growth.py`).
- Test: `tests/test_reconcile_shrinkage.py` (new)

**Interfaces:**
- Produces: `supply_reconcile.reconcile_shrinkage(conn, *, dry_run=False) -> {"written": [...], "skipped_unreadable": [...]}`.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble; index DB with N dressed **burned** editions (in genesis) and no burn rows → dry-run writes 0 rows and returns them in `written`; `--apply` path (call with `dry_run=False`) writes exactly N; second call writes 0 (idempotent); a blank burned edition is not in `written`; an unreadable burned edition lands in `skipped_unreadable`.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_reconcile_shrinkage.py -q`.
- [ ] **Step 3: Implement** — `reconcile_shrinkage` mirrors `reconcile_growth`: iterate `onchain_nfts` burned rows (add a small `nft_index` helper or query `SELECT * FROM onchain_nfts WHERE is_burned=1`), keep our character editions present in the effective genesis lacking a `burn` supply_change for their `nft_id`; use `trait_economy.burn_shrinkage_deltas` (None → skip blank/unreadable, report unreadable). Write via `record_supply_change(..., "burn", ..., nft_id=rec.nft_id)`. Add `scripts/reconcile_supply_shrinkage.py` copying the arg/RO-dry-run/`genesis_exists` scaffold of `reconcile_supply_growth.py`.
- [ ] **Step 4: Run to verify they pass**.
- [ ] **Step 5: Wider suite** — `.venv/bin/python -m pytest tests/ -q -k "reconcile or supply"`.
- [ ] **Step 6: Commit** — `feat(economy): reconcile_supply_shrinkage sweep for historical out-of-band burns (#322)`.

---

### Task 5: Nightly automation + drift alert + docs

**Files:**
- Create: `scripts/economy_nightly_reconcile.py` (runs growth + shrinkage `--apply` in one process).
- Modify: `scripts/audit_trait_economy.py` (optional webhook alert on non-clean run + benign-vs-real drift labelling), `ecosystem.prod.config.js`, `ecosystem.staging.config.js`, `docs/runbooks/mainnet-mvp-launch.md`, `CLAUDE.md`.
- Test: `tests/test_audit_alert_classification.py` (new)

**Interfaces:**
- Produces: a `classify_drift(conservation) -> {"benign_swap": {...}, "real": {...}}` helper in `audit_trait_economy.py` (or `trait_economy.py` if cleaner) partitioning per-`(slot,value)` drift by whether the slot's signed total nets to zero.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble; unit-test `classify_drift`: a slot with `{("Hat","A"):-1,("Hat","B"):+1}` → `benign_swap` (slot total 0); a slot with `{("Eyes","None"):-4}` → `real`. (Webhook POST itself is I/O — assert the classification + that alert body construction only happens when `not conservation.ok`; do not hit the network in tests.)
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_audit_alert_classification.py -q`.
- [ ] **Step 3: Implement** —
  - `classify_drift`: group `conservation.trait_drift` by slot; a slot whose signed deltas sum to 0 → all its entries `benign_swap`, else `real`.
  - In `audit_trait_economy.py::main`, when the run is non-clean and `--alert-webhook` (default `os.environ.get("ECONOMY_AUDIT_WEBHOOK_URL")`) is set, POST a compact summary (network, live count, real-drift table, benign-swap note) to the Discord webhook via `aiohttp` (best-effort, failure only logs). Clean run posts nothing. Report file behavior unchanged.
  - `scripts/economy_nightly_reconcile.py`: open one index conn, run `supply_reconcile.reconcile_growth(conn)` then `reconcile_shrinkage(conn)` with writes, commit, print a summary; `--network` arg.
  - `ecosystem.*.config.js`: add `lfg-economy-reconcile` (runs the nightly reconcile) and `lfg-economy-audit` (runs the audit) as `autorestart:false` cron entries scheduled after `lfg-snapshot` (e.g. `"20 0 * * *"` and `"25 0 * * *"`); staging mirrors with `--network testnet`.
  - Docs: in `docs/runbooks/mainnet-mvp-launch.md` + `CLAUDE.md` economy sections, state genesis is frozen ONCE; re-freeze is break-glass only; drift is diagnosed via the reconcile sweeps + audit, never normalized by re-freezing. Document `ECONOMY_AUDIT_WEBHOOK_URL` in the `.env` block.
- [ ] **Step 4: Run to verify they pass**.
- [ ] **Step 5: Wider suite** — `.venv/bin/python -m pytest tests/ -q`.
- [ ] **Step 6: Commit** — `feat(economy): nightly reconcile+audit cron with drift alert; demote re-freeze to break-glass (#322)`.

---

### Final Task: Full gate + PR

- [ ] Run the full gate locally: `.venv/bin/python -m pytest tests/ -q`, `ruff check . --fix`, `ruff format .`, `.venv/bin/mypy` per the pre-push config. Confirm `validate-trait-config` still passes. Never `--no-verify`.
- [ ] Manual smoke on staging: `reconcile_supply_shrinkage.py --network testnet --apply` then `audit_trait_economy.py --network testnet` → `Conservation: OK` with **no re-freeze**; confirm the 8-edition -4/slot drift is gone.
- [ ] Push the branch; `gh pr create` **non-draft** (Team-Hamsa/LFG rules): **no AI attribution** in commits or PR body, no `Co-Authored-By`. Body summarises the burn-asymmetry fix + automation and links #322.
- [ ] Wait for Greptile + CodeRabbit; resolve every actionable finding (fix in code AND reply on its thread naming the fixing commit) before merge. Confirm the `Greptile Review` check-run summary reads a pass.
- [ ] Ops follow-up (out of PR): review mainnet `reconcile_supply_shrinkage.py --network mainnet` **dry-run** output with the maintainer before any `--apply`; then `pm2 start` the two new cron entries on prod/staging and set `ECONOMY_AUDIT_WEBHOOK_URL`.
