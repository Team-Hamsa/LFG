# XLS-56 Batch Accept Offers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user accept multiple pending gift/mint NFT offers under **one**
Xaman signature via an XLS-56 Batch transaction, chunked at the 8-inner cap,
behind a feasibility gate defaulted OFF so the code lands and is unit-tested
while Xaman Batch-signing support and XLS-56 mainnet amendment activation remain
unverified. Issue #219.

**Architecture:** Four independent seams —
(1) config feasibility gate + inner cap;
(2) `xumm_ops.create_batch_accept_payload` + a pure chunking helper;
(3) the `POST /api/offers/accept-batch` service endpoint (fail-closed re-verify,
chunk, one payload per chunk) + `batch` flag on `/api/offers/pending`;
(4) the client checkbox/multi-select UI in the pending-offers tray, gated on the
server-advertised `batch` capability.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS client.

## Global Constraints

- **SourceTag = 2606160021 + provenance memos on every transaction.** The
  builder passes txjson through `xumm_ops._create_xumm_payload`, which
  `setdefault`s `SourceTag` and `Memos` onto the **outer Batch** txjson — never
  bypass it. Memo: `initiator=user`, `platform=platform_for_surface(...)`,
  `action=memos.ACTION_ACCEPT_OFFER`.
- **Pre-push gate** (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass. Never `--no-verify`. In a worktree,
  ensure the `.venv` symlink exists or the gate silently skips.
- **Any `app.js`/`index.html` change bumps the cache-buster** (`?v=` on the
  module import in `webapp/client/index.html`) in the same commit.
- **Signer pinning:** outer Batch `Account` and every inner `Account` pinned to
  the caller's wallet.
- **Feasibility gate stays OFF by default** — no task in this plan flips it on;
  enabling per-stack is an ops step gated on the blocked-on dependencies in the
  spec.

---

### Task 1: Config feasibility gate

**Files:**
- Modify: `lfg_core/config.py`
- Test: `tests/test_batch_accept_config.py` (new)

**Interfaces:**
- Produces: `config.BATCH_ACCEPT_ENABLED: bool` (default False),
  `config.BATCH_ACCEPT_ENABLED_DEFAULT: str`, `config.BATCH_ACCEPT_MAX_INNER: int`
  (default 8).

- [ ] **Step 1: Write the failing test(s).** In `tests/test_batch_accept_config.py`,
  env-guard preamble at module top, then assert the shipped default is OFF and
  the cap is 8:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "test-zone.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")

  from lfg_core import config

  def test_batch_accept_default_off():
      assert config.BATCH_ACCEPT_ENABLED_DEFAULT == "0"
      assert config.env_flag("BATCH_ACCEPT_ENABLED", config.BATCH_ACCEPT_ENABLED_DEFAULT) is False

  def test_batch_accept_inner_cap_default():
      assert config.BATCH_ACCEPT_MAX_INNER == 8
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_batch_accept_config.py -q`
  (expect `AttributeError: module 'lfg_core.config' has no attribute 'BATCH_ACCEPT_ENABLED_DEFAULT'`).
- [ ] **Step 3: Implement.** In `lfg_core/config.py`, near `BULK_MINT_UI_ENABLED`:
  ```python
  BATCH_ACCEPT_ENABLED_DEFAULT = "0"  # named so a test locks the shipped default
  BATCH_ACCEPT_ENABLED = env_flag("BATCH_ACCEPT_ENABLED", BATCH_ACCEPT_ENABLED_DEFAULT)
  BATCH_ACCEPT_MAX_INNER = int(os.getenv("BATCH_ACCEPT_MAX_INNER", "8"))  # XLS-56 cap
  ```
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_batch_accept_config.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest tests/test_config*.py -q`.
- [ ] **Step 6: Commit** — `feat(config): XLS-56 batch-accept feasibility gate (#219)`.

---

### Task 2: Payload builder + chunking helper

**Files:**
- Modify: `lfg_core/xumm_ops.py`
- Test: `tests/test_batch_accept_payload.py` (new)

**Interfaces:**
- Produces: `xumm_ops.create_batch_accept_payload(account, offer_ids, *, return_url=None, user_token=None, platform=..., campaign=None) -> dict | None`
  and a pure helper `xumm_ops.chunk_offer_ids(offer_ids, size) -> list[list[str]]`.
- Consumes: `xumm_ops._create_xumm_payload`, `memos.build_memos_json`,
  `memos.ACTION_ACCEPT_OFFER`, `memos.INITIATOR_USER`.

