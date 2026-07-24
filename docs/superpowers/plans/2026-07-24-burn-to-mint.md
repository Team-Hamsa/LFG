# Burn-to-mint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user burn M live LFG characters they own to earn M **freshly
minted**, cap-exempt (`MAX_COLLECTION_SIZE`-bypassing) NFTs — supply-neutral —
by filling the `BurnEntitlement` seam (#215) and reusing `mint_flow.mint_one_unit`.

**Architecture:** Three independent seams —
1. **Entitlement gate** (`lfg_core/entitlement.py`): construct `BurnEntitlement`
   only from a confirmed-burn id list (CodeRabbit gate: cap-exemption trails a
   verified burn).
2. **Burn-to-mint flow** (`lfg_core/burn_mint_flow.py`, new): durable job —
   `VERIFYING` (fail-closed on-ledger ownership/issuer/burnable) → `BURNING`
   (issuer-burn M, persist per burn, double-spend re-verify) → `FULFILLING`
   (cap-exempt reuse of `mint_one_unit` + `Unit` states + `mint_credits` tail) →
   `DONE`. Deliberately separate from the payment-critical `BulkMintJob`.
3. **Service + client** (`lfg_service/app.py`, `webapp/client/`): `POST
   /api/mint/burn`, status/active polls, per-unit accept; Activity affordance.

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; vanilla no-build JS client.

## Global Constraints

- **SourceTag `2606160021` + provenance memos on every tx.** The burn goes
  through `xrpl_ops.burn_nft` (already sets `source_tag=config.SOURCE_TAG` +
  `memos.build_memo_models(INITIATOR_BACKEND, platform, ACTION_BURN)`); the mint
  through `mint_flow.mint_one_unit` → `xrpl_ops.mint_nft` (`ACTION_MINT`).
  Thread the originating surface with `memos.platform_for_surface(job.platform)`.
  Never add a new tx path without SourceTag + memo.
- **Pre-push gate** (`.pre-commit-config.yaml`, blocking): ruff (--fix),
  ruff-format, mypy (from project `.venv`), gitleaks, pytest,
  validate-trait-config. Local and CI both block; **never** `--no-verify`.
  In a worktree, ensure the `.venv` symlink exists or the gate silently skips.
- **Cache-buster:** any `webapp/client/app.js` (or ES-module import) change bumps
  the `?v=` in `webapp/client/index.html` in the **same commit**.
- **No-forced-burns / no-custody:** burns are issuer-authority `NFTokenBurn` on
  the caller's own tokens, authorized by the authed endpoint, with on-ledger
  fail-closed ownership re-verification immediately before each burn. Ledger is
  source of truth; the app never holds the user's token.
- **Fail-safe invariant:** M burned ⇒ exactly M fresh NFTs **or** M durable
  `mint_credits` — never a loss, never a cap overshoot.
- **Tests:** every new `tests/` file carries the env-guard preamble at module top
  (`os.environ.setdefault("BUNNY_PULL_ZONE", ...)` / `LAYER_SOURCE="local"`) so
  frozen config constants don't strand full-suite ordering.

---

### Task 1: Entitlement gate

**Files:**
- Modify: `lfg_core/entitlement.py`
- Test: `tests/test_entitlement.py` (create if absent)

**Interfaces:**
- Produces: `build_burn_entitlement(quantity: int, burn_nft_ids: list[str]) -> BurnEntitlement` (was `NotImplementedError` stub).
- Consumes: `BurnEntitlement`, `from_dict` (unchanged).

- [ ] **Step 1: Write the failing test(s)** — with env-guard preamble:
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "example.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")
  import pytest
  from lfg_core import entitlement

  def test_build_burn_entitlement_ok():
      e = entitlement.build_burn_entitlement(2, ["A", "B"])
      assert e.source == "burn" and e.cap_exempt is True and e.quantity == 2
      assert e.to_dict() == {"source": "burn", "quantity": 2, "burn_nft_ids": ["A", "B"]}

  @pytest.mark.parametrize("q,ids", [(0, []), (2, ["A"]), (1, [""]), (-1, ["A"])])
  def test_build_burn_entitlement_rejects_unbacked(q, ids):
      with pytest.raises(ValueError):
          entitlement.build_burn_entitlement(q, ids)

  def test_from_dict_roundtrips_burn():
      e = entitlement.from_dict({"source": "burn", "quantity": 1, "burn_nft_ids": ["X"]})
      assert isinstance(e, entitlement.BurnEntitlement) and e.cap_exempt is True
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_entitlement.py -q` (expect `NotImplementedError` / failures).
- [ ] **Step 3: Implement** — replace the stub body:
  ```python
  def build_burn_entitlement(quantity: int, burn_nft_ids: list[str]) -> BurnEntitlement:
      if quantity < 1 or quantity != len(burn_nft_ids) or not all(burn_nft_ids):
          raise ValueError("burn entitlement requires quantity == confirmed burn count")
      return BurnEntitlement(quantity=quantity, burn_nft_ids=list(burn_nft_ids))
  ```
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_entitlement.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest tests/ -q -k "entitlement or bulk"`.
- [ ] **Step 6: Commit** — `feat(entitlement): construct BurnEntitlement only from a confirmed-burn list (#220)`.

