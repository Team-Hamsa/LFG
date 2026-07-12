# Root loose-file cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the four loose root Python helpers into their proper packages (`lfg_core/`, `scripts/`), fix every reference, and tidy the untracked local clutter — leaving the repo root professional and the test suite green.

**Architecture:** Pure mechanical relocation (Approach A, flat into existing packages), clean break (no back-compat shims). `git mv` preserves history; imports rewritten to absolute `from lfg_core… ` / adjusted subprocess paths. Housekeeping is untracked-file moves + worktree pruning with no git trace beyond one `.gitignore` line.

**Tech Stack:** Python 3.10, pytest, ruff, mypy (strict), git worktrees, pm2 (deploy only).

## Global Constraints

- **Clean break, no shims** — nothing outside the repo imports these by bare name; pm2 runs `main.py`/`run_telegram.py` (unchanged, stay at root).
- **`git mv`** for every move (preserve history).
- **Historical docs frozen** — never edit `docs/superpowers/specs/*` or `docs/superpowers/plans/*` older than today; they are point-in-time records.
- **Zero-straggler gate** — after all moves, this must return nothing:
  ```bash
  grep -rnE '^[[:space:]]*(import (db_helpers|user_db|init_db|rarity_admin)($|[[:space:]#])|from (db_helpers|user_db|init_db|rarity_admin) import )' --include='*.py' . | grep -vE '/\.venv/|/\.claude/worktrees/'
  ```
- **Full gate before push** — `ruff check .`, `ruff format --check .`, `mypy .`, `pytest -q` all green (~1495 tests).
- **Live DBs untouched** (`lfg_nfts.db`, `history_*.db*`, `onchain_*.db*`, `rarity.db`).
- **Commit trailer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Branch:** `chore/root-cleanup` (already created off `main` @ 3ffb44f).

---

### Task 1: Move `db_helpers.py` → `lfg_core/db_helpers.py`

**Files:**
- Move: `db_helpers.py` → `lfg_core/db_helpers.py`
- Modify: `lfg_core/mint_flow.py:17`, `tests/test_app_db_path.py:21`, `tests/test_rarity.py:542`

**Interfaces:**
- Produces: module `lfg_core.db_helpers` exporting `get_next_nft_number() -> int`, `record_nft_mint(...)`, `get_nft_data(nft_number: int) -> dict | None`. Internal imports (`from lfg_core import config`) already absolute — no change needed inside the file.

- [ ] **Step 1: Move the file**
```bash
git mv db_helpers.py lfg_core/db_helpers.py
```

- [ ] **Step 2: Rewrite the three importers**

`lfg_core/mint_flow.py:17`:
```python
from lfg_core.db_helpers import get_next_nft_number, record_nft_mint
```
`tests/test_app_db_path.py:21` (`import db_helpers` →):
```python
from lfg_core import db_helpers
```
`tests/test_rarity.py:542` (the in-function `    import db_helpers` →, preserving indentation):
```python
    from lfg_core import db_helpers
```

- [ ] **Step 3: Run the affected tests — expect PASS**
```bash
.venv/bin/pytest tests/test_app_db_path.py tests/test_rarity.py -q
```
Expected: all pass. (If run before Step 2, the `import db_helpers` lines raise `ModuleNotFoundError` — that is the red state this task resolves.)

- [ ] **Step 4: Confirm mint_flow imports cleanly**
```bash
.venv/bin/python -c "import lfg_core.mint_flow; print('mint_flow OK')"
```
Expected: `mint_flow OK`

