# Root loose-file cleanup — design

**Date:** 2026-07-12
**Status:** Approved (brainstorming) → planning
**Origin:** Direct user request ("the repo has a ton of loose files in the root, which
looks super unprofessional… likely going to require a bit of a refactor to point
everything that references those files to their new location"). Not issue-linked.

## Problem

The repo root mixes standard project files (README, LICENSE, pyproject, …) with
loose Python helper modules and a pile of untracked runtime clutter. On GitHub the
eyesore is four loose `.py` helpers sitting next to the docs; locally the root `ls`
is dominated by gitignored DBs, backups, logs, a CSV, and two HTML mockups.

## Scope (decided with the user)

- **Move the loose tracked Python helpers into packages** and fix every reference
  (the part visible on GitHub).
- **Tidy the untracked local clutter**: archive clearly-dead files into a gitignored
  dir; **leave the live runtime DBs at root** (they never appear on GitHub and are
  held open by production pm2 processes — relocating them is a risky ops change for
  a cosmetic, local-only gain).
- **Prune the 6 stale git worktrees** left under `.claude/worktrees/` by earlier
  Workflow runs. Leave the `/tmp/.../lfg-fix` worktree (a concurrent session).

Out of scope: reorganizing conventional root config (`requirements*.txt`, `setup.sh`,
`trait_config.yaml`), moving live DBs, deleting (vs archiving) data, deleting branches.

## What stays at root (deliberately)

Standard/community docs (`README.md`, `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`,
`CLAUDE.md`), tool config (`.gitignore`, `.coderabbit.yaml`, `.pre-commit-config.yaml`,
`pyproject.toml`, `requirements*.txt`, `setup.sh`), `conftest.py` (pytest root
env-guard — must be at root), and the two pm2 entrypoint shims `main.py` /
`run_telegram.py` (documented to stay for a stable pm2 entrypoint).

## Target — Approach A (flat into existing packages, clean break)

`lfg_core/` already holds every flat store module (`market_store`, `nft_index`,
`history_store`, `layer_store`, `db_path`); `scripts/` is the home for standalone
ops CLIs and is already an importable package (`scripts/__init__.py`). So:

| From (root) | To | Real importers (excl. stale worktrees) |
|---|---|---|
| `db_helpers.py` | `lfg_core/db_helpers.py` | `lfg_core/mint_flow.py`, `tests/test_app_db_path.py`, `tests/test_rarity.py` |
| `user_db.py` | `lfg_core/user_db.py` | `surfaces/discord_bot/bot.py`, `surfaces/discord_bot/views.py`, `lfg_service/identity.py`, `lfg_service/app.py`, `webapp/test_smoke.py`, `tests/test_event_endpoints.py`, `tests/test_app_db_path.py` |
| `init_db.py` | `lfg_core/init_db.py` | `tests/test_market_trait_flow.py`, `tests/test_leaderboard_api.py`, `tests/test_market_api.py` |
| `rarity_admin.py` | `scripts/rarity_admin.py` | none (standalone CLI) |

Moves use `git mv` (history preserved). No back-compat shims: nothing outside the
repo imports these by bare name — pm2 runs `main.py`/`run_telegram.py`, which stay.

Public API preserved unchanged:
- `db_helpers`: `get_next_nft_number`, `record_nft_mint`, `get_nft_data`
- `user_db`: `DATABASE` (= `config.DB_PATH`), `create_users_table`, `register_user`,
  `get_user`, `get_all_registered_users`
- `init_db`: `init_db()`

### Import rewrites (clean break)

- `import db_helpers` → `from lfg_core import db_helpers`
- `from db_helpers import X` → `from lfg_core.db_helpers import X`
- `import user_db` → `from lfg_core import user_db`
- `from user_db import DATABASE` / `get_user` / … → `from lfg_core.user_db import …`
- `import init_db` / `from init_db import init_db` → `from lfg_core import init_db` /
  `from lfg_core.init_db import init_db`

Every `.py` under the repo (excluding `.venv/`, `.claude/worktrees/`) is swept; the
acceptance gate is **zero** remaining bare-module references.

### Two gotchas

1. **`rarity_admin.py` sys.path bootstrap.** It does
   `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` to reach repo
   root. Under `scripts/`, `__file__`'s dir is `scripts/`, so it must become
   `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` (two levels up) so
   `from lfg_core import config, rarity` still resolves when run as
   `python scripts/rarity_admin.py` (or `python ../rarity_admin.py` from the
   rebuild-collection subdir).
2. **`init_db.py` mypy posture.** It is currently `ignore_errors = true`. Moving it
   into the type-checked `lfg_core/` means it must satisfy the gate. It is ~50 lines
   of trivial sqlite DDL — add the `-> None` annotation and drop its override rather
   than carry an `ignore_errors` smell into a package module.

### `pyproject.toml` mypy overrides

- Relaxed-annotation override (line ~63): `"db_helpers", "user_db"` →
  `"lfg_core.db_helpers", "lfg_core.user_db"`.
- Fully-ignored ops-tool override (line ~74): drop `"init_db"` (now annotated in
  `lfg_core/`) and `"rarity_admin"` (now under `scripts/`, already covered by the
  `scripts/` mypy `exclude`). If annotating `init_db` proves noisy, fall back to a
  single relaxed override `module = ["lfg_core.init_db"]` — preferred outcome is a
  clean annotation.

### Doc updates (live docs only)

Historical `docs/superpowers/specs/*` and `docs/superpowers/plans/*` are point-in-time
records and stay frozen. Update only:
- `CLAUDE.md` — "Directory Structure" section (the `db_helpers.py, user_db.py,
  init_db.py, rarity_admin.py # root-level helpers / ops tools` line) and "Key
  Modules" pointers referencing `db_helpers.py` / `user_db.py`.
- `docs/runbooks/mainnet-mvp-launch.md:~217` — `db_helpers.py:7` path ref →
  `lfg_core/db_helpers.py`.
- `scripts/rebuild_collection_db/README.md:~55` — `python ../../rarity_admin.py …`
  → `python ../rarity_admin.py …`.
- `README.md` — verify (grep shows no refs) and update only if any appear.

## Housekeeping

- New gitignored `.archive/` dir; add `.archive/` to `.gitignore`. Move the dead,
  untracked files into it (plain `mv` — they're untracked, so no git trace):
  `lfg_nfts.db.bak-20260711-pre-testnet-purge`,
  `lfg_nfts.db.bak-pre-realtraits-20260613-001004`, `webapp.log`, `LFGOdata.csv`,
  `users.json`, `lfg-app-redesign.html`, `lfgo-brand-kit.html`.
- Live DBs (`lfg_nfts.db`, `history_*.db*`, `onchain_*.db*`, `rarity.db`) — untouched.
- Prune the 6 `.claude/worktrees/*` worktrees: verify each has no uncommitted work
  (skip/`--force` only if confirmed orphaned), `git worktree remove` each, then
  `git worktree prune`. Do **not** touch the `/tmp/.../lfg-fix` worktree. Branch
  deletion is out of scope.

## Delivery, verification, deploy

- **One draft PR** on `chore/root-cleanup`. Touches app source → Greptile + CodeRabbit
  must pass before merge (not a direct-to-main). Committed changes: the `git mv`s +
  import rewrites, `pyproject.toml`, the `.gitignore` `.archive/` line, and the doc
  edits (plus this spec + the plan). The file archiving and worktree pruning are
  untracked local ops with no git trace.
- **Verification gate before pushing:** grep-sweep proving zero stragglers of the old
  bare imports (outside `.venv/`/`.claude/worktrees/`/historical specs), then the full
  pre-push equivalent — `ruff check`, `ruff format --check`, `mypy`, `pytest`
  (~1495 tests) — all green.
- **Deploy note:** merging trips the new drain-aware post-merge hook, which restarts
  `lfg-activity` after draining. `lfg-bot` / `lfg-telegram` / the index listeners are
  **not** auto-restarted by that hook, so they need a manual
  `pm2 restart lfg-bot lfg-telegram lfg-index-testnet lfg-index-mainnet` to load the
  new import paths. Flagged for the user to run; not automated.

## Risks

- **Missed reference → import error at runtime.** Mitigated by the zero-straggler
  grep gate + full pytest before push; the surface is small (~11 files).
- **Live-tree edit race.** This repo is the live deployment; running processes keep
  their already-loaded modules until restart, so an in-place file move is inert until
  a restart. The move + all import updates land as one coherent commit, so any restart
  after merge loads a consistent tree.
- **Concurrent session.** A second Claude session is active on
  `fix/backfill-slowdown-retry` (its own `/tmp` worktree). This work stays on
  `chore/root-cleanup` and does not touch that worktree or its branch.