---

### Task 2: Verify + burn preflight (`burn_mint_flow.py`)

**Files:**
- Create: `lfg_core/burn_mint_flow.py`
- Test: `tests/test_burn_mint_flow.py`

**Interfaces:**
- Produces: `BurnTarget` dataclass; `BurnMintJob` (fields per spec: `id`,
  `discord_id`, `wallet_address`, `platform`, `push_user_token`, `return_url`,
  `network`, `created_at`, `state`, `targets`, `units`, `entitlement`);
  `async verify_targets(job) -> list[str] | None` (per-id error reasons, or None
  = all eligible); `async run_burns(job) -> None`; `persist(job)` /
  `load_all_resumable()` / `_record_path` (own `BURN_MINT_JOBS_DIR`, atomic
  tmp-file + `os.replace`, never-raise contract mirroring `bulk_mint_flow`).
- Consumes: `xrpl_ops.nft_info`, `xrpl_ops.get_account_nfts`,
  `xrpl_ops.burn_nft`, `xrpl_ops.NFT_FLAG_BURNABLE`, `config.SWAP_ISSUER_ADDRESS`,
  `config.XRPL_NETWORK`, `config.BULK_MINT_MAX` (or new `BURN_MINT_MAX`),
  `memos.platform_for_surface`, `bulk_mint_flow.Unit`.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble, then fakes for
  `xrpl_ops`. Cover:
  - `verify_targets` rejects wrong-owner, wrong-issuer, non-burnable-flag,
    already-burned, and indeterminate (`nft_info` → None) targets, returning a
    per-id reason list; a fully-eligible set returns `None`.
  - all-or-nothing: monkeypatch `xrpl_ops.burn_nft` to a spy; when any target is
    ineligible, `run_burns` is never reached (guard lives in the handler /
    a precondition assert) — assert the spy is not called.
  - `run_burns` burns each eligible target, sets `state="burned"`/`burn_tx`,
    persists after each, and a target that `nft_info` shows transferred away
    between verify and burn is marked `failed` (double-spend guard) so
    `quantity` (confirmed burns) < M.
  - `BURN_MINT_MAX` / dedupe bound enforced.
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "example.b-cdn.net")
  os.environ.setdefault("LAYER_SOURCE", "local")
  import pytest
  from lfg_core import burn_mint_flow, xrpl_ops

  @pytest.mark.asyncio
  async def test_verify_rejects_non_burnable(monkeypatch, tmp_path):
      monkeypatch.setattr(burn_mint_flow, "BURN_MINT_JOBS_DIR", str(tmp_path))
      async def fake_info(nft_id, clio=None):
          return {"owner": "rUSER", "issuer": "rISS", "flags": 0x0018, "is_burned": False}
      monkeypatch.setattr(xrpl_ops, "nft_info", fake_info)
      monkeypatch.setattr("lfg_core.config.SWAP_ISSUER_ADDRESS", "rISS")
      job = burn_mint_flow.BurnMintJob("d1", "rUSER", ["N1"], platform="webapp")
      reasons = await burn_mint_flow.verify_targets(job)
      assert reasons and "N1" in reasons[0]
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_burn_mint_flow.py -q` (module/attrs don't exist yet).
- [ ] **Step 3: Implement** — write `burn_mint_flow.py`:
  - `BurnTarget(nft_id, state="pending", burn_tx=None)`; states
    `VERIFYING/BURNING/FULFILLING/DONE/FAILED`, `TERMINAL_STATES={DONE, FAILED}`.
  - `BurnMintJob.__init__(discord_id, wallet_address, nft_ids, platform, ...)`:
    dedupe + clamp `nft_ids` to `BURN_MINT_MAX`; build `targets`; `state=VERIFYING`.
  - `verify_targets`: per target, `nft_info` (fail-closed on None); check
    `owner == wallet`, `issuer == config.SWAP_ISSUER_ADDRESS`,
    `flags & xrpl_ops.NFT_FLAG_BURNABLE`, `not is_burned`; collect reasons.
  - `run_burns`: persist (targets `pending`) FIRST; per target re-verify owner
    on-ledger, `burn_nft(nft_id, owner=wallet, platform=platform_for_surface(...))`,
    set `burned`/`burn_tx` (or `failed`), persist after each; then set
    `quantity`, build `units`, `entitlement = entitlement.build_burn_entitlement(
    quantity, burned_ids)`, `state=FULFILLING`, persist. Idempotent on resume: a
    target already gone/burned is counted `burned`, never re-burned.
  - `persist`/`delete_record`/`load_all_resumable` (`BURNING`/`FULFILLING`
    resumable) mirrored from `bulk_mint_flow`, own dir.
  - `serialize`/`from_serialized`/`to_dict` (state, targets, units, minted/offered
    counts).
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_burn_mint_flow.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest tests/ -q -k "burn or bulk or mint"`.
- [ ] **Step 6: Commit** — `feat(burn-mint): verify + issuer-burn preflight for burn-to-mint (#220)`.