- [ ] **Step 1: Write the failing test(s).** Env-guard preamble, then fake
  `_post_xumm_payload` (monkeypatch to capture the posted `payload["txjson"]`
  and return a canned `{"qr_url","xumm_url","uuid","pushed"}`):
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "test-zone.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")
  import asyncio
  import pytest
  from lfg_core import xumm_ops, config

  TF_INNER = 0x40000000

  def test_chunking():
      assert xumm_ops.chunk_offer_ids(["a"], 8) == [["a"]]
      assert xumm_ops.chunk_offer_ids(list("abcdefghij"), 8) == [list("abcdefgh"), list("ij")]

  def test_batch_payload_shape(monkeypatch):
      captured = {}
      async def fake_post(payload):
          captured["txjson"] = payload["txjson"]
          return {"qr_url": "q", "xumm_url": "x", "uuid": "u", "pushed": False}
      monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
      ids = ["OFFER1", "OFFER2", "OFFER3"]
      res = asyncio.run(xumm_ops.create_batch_accept_payload("rBUYER", ids))
      tx = captured["txjson"]
      assert tx["TransactionType"] == "Batch"
      assert tx["Account"] == "rBUYER"
      assert len(tx["RawTransactions"]) == 3
      for raw, oid in zip(tx["RawTransactions"], ids):
          inner = raw["RawTransaction"]
          assert inner["TransactionType"] == "NFTokenAcceptOffer"
          assert inner["Account"] == "rBUYER"
          assert inner["NFTokenSellOffer"] == oid
          assert inner["Flags"] & TF_INNER
      assert tx["SourceTag"] == config.SOURCE_TAG
      assert tx["Memos"]  # provenance memo present on the outer Batch
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_batch_accept_payload.py -q`
  (expect `AttributeError` on `chunk_offer_ids` / `create_batch_accept_payload`).
- [ ] **Step 3: Implement** in `lfg_core/xumm_ops.py` (see spec §1 for the full
  builder). Add:
  ```python
  _TF_INNER_BATCH_TXN = 0x40000000
  _BATCH_TF_INDEPENDENT = 0x00080000

  def chunk_offer_ids(offer_ids: list[str], size: int) -> list[list[str]]:
      return [offer_ids[i : i + size] for i in range(0, len(offer_ids), size)]
  ```
  `create_batch_accept_payload` builds the `RawTransactions` array (each inner
  pinned to `account`, `Flags=_TF_INNER_BATCH_TXN`), the outer
  `{"TransactionType": "Batch", "Account": account,
  "Flags": _BATCH_TF_INDEPENDENT, "RawTransactions": inner}`, and delegates to
  `_create_xumm_payload(txjson, options=_with_return_url({}, return_url),
  user_token=user_token, memos_json=memos.build_memos_json(memos.INITIATOR_USER,
  platform, memos.ACTION_ACCEPT_OFFER, campaign))`.
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_batch_accept_payload.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest tests/ -k "xumm or offer" -q`.
- [ ] **Step 6: Commit** — `feat(xumm): XLS-56 batch NFTokenAcceptOffer payload builder (#219)`.

---

### Task 3: Service endpoint + pending-offers capability flag