- [ ] **Step 5: Commit**
```bash
git add lfg_core/db_helpers.py lfg_core/mint_flow.py tests/test_app_db_path.py tests/test_rarity.py
git commit -m "refactor: move db_helpers into lfg_core/

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Move `user_db.py` → `lfg_core/user_db.py`

**Files:**
- Move: `user_db.py` → `lfg_core/user_db.py`
- Modify: `surfaces/discord_bot/bot.py:13`, `surfaces/discord_bot/views.py:16`, `lfg_service/identity.py:9`, `lfg_service/app.py:59`, `webapp/test_smoke.py:27`, `tests/test_event_endpoints.py:166`, `tests/test_app_db_path.py:22`

**Interfaces:**
- Produces: module `lfg_core.user_db` exporting `DATABASE` (= `config.DB_PATH`), `create_users_table() -> None`, `register_user(discord_id, discord_name, wallet) -> bool`, `get_user(discord_id) -> dict | None`, `get_all_registered_users() -> list[dict]`. Monkeypatch targets (`user_db.DATABASE`, `user_db.get_user`) keep working as long as the name `user_db` is bound to the module.

- [ ] **Step 1: Move the file**
```bash
git mv user_db.py lfg_core/user_db.py
```

- [ ] **Step 2: Rewrite the importers**

`surfaces/discord_bot/bot.py:13`:
```python
from lfg_core.user_db import create_users_table
```
`surfaces/discord_bot/views.py:16`:
```python
from lfg_core.user_db import get_user
```
`lfg_service/identity.py:9`:
```python
from lfg_core.user_db import DATABASE  # single source of truth for the db path
```
`lfg_service/app.py:59`:
```python
from lfg_core.user_db import create_users_table, get_user, register_user
```
`webapp/test_smoke.py:27` (`import user_db  # noqa: E402` →):
```python
from lfg_core import user_db  # noqa: E402
```
`tests/test_event_endpoints.py:166` (the in-function `    import user_db` →, preserving indentation):
```python
    from lfg_core import user_db
```
`tests/test_app_db_path.py:22` (`import user_db` →):
```python
from lfg_core import user_db
```

- [ ] **Step 3: Run the affected tests — expect PASS**
```bash
.venv/bin/pytest tests/test_app_db_path.py tests/test_event_endpoints.py webapp/test_smoke.py -q
```
Expected: all pass.

- [ ] **Step 4: Confirm the prod importers load cleanly**
```bash
.venv/bin/python -c "import lfg_service.app, lfg_service.identity, surfaces.discord_bot.views; print('prod importers OK')"
```
Expected: `prod importers OK` (bot.py pulls in discord runtime; `views` covers the surface import path).

- [ ] **Step 5: Commit**
```bash
git add lfg_core/user_db.py surfaces/discord_bot/bot.py surfaces/discord_bot/views.py \
        lfg_service/identity.py lfg_service/app.py webapp/test_smoke.py \
        tests/test_event_endpoints.py tests/test_app_db_path.py
git commit -m "refactor: move user_db into lfg_core/

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Move `init_db.py` → `scripts/init_db.py`

**Files:**
- Move: `init_db.py` → `scripts/init_db.py`
- Modify: `scripts/init_db.py` (add repo-root bootstrap), `tests/test_app_db_path.py:78` (subprocess path arg)

**Interfaces:**
- `init_db.py` is a `__main__`-guarded bootstrap — no importers. It must remain runnable as `python scripts/init_db.py` with only `DB_PATH`/`XRPL_NETWORK` in the env.

- [ ] **Step 1: Move the file**
```bash
git mv init_db.py scripts/init_db.py
```

- [ ] **Step 2: Add the repo-root sys.path bootstrap**

Replace the top of `scripts/init_db.py` (lines 1–8, the imports + the "Deliberately NOT lfg_core.config" comment + `logging.basicConfig`) with:
```python
import logging
import os
import sqlite3
import sys

# Deliberately NOT lfg_core.config: this standalone initializer must run with
# only DB_PATH / XRPL_NETWORK set, without the bot's runtime secrets. db_path is
# dependency-free, so this stays true. Bootstrap the repo root onto sys.path so
# `python scripts/init_db.py` resolves lfg_core (matches every other scripts/ tool).
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core.db_path import app_db_path  # noqa: E402

logging.basicConfig(level=logging.INFO)
```
(Leave the rest of the file — `def init_db(): …` and the `if __name__ == "__main__": init_db()` guard — unchanged.)

- [ ] **Step 3: Update the subprocess path in the bootstrap test**

`tests/test_app_db_path.py:78` — change the script path arg:
```python
        [sys.executable, os.path.join(repo_root, "scripts", "init_db.py")],
```

- [ ] **Step 4: Run the bootstrap test — expect PASS**
```bash
.venv/bin/pytest tests/test_app_db_path.py::test_init_db_runs_without_runtime_secrets -q
```
Expected: pass (creates `LFG` + `burned_nfts` tables in a scrubbed env).

- [ ] **Step 5: Sanity-run it standalone without PYTHONPATH (proves the bootstrap works)**
```bash
cd /home/hamsa/LFG && env -i PATH="$PATH" DB_PATH=/tmp/_initdb_check.db XRPL_NETWORK=testnet .venv/bin/python scripts/init_db.py && rm -f /tmp/_initdb_check.db && echo "standalone OK"
```
Expected: `Database initialized successfully` then `standalone OK`.

- [ ] **Step 6: Commit**
```bash
git add scripts/init_db.py tests/test_app_db_path.py
git commit -m "refactor: move init_db bootstrap into scripts/ (repo-root sys.path shim)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Move `rarity_admin.py` → `scripts/rarity_admin.py`