---

### Task 3: Cap-exempt fulfillment loop

**Files:**
- Modify: `lfg_core/burn_mint_flow.py`
- Test: `tests/test_burn_mint_flow.py` (extend)

**Interfaces:**
- Produces: `async _fulfill_unit(job, unit)` (cap-exempt only — NO headroom),
  `async run_burn_mint_job(job)` (drives `BURNING`→`FULFILLING`→`DONE`).
- Consumes: `mint_flow.mint_one_unit`, `mint_flow._allocate_nft_number`,
  `mint_credits.add_credit`, `bulk_mint_flow._ensure_offer` (reuse — it only
  touches `job.wallet_address`/`platform`/`nft_id` and the offer RPCs; if its
  `persist` coupling is awkward, inline a trimmed copy).

- [ ] **Step 1: Write the failing test(s)** — extend the test module:
  - mint all-attempts-fail → unit becomes a `mint_credits` row (assert
    `mint_credits.add_credit` called `quantity` times; job reaches a resolved
    terminal/non-loss state), and assert **no** `headroom.*` function is called
    (patch `headroom.try_reserve`/`reserved_for`/`retire_to_pending` to raise).
  - mint-ok / offer-fail leaves `MINTED`; the bounded final re-offer pass drives
    it to `OFFERED`.
  - happy path: M eligible → M burns → M `OFFERED` units → `state == DONE`.
  ```python
  @pytest.mark.asyncio
  async def test_fulfillment_never_touches_headroom(monkeypatch, tmp_path):
      monkeypatch.setattr(burn_mint_flow, "BURN_MINT_JOBS_DIR", str(tmp_path))
      import lfg_core.headroom as hr
      for fn in ("try_reserve", "reserved_for", "retire_to_pending", "release"):
          monkeypatch.setattr(hr, fn, lambda *a, **k: (_ for _ in ()).throw(AssertionError("headroom touched")))
      # ... drive a burn-mint job with faked mint_one_unit -> assert DONE
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_burn_mint_flow.py -q`.
- [ ] **Step 3: Implement** —
  - `_fulfill_unit`: cap-exempt clone of `bulk_mint_flow._fulfill_unit` with the
    reservation re-check and `retire_to_pending` branches removed entirely; keep
    the `on_mint` persist-MINTED-first pattern, the mint/offer result handling,
    and the `mint_credits` tail (with the "credit write failed → leave retryable"
    guard). No `headroom` import.
  - `run_burn_mint_job`: entry guard (terminal → return); if `state==BURNING`
    call `run_burns`; then the fulfillment loop (skip `OFFERED`/`UNIT_FAILED`;
    `MINTED` → `_ensure_offer`; else `_fulfill_unit`; persist each); bounded final
    re-offer pass; conditional completion (`all OFFERED/UNIT_FAILED` → `DONE`,
    else stay `FULFILLING`). Wrap in the same `CancelledError`-reraise /
    post-work-failure-stays-resumable posture as `run_bulk_mint_job` (but there
    is no payment, so a pre-fulfillment failure with zero confirmed burns is
    `FAILED`, while any confirmed burn keeps the job resumable so owed mints/credits
    are never dropped).
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_burn_mint_flow.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest tests/ -q`.
- [ ] **Step 6: Commit** — `feat(burn-mint): cap-exempt fulfillment reusing mint_one_unit + mint_credits tail (#220)`.

---

### Task 4: Service endpoints + startup resume

**Files:**
- Modify: `lfg_service/app.py`
- Test: `tests/test_burn_mint_api.py` (or extend `webapp` smoke)

**Interfaces:**
- Produces: `handle_burn_mint_start` (`POST /api/mint/burn`),
  `handle_burn_mint_status` (`GET /api/mint/burn/{session_id}`),
  `handle_burn_mint_active` (`GET /api/mint/burn/active`),
  `handle_burn_mint_unit_accept`
  (`POST /api/mint/burn/{session_id}/units/{index}/accept`), and a
  `resume_burn_jobs()` wired via `app.on_startup` next to `_start_bulk_resume`.
  New `burn_sessions` registry mirroring `bulk_sessions`.
- Consumes: `@require_auth`, `request["wallet"]`, `_platform`, `_push_token`,
  `_request_return_url`, `_active_session`, `_prune_sessions`,
  `burn_mint_flow.*`.

- [ ] **Step 1: Write the failing test(s)** — with the service test harness:
  - `POST /api/mint/burn` with ineligible ids → `400 ineligible_nfts`
    (per-id reasons in body); no burns issued.
  - happy path (faked xrpl) → job registered, background task launched, `to_dict`
    returned; polling `/api/mint/burn/{id}` reaches `DONE` with M offered units.
  - a second concurrent `POST` for the same user → `409 already in progress`.
  - `invalid_quantity` for empty/oversized id lists (reuse the bulk int-guard
    discipline — reject non-list / non-str ids).
  - unit accept builds a XUMM payload (fake `xumm_ops`).
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest tests/test_burn_mint_api.py -q`.
- [ ] **Step 3: Implement** — add handlers mirroring the bulk-mint handlers:
  resolve platform/return-url/push-token BEFORE the one-active-job check
  (await-free check→insert window), run `verify_targets` synchronously (return
  `400` with reasons on failure), register in `burn_sessions`, launch
  `asyncio.create_task(burn_mint_flow.run_burn_mint_job(job))`. Register routes in
  the router block next to the `/api/mint/bulk` routes. Add `resume_burn_jobs`
  and append it to `app.on_startup`.
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest tests/test_burn_mint_api.py -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest tests/ -q && .venv/bin/pytest webapp/ -q`.
- [ ] **Step 6: Commit** — `feat(service): POST /api/mint/burn + status/active/accept + startup resume (#220)`.

---

### Task 5: Client affordance (Activity)

**Files:**
- Modify: `webapp/client/app.js` (+ any burn ES-module), `webapp/client/index.html`
  (cache-buster bump)
- Test: `webapp/` smoke test as applicable

**Interfaces:** consumes `/api/market/mine` (`unlisted_characters`) for the
selectable roster and the `/api/mint/burn*` endpoints from Task 4.

- [ ] **Step 1: Write the failing test(s)** — extend the webapp smoke test to
  assert the burn endpoints are reachable and (if a JS harness exists) the
  affordance renders behind its feature flag.
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/pytest webapp/ -q`.
- [ ] **Step 3: Implement** — a "Burn to mint" flow: multi-select owned live
  characters, confirm (in-app overlay — Discord's iframe makes `window.confirm`
  a silent no-op), `POST /api/mint/burn`, poll status, render burn-then-mint
  progress + per-unit accept buttons. Gate behind an env-driven UI flag mirroring
  `BULK_MINT_UI_ENABLED` (endpoints stay live regardless). **Bump the `?v=`
  cache-buster in `index.html` (and any changed ES-module import) in this commit.**
- [ ] **Step 4: Run to verify they pass** — `.venv/bin/pytest webapp/ -q`.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/pytest -q`.
- [ ] **Step 6: Commit** — `feat(activity): burn-to-mint selection + progress UI behind flag (#220)`.

---

### Final Task: Full gate + PR

- [ ] Run the full pre-push gate locally: `.venv/bin/pytest -q`, `.venv/bin/ruff
  check .`, `.venv/bin/ruff format --check .`, `.venv/bin/mypy .` (from the
  project `.venv`), gitleaks, validate-trait-config. Fix everything; **never**
  `--no-verify`.
- [ ] Push the branch and `gh pr create` against `Team-Hamsa/LFG`, non-draft, per
  repo rules: **no AI attribution** in the commits or PR body.
- [ ] Wait for **Greptile** and **CodeRabbit**; resolve every actionable finding
  by fixing it in code **and** replying on its thread naming the fixing commit —
  in particular the PR #225 gate (cap-exemption must trail a verified burn) must
  be visibly satisfied. Do not merge until both reviews are clean.
- [ ] After merge: this is a testnet-first feature — verify on staging (`main`)
  before `scripts/promote.sh` to prod (`deploy`), and set any new env flag
  (`BURN_MINT_ENABLED` / UI flag) per stack. Then link the spec + plan permalinks
  back onto issue #220 (repo CLAUDE.md convention).