**Files:**
- Modify: `lfg_service/app.py` (add `handle_pending_offers_accept_batch`, route
  registration next to the existing `/api/offers/*`; add `"batch"` to
  `handle_pending_offers`' response)
- Test: `webapp/test_smoke.py` (extend) and/or `tests/test_batch_accept_endpoint.py` (new)

**Interfaces:**
- Produces: `POST /api/offers/accept-batch` → `{"batches": [{"qr","link","push","count"}...]}`
  or `{"single": true, "offer_index": ...}` or error codes
  `batch_disabled` (409) / `pending_unavailable` (503) / `offer_gone` (410).
- Modifies: `GET /api/offers/pending` response gains `"batch": bool`.
- Consumes: `xrpl_ops.get_account_nft_offers`, `xrpl_ops.filter_claimable_offers`,
  `xumm_ops.create_batch_accept_payload`, `xumm_ops.chunk_offer_ids`,
  `config.BATCH_ACCEPT_ENABLED`, `config.BATCH_ACCEPT_MAX_INNER`, `_push_token`,
  `_platform`, `memos.platform_for_surface`.

- [ ] **Step 1: Write the failing test(s).** Env-guard preamble. Using the
  existing smoke-test aiohttp harness (see `webapp/test_smoke.py` patterns),
  assert: (a) `/api/offers/pending` includes `"batch"` matching
  `config.BATCH_ACCEPT_ENABLED`; (b) with the gate patched OFF,
  `POST /api/offers/accept-batch` returns 409 `batch_disabled` and makes **no**
  XUMM call; (c) with the gate patched ON and `get_account_nft_offers` /
  `filter_claimable_offers` faked to return 3 claimable offers,
  `create_batch_accept_payload` is invoked once (3 ≤ 8) and the response
  `batches` has length 1 with `count == 3`; (d) a request whose survivors number
  1 returns `{"single": true}`; (e) zero survivors → 410 `offer_gone`.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest webapp/test_smoke.py -k batch -q`.
- [ ] **Step 3: Implement** `handle_pending_offers_accept_batch` per spec §2
  (gate check → dev-mode 501 → validate list → on-ledger re-verify preserving
  request order → single-fallback / 410 → chunk at `BATCH_ACCEPT_MAX_INNER` →
  one `create_batch_accept_payload` per chunk pinning `account=wallet`). Add
  `"batch": config.BATCH_ACCEPT_ENABLED` to the `handle_pending_offers` return.
  Register `app.router.add_post("/api/offers/accept-batch", handle_pending_offers_accept_batch)`
  beside the existing offer routes.
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest webapp/test_smoke.py -k batch -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest webapp/test_smoke.py tests/test_pending_offers.py -q`.
- [ ] **Step 6: Commit** — `feat(service): /api/offers/accept-batch behind feasibility gate (#219)`.

---

### Task 4: Pending-offers tray multi-select UI

**Files:**
- Modify: `webapp/client/app.js`, `webapp/client/index.html` (cache-buster bump),
  `webapp/client/style.v22.css` (checkbox + sticky button styles — reuse
  `.bulk-unit`/`.u-accept`)
- Test: manual smoke (client is no-build vanilla JS; covered by the Task 3
  endpoint tests + the manual checklist below)

**Interfaces:**
- Consumes: `GET /api/offers/pending` `batch` flag, `POST /api/offers/accept-batch`.

- [ ] **Step 1: Write the failing test(s).** No JS unit harness exists; the
  behavioral contract is covered by Task 3's endpoint tests. Add an assertion to
  `webapp/test_smoke.py` that `/api/offers/pending` exposes `batch` so the client
  branch has a stable contract. (If a fast path is wanted, assert the served
  `app.js` string contains `accept-batch` after implementation — optional.)
- [ ] **Step 2: Run to verify it fails** — `.venv/bin/pytest webapp/test_smoke.py -k "pending and batch" -q`.
- [ ] **Step 3: Implement** in `webapp/client/app.js`: in `openOffers()`, when
  the `/api/offers/pending` response's `batch` is true and `offers.length >= 2`,
  render a checkbox in each `offerRow` and a sticky **"Accept selected (1
  signature)"** button that POSTs the checked `offer_index` list to
  `/api/offers/accept-batch`, then renders one `.u-accept` QR block per returned
  chunk (copy: *"Scan once to claim these N."*); a `{single:true}` response
  routes through the existing `offerAccept`. Leave the per-row Accept path intact
  for the gate-off / single-offer case. Bump the `?v=` cache-buster on the
  `app.js` module import in `webapp/client/index.html` in this same commit.
- [ ] **Step 4: Run to verify it passes** — `.venv/bin/pytest webapp/test_smoke.py -k "pending and batch" -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest webapp/test_smoke.py -q`.
- [ ] **Step 6: Commit** — `feat(client): multi-select batch accept in pending-offers tray (#219)`.

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/pytest -q`.
- [ ] Run lint/format/type: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy lfg_core lfg_service`.
- [ ] Confirm `BATCH_ACCEPT_ENABLED` remains OFF by default (Task 1 test) — the
  branch must ship dark; enabling is a separate ops step blocked on Xaman +
  amendment verification (spec "Blocked-on").
- [ ] Add the three new env vars to the CLAUDE.md `.env` block
  (`BATCH_ACCEPT_ENABLED`, `BATCH_ACCEPT_MAX_INNER`) with "optional; off by
  default (#219), blocked on XLS-56 amendment + Xaman Batch signing".
- [ ] Push the branch and `gh pr create` (non-draft, per repo rules). PR body
  must state the feature ships **gate-off** and list the blocked-on
  dependencies. **No AI attribution** in the commit trailers or PR body.
- [ ] Wait for Greptile + CodeRabbit; resolve every actionable finding (fix in
  code AND reply on its thread naming the fixing commit) before merge.
- [ ] Note in the PR / on issue #219 the open feasibility questions (Xaman Batch
  signing sign-test, XLS-56 mainnet/testnet amendment status) so the enable step
  is tracked.