**Files:**
- Move: `rarity_admin.py` → `scripts/rarity_admin.py`
- Modify: `scripts/rarity_admin.py:18` (sys.path) + its usage docstring, `scripts/rebuild_collection_db/README.md:55`

**Interfaces:**
- Standalone CLI, zero importers. Must run as `python scripts/rarity_admin.py <subcmd>`.

- [ ] **Step 1: Move the file**
```bash
git mv rarity_admin.py scripts/rarity_admin.py
```

- [ ] **Step 2: Fix the sys.path bootstrap**

`scripts/rarity_admin.py:18` — repo root is now two levels up:
```python
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

- [ ] **Step 3: Update the usage examples in its own header comment**

In the header docstring block (currently `#   python rarity_admin.py seed …` etc.), change each `python rarity_admin.py` to `python scripts/rarity_admin.py`.

- [ ] **Step 4: Update the rebuild-collection README invocation**

`scripts/rebuild_collection_db/README.md:55` (invoked from `scripts/rebuild_collection_db/`, so repo-root sibling is now one `..` closer):
```
python ../rarity_admin.py --network mainnet refresh
```

- [ ] **Step 5: Verify the CLI loads**
```bash
cd /home/hamsa/LFG && .venv/bin/python scripts/rarity_admin.py --help >/dev/null && echo "rarity_admin CLI OK"
```
Expected: `rarity_admin CLI OK` (argparse help renders → imports resolved).

- [ ] **Step 6: Commit**
```bash
git add scripts/rarity_admin.py scripts/rebuild_collection_db/README.md
git commit -m "refactor: move rarity_admin CLI into scripts/ (fix repo-root sys.path)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Repoint `pyproject.toml` mypy overrides

**Files:**
- Modify: `pyproject.toml` (relaxed-annotation override module list; remove the ops-tool override block)

- [ ] **Step 1: Repoint the relaxed-annotation override**

In the `[[tool.mypy.overrides]]` block whose `module = [...]` contains `"db_helpers", "user_db"`, replace those two entries with their new dotted paths:
```toml
module = ["main", "run_telegram", "lfg_service.app", "webapp.server", "lfg_core.db_helpers", "lfg_core.user_db", "surfaces.discord_bot.*", "surfaces.telegram_bot.*", "surfaces._shared.*"]
```

- [ ] **Step 2: Remove the now-dead ops-tool override block**

Delete the entire block (comment + override) for `["rarity_admin", "init_db"]` — both now live under `scripts/`, which the top-level `exclude = [..., "^scripts/"]` already skips:
```toml
# One-off standalone ops tools (run manually, never imported by the bot or
# webapp). Fully ignored — typing them is pure cost. Re-promote if they re-enter
# the runtime path. (scripts/ is already skipped via the `exclude` list above;
# ts_helpers, the old trait-swap helper, was retired to legacy/.)
[[tool.mypy.overrides]]
module = ["rarity_admin", "init_db"]
ignore_errors = true
```

- [ ] **Step 3: Run mypy — expect clean**
```bash
.venv/bin/mypy .
```
Expected: `Success: no issues found` (same as before the move). If `lfg_core.db_helpers`/`lfg_core.user_db` surface new strict errors, they were previously relaxed — the repointed override restores that; do NOT add annotations beyond what the override covered.

- [ ] **Step 4: Commit**
```bash
git add pyproject.toml
git commit -m "chore(mypy): repoint overrides to moved modules; drop dead ops-tool block

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Update live docs

**Files:**
- Modify: `CLAUDE.md` (Directory Structure + Key Modules), `docs/runbooks/mainnet-mvp-launch.md` (path ref)

- [ ] **Step 1: CLAUDE.md — Directory Structure**

Remove the root-helpers line:
```
├── db_helpers.py, user_db.py, init_db.py, rarity_admin.py   # root-level helpers / ops tools
```
Fold `db_helpers`, `user_db` into the `lfg_core/` description line and `init_db.py`, `rarity_admin.py` into the `scripts/` description line (append to each existing enumerated list).

