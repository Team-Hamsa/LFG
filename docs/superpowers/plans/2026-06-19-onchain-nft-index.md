# LFG On-Chain NFT Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a per-`nft_id` SQLite index of every LFG NFToken, populated by a backfill and kept fresh by a websocket listener, so the layer-coverage auditor queries it fast and offline on both networks.

**Architecture:** A shared `lfg_core/nft_index.py` module (enumeration, normalization, DB upsert/query) feeds three thin entry points: a backfill script (Phase 1), the repointed auditor (Phase 2), and a live listener (Phase 3). Per-network SQLite files (`onchain_testnet.db` / `onchain_mainnet.db`).

**Tech Stack:** Python 3.10+, `xrpl-py` (AsyncWebsocketClient, clio `nfts_by_issuer` / `nft_info` / tx stream), `aiohttp`, stdlib `sqlite3`, pytest. Reuses `lfg_core.{config,swap_meta,layer_store,xrpl_ops}`.

## Global Constraints

- Python 3.10+, ruff line-length 100, mypy `strict = true` (test files relaxed).
- Per-network DB files selected by `XRPL_NETWORK`; env override `ONCHAIN_DB_PATH`.
- clio: mainnet `wss://s2-clio.ripple.com`, testnet `wss://clio.altnet.rippletest.net:51233`.
- Issuer/taxon: mainnet `rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ`/1760; testnet `config.SWAP_ISSUER_ADDRESS`/1760.
- Normalize via `swap_meta.normalize_attributes`; body via `swap_meta.detect_body`. NFT lsfMutable flag = `0x0010`.
- Tokens with unfetchable metadata are recorded with null `attributes_json`, never dropped.
- Tests stub env like `tests/test_rarity.py` (incl. `BUNNY_PULL_ZONE`); async tests use a fresh `new_event_loop()`, never `get_event_loop()`.

---

## Phase 1 — Index foundation + backfill

### Task 1: Schema + DB helpers in `nft_index.py`

**Files:**
- Create: `lfg_core/nft_index.py`
- Test: `tests/test_nft_index.py`

**Interfaces:**
- Produces: `OnchainNft` dataclass (`nft_id:str, nft_number:int|None, owner:str|None, is_burned:bool, mutable:bool|None, uri_hex:str, body:str, attributes:list[dict], image:str, ledger_index:int|None`); `index_db_path(network:str)->str`; `init_db(path:str)->sqlite3.Connection`; `upsert(conn, rec:OnchainNft)->None`; `live_nfts(conn)->list[OnchainNft]`.

- [ ] **Step 1:** Write failing tests: `init_db` creates `onchain_nfts` with the spec columns; `upsert` inserts then updates on conflict (same `nft_id` second write changes owner, not row count); `live_nfts` returns only `is_burned=0`; round-trips `attributes` through JSON. `index_db_path("testnet")` ends with `onchain_testnet.db`, honors `ONCHAIN_DB_PATH`.
- [ ] **Step 2:** Run `pytest tests/test_nft_index.py -v` → FAIL (module missing).
- [ ] **Step 3:** Implement `OnchainNft`, `index_db_path`, `init_db` (CREATE TABLE + indexes from spec), `_row_to_nft`/`_nft_to_row` (attributes ↔ `attributes_json`, bools ↔ int), `upsert` (`INSERT … ON CONFLICT(nft_id) DO UPDATE SET …`, stamp `last_synced_at` via SQL `CURRENT_TIMESTAMP`), `live_nfts` (`SELECT … WHERE is_burned=0`).
- [ ] **Step 4:** Run tests → PASS. `ruff check`, `mypy lfg_core/nft_index.py`.
- [ ] **Step 5:** Commit `feat: onchain_nfts schema + sqlite helpers`.

### Task 2: Token enumeration + record building

**Files:**
- Modify: `lfg_core/nft_index.py`
- Test: `tests/test_nft_index.py`

**Interfaces:**
- Consumes: `OnchainNft`.
- Produces: `enumerate_tokens(clio:str, issuer:str, taxon:int, limit:int=400)->list[dict]` (each `{nft_id, nft_number?, owner, is_burned, flags, uri_hex}`); `token_record(token:dict, metadata:dict|None)->OnchainNft` (pure).

