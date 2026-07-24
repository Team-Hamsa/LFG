# Identity wallet-dedup (shared-wallet "same human" bucket) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a thin, first-class "same human" account-bucket lookup layer over
the existing wallet-keyed `identities` table (from #90), expose the caller's
bucket via `GET /api/account`, and ship a read-only audit script that surfaces
which identities collapse into shared-wallet buckets. No schema change, no data
migration, no on-ledger transaction.

**Architecture:** Three independent seams —
- **A. `lfg_service/identity.py`** — `account_bucket`, `bucket_members`,
  `same_human` (pure reads over `resolve` + `identities_for_wallet`).
- **B. `lfg_service/app.py::handle_account`** — additive `bucket_size` +
  `platforms` fields on the existing `/api/account` response.
- **C. `scripts/audit_identity_buckets.py`** — informational bucket-graph report.

Seams A and C can be built in parallel; B depends on A only for consistency of
the "bucket = identities_for_wallet" definition (it can reuse the value it
already computes).

**Tech Stack:** Python 3 / aiohttp / asyncio / pytest; no client (vanilla-JS)
change required for the read path.

## Global Constraints

- **No transaction is built** by this feature, so SourceTag `2606160021` +
  provenance memos are N/A — but if any step is tempted to add a signed/submitted
  tx, it MUST carry SourceTag + memos. (It should not.)
- **Cross-platform isolation:** bucket membership derives ONLY from a shared
  `wallet`. Never add an id-collision join. `same_human` must be False when
  either identity is unregistered (both-None must not match).
- **XRPL wallets are case-sensitive** — compare verbatim, never `.lower()`.
- **Pre-push gate** (ruff `--fix`, ruff-format, mypy from `.venv`, gitleaks,
  pytest, validate-trait-config) must pass; never `--no-verify`.
- **No `app.js` change** here, so no cache-buster bump in
  `webapp/client/index.html` is required. (If a later UI task renders
  `bucket_size`, that task bumps the cache-buster in the same commit.)
- Match the existing `identity.py` style: `sqlite3.connect(DATABASE)`,
  try/except logging, `finally: conn.close()`.

---

### Task 1: Bucket lookup layer in `identity.py`

**Files:**
- Modify: `lfg_service/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Produces: `account_bucket(platform, uid) -> str | None`,
  `bucket_members(platform, uid) -> list[dict]`,
  `same_human(pa, ua, pb, ub) -> bool`
- Consumes: existing `resolve`, `identities_for_wallet` (same module).

- [ ] **Step 1: Write the failing test(s)** — append to `tests/test_identity.py`
  (this file imports only `lfg_service.identity`, so NO lfg_core env-guard
  preamble is needed; reuse the existing `_fresh_db` helper):
  ```python
  def test_account_bucket_returns_wallet_or_none(tmp_path, monkeypatch):
      _fresh_db(tmp_path, monkeypatch)
      identity.ensure_identities_table()
      assert identity.account_bucket("discord", "1") is None
      identity.link("discord", "1", "bob", "rW")
      assert identity.account_bucket("discord", "1") == "rW"

  def test_bucket_members_groups_cross_platform_same_wallet(tmp_path, monkeypatch):
      _fresh_db(tmp_path, monkeypatch)
      identity.ensure_identities_table()
      identity.link("discord", "1", "alice", "rSHARED")
      identity.link("telegram", "2", "alice_tg", "rSHARED")
      members = identity.bucket_members("discord", "1")
      keys = {(m["platform"], m["platform_user_id"]) for m in members}
      assert keys == {("discord", "1"), ("telegram", "2")}
      # lone identity is a bucket of one
      identity.link("discord", "9", "solo", "rSOLO")
      assert len(identity.bucket_members("discord", "9")) == 1
      # unregistered -> empty bucket
      assert identity.bucket_members("discord", "404") == []

  def test_same_human_matches_only_same_proven_wallet(tmp_path, monkeypatch):
      _fresh_db(tmp_path, monkeypatch)
      identity.ensure_identities_table()
      identity.link("discord", "1", "alice", "rSHARED")
      identity.link("telegram", "2", "alice_tg", "rSHARED")
      assert identity.same_human("discord", "1", "telegram", "2") is True
      identity.link("telegram", "3", "carol", "rOTHER")
      assert identity.same_human("discord", "1", "telegram", "3") is False
      # unregistered on either side never matches (both-None must be False)
      assert identity.same_human("discord", "1", "discord", "404") is False
      assert identity.same_human("discord", "404", "discord", "405") is False

  def test_same_human_id_collision_does_not_bucket(tmp_path, monkeypatch):
      _fresh_db(tmp_path, monkeypatch)
      identity.ensure_identities_table()
      identity.link("discord", "55", "d", "rA")
      identity.link("telegram", "55", "t", "rB")
      assert identity.same_human("discord", "55", "telegram", "55") is False
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_identity.py -k "bucket or same_human" -q` → expect `AttributeError` (functions not defined).
- [ ] **Step 3: Implement** — add the three functions to `lfg_service/identity.py`
  exactly as specified in the design (§A): `account_bucket` = thin `resolve`
  wrapper; `bucket_members` = `identities_for_wallet(resolve(...))` guarded on a
  truthy wallet, `[]` otherwise; `same_human` resolves both and returns
  `wa is not None and wa == wb`. Full type hints for mypy.
- [ ] **Step 4: Run to verify they pass** — same pytest command, all green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest tests/test_identity.py -q` (confirm the existing isolation/link tests still pass).
- [ ] **Step 6: Commit** — `feat(identity): add same-human account-bucket lookup (account_bucket/bucket_members/same_human) (#206)`

---

### Task 2: Enrich `GET /api/account` with `bucket_size` + `platforms`

**Files:**
- Modify: `lfg_service/app.py` (`handle_account`)
- Test: the existing account-endpoint test module (locate with
  `grep -rln "api/account\|handle_account" tests/`; add a case there, or create
  `tests/test_service_account.py` with the standard env-guard preamble if none
  covers it).

**Interfaces:**
- Produces: `/api/account` JSON gains `bucket_size: int`, `platforms: list[str]`
  (sorted, deduped). Existing `wallet` + `identities` keys unchanged.

- [ ] **Step 1: Write the failing test(s)** — new test file needs the env-guard
  preamble at module top (it imports `lfg_service.app`, which pulls `lfg_core`):
  ```python
  import os
  os.environ.setdefault("BUNNY_PULL_ZONE", "test-zone")
  os.environ.setdefault("LAYER_SOURCE", "local")
  # ... aiohttp test client setup mirroring an existing tests/test_service_*.py ...
  ```
  Assert: an authed `GET /api/account` for a caller whose wallet has two
  identities returns `bucket_size == 2` and `sorted(platforms) == ["discord","telegram"]`;
  a single-identity caller returns `bucket_size == 1`. Reuse the auth/session
  fixtures from an existing `tests/test_service_*.py` (grep for how `@require_wallet`
  handlers are exercised, e.g. `tests/test_web_signin_endpoint.py`).
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_service_account.py -q` → `KeyError`/assertion on the missing fields.
- [ ] **Step 3: Implement** — in `handle_account`, after computing `identities`,
  add:
  ```python
  platforms = sorted({str(i["platform"]) for i in identities})
  return web.json_response({
      "wallet": wallet,
      "identities": identities,
      "bucket_size": len(identities),
      "platforms": platforms,
  })
  ```
  Keep the privacy note intact (caller sees only their own bucket).
- [ ] **Step 4: Run to verify they pass** — same pytest command, green.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest tests/ -k "service and account" -q` plus a run of any test asserting the old `/api/account` shape (additive keys must not break them).
- [ ] **Step 6: Commit** — `feat(service): surface bucket_size + platforms on /api/account (#206)`

---

### Task 3: Read-only bucket-graph audit script

**Files:**
- Create: `scripts/audit_identity_buckets.py`
- Test: `tests/test_audit_identity_buckets.py`

**Interfaces:**
- Produces: CLI `audit_identity_buckets.py [--json]` printing total identities,
  total buckets (distinct wallets), and each multi-identity bucket. A callable
  `collect_buckets(db_path) -> dict[str, list[dict]]` (wallet → identity rows) so
  the test can assert without shelling out. Exit 0 always.
- Consumes: `lfg_service.identity` / direct `sqlite3` read of `identities`.

- [ ] **Step 1: Write the failing test(s)** — env-guard preamble at top; build a
  temp DB via `identity.ensure_identities_table()` + `identity.link(...)` (point
  `identity.DATABASE` at the tmp db as `_fresh_db` does), seed one shared-wallet
  cross-platform pair + one lone identity, then:
  ```python
  buckets = audit_identity_buckets.collect_buckets(db_path)
  multi = {w: rows for w, rows in buckets.items() if len(rows) > 1}
  assert list(multi) == ["rSHARED"]
  assert {(r["platform"], r["platform_user_id"]) for r in multi["rSHARED"]} \
      == {("discord", "1"), ("telegram", "2")}
  ```
- [ ] **Step 2: Run to verify they fail** — `.venv/bin/python -m pytest tests/test_audit_identity_buckets.py -q` → import/AttributeError.
- [ ] **Step 3: Implement** — `collect_buckets` groups `identities` rows by
  `wallet`; `main()` uses `argparse` (`--json`), prints a human summary
  (counts + each multi-identity bucket) or `json.dumps` of the multi-buckets;
  `sys.exit(0)`. Match the loopback-only, no-DB-write posture of other
  `scripts/*.py` audits. Full type hints.
- [ ] **Step 4: Run to verify they pass** — same pytest command, green; also run
  the script by hand against the test/staging app DB and eyeball output.
- [ ] **Step 5: Wider suite / regression run** — `.venv/bin/python -m pytest tests/test_identity.py tests/test_audit_identity_buckets.py -q`.
- [ ] **Step 6: Commit** — `feat(scripts): audit_identity_buckets — report shared-wallet identity buckets (#206)`

---

### Final Task: Full gate + PR

- [ ] Run the full suite: `.venv/bin/python -m pytest -q` (green).
- [ ] Run linters/types exactly as the pre-push hook does: `ruff check --fix .`,
  `ruff format .`, `mypy` (from `.venv`), gitleaks, validate-trait-config —
  never `--no-verify`.
- [ ] Push the feature branch (worktree already isolated). Ensure the worktree
  `.venv` symlink exists so the pre-push gate actually runs (a missing `.venv`
  silently skips it).
- [ ] `gh pr create` against `Team-Hamsa/LFG`, **non-draft**, body referencing
  #206 and cross-referencing #207 (profiles) — **no AI attribution / no
  Co-Authored-By trailer**. Note in the body that the issue's `wallet_links` /
  free-mint premise was stale (never merged) and this PR builds the bucket layer
  on the existing `identities.wallet` foundation instead; free-mint per-bucket
  gating is deferred to whenever free-mint ships.
- [ ] Wait for **Greptile** and **CodeRabbit**. Read Greptile's verdict from the
  `Greptile Review` check-run `output.summary` (a clean review posts no comment).
  Resolve every actionable finding: fix in code **and** reply on its thread
  naming the fixing commit (or why declined) before merge.