- [ ] **Step 2: CLAUDE.md — Key Modules pointers**

Change the two pointer lines' paths:
```
- `lfg_core/db_helpers.py` — LFG-table helpers (`get_next_nft_number`, `record_nft_mint`, `get_nft_data`)
- `lfg_core/user_db.py` — Users-table helpers (`create_users_table`, `register_user`, ...)
```

- [ ] **Step 3: Runbook path reference**

`docs/runbooks/mainnet-mvp-launch.md` — change the `db_helpers.py:7` reference to `lfg_core/db_helpers.py` (line number rots; drop it).

- [ ] **Step 4: Verify no other live-doc references remain**
```bash
grep -rnE '\b(db_helpers|user_db|init_db|rarity_admin)\.py\b' --include='*.md' . \
  | grep -vE '/\.venv/|/\.claude/worktrees/|docs/superpowers/'
```
Expected: empty (historical `docs/superpowers/` intentionally excluded/frozen).

- [ ] **Step 5: Commit**
```bash
git add CLAUDE.md docs/runbooks/mainnet-mvp-launch.md
git commit -m "docs: point references at relocated helper modules

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Full verification gate

**Files:** none (verification only)

- [ ] **Step 1: Zero-straggler grep — expect empty**
```bash
grep -rnE '^[[:space:]]*(import (db_helpers|user_db|init_db|rarity_admin)($|[[:space:]#])|from (db_helpers|user_db|init_db|rarity_admin) import )' --include='*.py' . | grep -vE '/\.venv/|/\.claude/worktrees/'
```
Expected: no output.

- [ ] **Step 2: Confirm root no longer holds the four helpers**
```bash
ls db_helpers.py user_db.py init_db.py rarity_admin.py 2>&1 | grep -c 'No such file'
```
Expected: `4`.

- [ ] **Step 3: Full lint/format/type/test gate**
```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy . && .venv/bin/pytest -q
```
Expected: ruff clean, format clean, mypy `Success`, pytest all green (~1495 passed). If `ruff format` wants changes, run `.venv/bin/ruff format .`, re-review the diff, and fold into the nearest commit.

- [ ] **Step 4: If any formatting was applied, commit it**
```bash
git add -A && git commit -m "style: ruff-format after module relocation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" || echo "nothing to format-commit"
```

---

### Task 8: Housekeeping — archive dead untracked files

**Files:**
- Modify: `.gitignore` (add `.archive/`)
- Move (untracked, no git trace): the dead files into `.archive/`

- [ ] **Step 1: Add `.archive/` to `.gitignore`**

Append under the macOS section (or a new "# Local archive of dead root clutter" comment):
```
# Local archive of dead root clutter (backups, logs, mockups, source CSVs)
.archive/
```

- [ ] **Step 2: Create the dir and move the dead files**
```bash
cd /home/hamsa/LFG && mkdir -p .archive
mv lfg_nfts.db.bak-20260711-pre-testnet-purge \
   lfg_nfts.db.bak-pre-realtraits-20260613-001004 \
   webapp.log LFGOdata.csv users.json \
   lfg-app-redesign.html lfgo-brand-kit.html \
   .archive/ 2>&1 | tee /dev/stderr | grep -q 'No such file' && echo "NOTE: one or more already moved" || true
ls .archive/
```
Expected: the seven files listed under `.archive/`. (Any already-absent file is fine — idempotent.)

- [ ] **Step 3: Confirm root is clean of them + git sees only the .gitignore change**
```bash
git status --porcelain
```
Expected: only ` M .gitignore` (the moved files were untracked → invisible to git; `.archive/` now ignored).

- [ ] **Step 4: Commit the .gitignore line**
```bash
git add .gitignore
git commit -m "chore(gitignore): ignore local .archive/ for dead root clutter

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Housekeeping — prune stale worktrees

**Files:** none (git worktree admin, no tracked change)

- [ ] **Step 1: List worktrees and their dirty state**
```bash
git worktree list
for w in .claude/worktrees/marketplace .claude/worktrees/wf_4dd0895f-d21-1 \
         .claude/worktrees/wf_4dd0895f-d21-2 .claude/worktrees/wf_4dd0895f-d21-3 \
         .claude/worktrees/wf_4dd0895f-d21-4; do
  echo "--- $w ---"; git -C "$w" status --porcelain 2>/dev/null | head
done
```
Expected: each prints its dirty files (usually empty = clean).