- [ ] **Step 1:** Write failing tests for `token_record`: with metadata → normalized attributes + `body` from `detect_body` + `mutable` from `flags & 0x10` + `nft_number` from name; with `metadata=None` → `attributes==[]`, `body==""`, other fields from token. (No network test for `enumerate_tokens`; it's covered via injection in Task 3.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `token_record` (reuse `swap_meta.normalize_attributes`, `detect_body`, `extract_nft_number`); implement `enumerate_tokens` by moving the auditor's `enumerate_onchain` here and also capturing `owner`, `is_burned`, `flags`, `nft_number` (`nft_serial`/name). Keep returning burned tokens (caller decides).
- [ ] **Step 4:** Run → PASS. ruff + mypy.
- [ ] **Step 5:** Commit `feat: token enumeration + record normalization`.

### Task 3: Backfill script

**Files:**
- Create: `scripts/backfill_onchain.py`
- Test: `tests/test_backfill_onchain.py`

**Interfaces:**
- Consumes: `nft_index.{enumerate_tokens,token_record,init_db,upsert,index_db_path}`.
- Produces: `run_backfill(conn, enumerate_fn, fetch_meta_fn, concurrency=16)->dict` (counts: `total, with_metadata, unreadable`).

- [ ] **Step 1:** Write failing test: injected `enumerate_fn` returns 3 tokens (one duplicate edition pair like #3547, one burned, one URI-less); injected `fetch_meta_fn` returns metadata for two; assert rows upserted, duplicate kept as two rows, burned recorded with `is_burned=1`, URI-less → null attributes; re-running is idempotent (row count stable). Use a fresh `new_event_loop()`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `run_backfill` (bounded `asyncio.Semaphore`, fetch metadata per token, `token_record`, `upsert`) and a `main()` wiring real clio enumeration + `swap_meta.fetch_metadata` + per-network DB. Mirror the auditor's arg style (`--network`, `--issuer/--taxon/--clio` overrides).
- [ ] **Step 4:** Run → PASS. ruff + mypy.
- [ ] **Step 5:** Commit `feat: on-chain backfill script`.

### Task 4: Run the testnet backfill (manual verification)

**Files:** none (operational).

- [ ] **Step 1:** Run `.venv/bin/python scripts/backfill_onchain.py --network testnet`.
- [ ] **Step 2:** Verify with sqlite: `onchain_testnet.db` has the live tokens incl. two `#3547` rows (the `0018…` and `0019…` ids). Record the count.
- [ ] **Step 3:** (Mainnet backfill deferred until after Phase 2, since IPFS is slow; run opportunistically.)

---

## Phase 2 — Repoint the auditor

### Task 5: Auditor reads the index DB

**Files:**
- Modify: `scripts/audit_layer_coverage.py`
- Test: `tests/test_audit_layer_coverage.py`

**Interfaces:**
- Consumes: `nft_index.{index_db_path,init_db,live_nfts}`.
- Produces: a `--live` flag (bool); default source is the DB.

- [ ] **Step 1:** Write failing test: build a temp index DB via `nft_index` with the #3547 clean+Wonder pair, point a new `run_audit_from_db(conn, store)` at it, assert the Wonder variant is flagged and the clean one isn't. (Reuses the LocalLayerStore fixture pattern already in the test file.)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add `run_audit_from_db(conn, store)` that maps `live_nfts()` rows to `NftResult` via the existing `audit_attributes` (reusing each row's cached `body` + `attributes`). In `main()`, default to the DB source for the configured network; `--live` keeps `run_audit(enumerate, fetch, store)`. If the DB file is missing, exit with a clear "run backfill first" message.
- [ ] **Step 4:** Run full suite → PASS. ruff + mypy.
- [ ] **Step 5:** Commit `feat: auditor reads the on-chain index by default`.

### Task 6: Verify auditor parity (manual)

**Files:** none (operational).

- [ ] **Step 1:** `.venv/bin/python scripts/audit_layer_coverage.py --network testnet` (DB source) → confirm it reports the same 4 NFTs (incl. #3547) as the earlier `--live` run, instantly.
- [ ] **Step 2:** Spot-check `--live` still works and agrees.

---

## Phase 3 — Live listener

### Task 7: Transaction handlers (pure)

**Files:**
- Create: `lfg_core/nft_listener.py`
- Test: `tests/test_nft_listener.py`

**Interfaces:**
- Consumes: `nft_index.{OnchainNft,upsert,token_record}`, `swap_meta`.
- Produces: `classify_tx(tx:dict)->str|None` (`"mint"|"accept"|"burn"|"modify"|None`); `affected_nft_ids(tx:dict)->list[str]` (from `meta.AffectedNodes` NFTokenPage diffs + tx fields).

- [ ] **Step 1:** Write failing tests with canned tx dicts (one per type) asserting `classify_tx` and that `affected_nft_ids` extracts the token id(s). Include a non-NFT tx → `None`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `classify_tx` (by `TransactionType`) and `affected_nft_ids` (read `NFTokenID` from tx/meta; for mint/accept, diff `NFTokenPage` AffectedNodes).
- [ ] **Step 4:** Run → PASS. ruff + mypy.
- [ ] **Step 5:** Commit `feat: NFT tx classification + affected-id extraction`.

### Task 8: Listener apply-loop with injected fetchers

**Files:**
- Modify: `lfg_core/nft_listener.py`
- Test: `tests/test_nft_listener.py`

**Interfaces:**
- Consumes: Task 7 helpers, `nft_index`.
- Produces: `apply_tx(conn, tx, fetch_token_fn, fetch_meta_fn)->None` where `fetch_token_fn(nft_id)->dict|None` resolves owner/flags/uri via `nft_info` (the Kinesis pattern) and `fetch_meta_fn(uri_hex)->dict|None`.

- [ ] **Step 1:** Write failing tests over a temp index DB: a `mint` tx upserts a new row; `accept` updates `owner` (via `fetch_token_fn`); `burn` sets `is_burned=1`; `modify` re-fetches metadata and updates `attributes`/`uri_hex`. Use injected fetchers; fresh `new_event_loop()`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `apply_tx`: classify, resolve affected ids, for each build/refresh the record (`fetch_token_fn` + `fetch_meta_fn` + `token_record`), burn just flips the flag, upsert. Per-id try/except + logging.
- [ ] **Step 4:** Run → PASS. ruff + mypy.
- [ ] **Step 5:** Commit `feat: apply NFT transactions to the index`.

### Task 9: Listener entry point + nft_info resolver

**Files:**
- Create: `scripts/onchain_listener.py`
- Modify: `lfg_core/xrpl_ops.py` (add `nft_info(nft_id, clio)->dict|None` if absent)
- Test: `tests/test_nft_listener.py` (resolver shape only; ws loop is operational)

**Interfaces:**
- Consumes: `nft_listener.apply_tx`, `nft_index`, `xrpl_ops.nft_info`.
- Produces: CLI `--network … {snapshot|listen}`.

- [ ] **Step 1:** Write failing test: `xrpl_ops.nft_info` parses a canned clio response into `{owner, flags, uri_hex, is_burned}` (inject the request via a fake client or factor the parse into a pure helper `_parse_nft_info(result)`), assert the parse.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `_parse_nft_info` + `nft_info`. Implement `onchain_listener.py`: `snapshot` calls the Phase-1 backfill; `listen` opens `AsyncWebsocketClient(clio)`, subscribes to the tx stream, calls `apply_tx` per message, reconnects with backoff. Real `fetch_token_fn = nft_info`, `fetch_meta_fn = swap_meta.fetch_metadata`.
- [ ] **Step 4:** Run → PASS. ruff + mypy. Full suite green.
- [ ] **Step 5:** Commit `feat: on-chain listener entry point + nft_info resolver`.

### Task 10: pm2 wiring + docs (operational)

**Files:**
- Modify: `CLAUDE.md` (document the index + listener), optionally an `ecosystem`/pm2 note.

- [ ] **Step 1:** Start `lfg-index-testnet` (`onchain_listener.py --network testnet listen`) under pm2 after a `snapshot`; confirm it ingests a live event (e.g. trigger/observe a testnet swap modify) and the row updates.
- [ ] **Step 2:** Document run/operate steps in `CLAUDE.md` (backfill, listener, pm2 names, DB files). `pm2 save`.
- [ ] **Step 3:** Commit `docs: operate the on-chain NFT index + listener`.

---

## Self-Review notes

- Spec coverage: schema (T1), shared module (T1–2), backfill (T3–4), auditor repoint (T5–6), listener mint/accept/burn/modify + nft_info (T7–9), pm2/docs (T10). ✓
- Mainnet backfill is operational (run after Phase 2) — flagged in T4/T6, not a code gap.
- Types consistent: `OnchainNft`, `token_record`, `upsert`, `live_nfts`, `apply_tx`, `nft_info` names reused verbatim across tasks.