- [ ] **Step 2: Remove each `.claude/worktrees/*` worktree**

For a CLEAN worktree: `git worktree remove <path>`. For one with only throwaway build artifacts (`__pycache__`, `.pyc`) confirmed non-essential: `git worktree remove --force <path>`. **Never** touch `/tmp/.../lfg-fix` (a concurrent session).
```bash
for w in .claude/worktrees/marketplace .claude/worktrees/wf_4dd0895f-d21-1 \
         .claude/worktrees/wf_4dd0895f-d21-2 .claude/worktrees/wf_4dd0895f-d21-3 \
         .claude/worktrees/wf_4dd0895f-d21-4; do
  git worktree remove "$w" 2>&1 || echo "SKIP $w (dirty — inspect manually before --force)"
done
git worktree prune
git worktree list
```
Expected: final list shows only `/home/hamsa/LFG [chore/root-cleanup]` and the `/tmp/.../lfg-fix` worktree.

- [ ] **Step 3: No commit** — worktree admin leaves no tracked change.

---

### Task 10: Push and open the draft PR

**Files:** none

- [ ] **Step 1: Push the branch (triggers the pre-push gate)**
```bash
git push -u origin chore/root-cleanup
```
Expected: pre-push hook runs ruff/ruff-format/mypy/gitleaks/pytest and passes; branch pushed.

- [ ] **Step 2: Open the draft PR**
```bash
gh pr create --draft --repo Team-Hamsa/LFG \
  --title "chore: relocate loose root helpers into packages + tidy root" \
  --body "$(cat <<'EOF'
## What
Moves the four loose root Python helpers into their proper packages and fixes every reference — no behavior change.

- `db_helpers.py` → `lfg_core/db_helpers.py`
- `user_db.py` → `lfg_core/user_db.py`
- `init_db.py` → `scripts/init_db.py` (repo-root sys.path shim; it's a `__main__` bootstrap, zero importers)
- `rarity_admin.py` → `scripts/rarity_admin.py` (fix repo-root sys.path)

Plus: repointed `pyproject.toml` mypy overrides, updated live docs (`CLAUDE.md`, mainnet runbook, rebuild README), and `.gitignore`d a local `.archive/` for dead untracked clutter (backups/logs/mockups/CSV — not in this diff). Stale `.claude/worktrees/` pruned locally.

## Why
The root mixed loose helper modules in with the standard project files — looked unprofessional and buried the real entrypoints. `lfg_core/` already holds every flat store module; `scripts/` is the home for run-not-imported ops tools.

## Verification
- Zero-straggler grep for the old bare imports: empty.
- `ruff` / `ruff format --check` / `mypy` / `pytest` (~1495 tests): green.
- Live runtime DBs untouched.

## Deploy note
Merging trips the drain-aware post-merge hook (restarts `lfg-activity`). `lfg-bot` / `lfg-telegram` / index listeners need a manual `pm2 restart lfg-bot lfg-telegram lfg-index-testnet lfg-index-mainnet` to load the new import paths.

Spec: `docs/superpowers/specs/2026-07-12-root-loose-file-cleanup-design.md`
Plan: `docs/superpowers/plans/2026-07-12-root-loose-file-cleanup.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
Expected: draft PR URL printed. Then wait for Greptile + CodeRabbit, triage findings, mark ready, merge only when both are clean.

---

## Self-Review

**Spec coverage:** every spec section maps to a task — moves (T1–T4), sys.path gotchas (T3 §2, T4 §2), pyproject overrides (T5), live docs incl. rebuild README (T4 §4, T6), housekeeping archive (T8), worktree prune (T9), draft-PR delivery + deploy note (T10). ✓
**Placeholder scan:** no TBD/TODO; every code step shows exact content. ✓
**Type/name consistency:** public APIs restated in T1/T2 Interfaces match the source (`get_next_nft_number`/`record_nft_mint`/`get_nft_data`; `DATABASE`/`create_users_table`/`register_user`/`get_user`/`get_all_registered_users`). ✓
**Straggler regex safety:** anchored at line start so `from lfg_core.market_store import init_db as …` (the function alias) is NOT matched — only true bare-module imports. ✓
