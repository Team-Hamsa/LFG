# Pre-push gate worktree venv resolution — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #315

## Problem

The blocking pre-push gate (`.pre-commit-config.yaml`, `default_stages: [pre-push]`)
invokes the project virtualenv by a **relative** path in its three `local`
hooks:

- `mypy` — `entry: .venv/bin/python -m mypy .`
- `pytest` — `entry: .venv/bin/python -m pytest`
- `validate-trait-config` — `entry: .venv/bin/python scripts/validate_trait_config.py`

pre-commit runs each hook with the repo root as cwd, and resolves `.venv/bin/python`
relative to it. In a **git worktree** (`.claude/worktrees/…`, the repo's normal
dev pattern per CLAUDE.md and the concurrent-sessions convention) the repo root
is the *worktree's* directory, which has no `.venv` — `setup.sh` only ever
created `.venv` in the main checkout. The hooks then report:

```
mypy.....................................................................Failed
- hook id: mypy
- exit code: 1
Executable `.venv/bin/python` not found

pytest...................................................................Failed
- exit code: 1
Executable `.venv/bin/python` not found
```

The push is blocked (nothing unverified ships **from that push**), but mypy and
pytest **never ran** — the gate is a false red, not a real failure. That is
strictly worse than either outcome it's mistaken for:

1. It is indistinguishable at a glance from "the gate caught a real bug," so it
   trains the developer to read gate output as noise.
2. The obvious unblock is `git push --no-verify`, which the repo forbids
   (CLAUDE.md pre-push gate is BLOCKING) precisely because it skips the gate
   *for real* — turning a cosmetic annoyance into genuinely unverified code
   shipping.

The current field workaround (memory: "Batched Build save", "Cache-buster")
is `ln -sfn /path/to/main/.venv .venv` inside each worktree. That fixes exactly
one worktree and silently leaves every future one with the same hole — the fix
has to be remembered per worktree, so it will be forgotten.

Encountered pushing #313 from `.claude/worktrees/`.

## Constraints discovered

- **Worktrees are the sanctioned workflow**, not an edge case: CLAUDE.md ("do
  day-to-day dev in worktrees/feature branches") and the concurrent-sessions
  memory both direct dev into `.claude/worktrees/`. The gate must work there by
  default, with no per-worktree manual step.
- **One interpreter should serve every worktree.** All worktrees of one repo
  share the same `requirements.txt`/`requirements-dev.txt` at a given commit, so
  the main checkout's `.venv` (already built by `setup.sh`) is a correct
  interpreter for every worktree. Duplicating a `.venv` per worktree wastes disk
  and drifts.
- **`git rev-parse --git-common-dir` is the canonical seam.** From a worktree it
  returns the *main* repo's git dir (absolute, e.g. `/home/hamsa/LFG/.git`);
  from a normal checkout it returns the relative `.git`. In BOTH cases
  `<common-dir>/../.venv/bin/python` resolves to the one shared venv — verified:
  worktree → `/home/hamsa/LFG/.venv/bin/python`; main checkout → `.venv/bin/python`.
- **Never silently skip.** The failure mode the issue is about is a check that
  *appears* to run but doesn't. Any replacement must either RUN the real checks
  or HARD-FAIL with an actionable message ("run ./setup.sh"), never no-op green.
- **CI must stay green unchanged.** `.github/workflows/ci.yml` builds `.venv` at
  the checkout root then runs `pre-commit run --all-files --hook-stage pre-push`.
  The chosen resolution must produce `.venv/bin/python` there too (it does:
  `.git/../.venv` = `.venv`).
- **Sibling to #323 (test env isolation).** #315 is "the *gate* doesn't run in a
  worktree"; #323 is "when tests DO run they inherit the deployed `.env` up the
  tree." Both are worktree-rooted footguns in the same gate; they are
  independent fixes (this one touches hook interpreter resolution, #323 touches
  `conftest.py` / dotenv loading) and should cross-reference but not merge.
- No transaction is built here — SourceTag / provenance-memo constraints do not
  apply to this change.

## Design

Introduce **one shared interpreter-resolution shim**, `scripts/venv-python`, and
point all three `.venv/bin/python` hook entries at it. The shim resolves the
shared venv via the git common dir and hard-fails loudly when it genuinely
cannot find it.

### `scripts/venv-python` (new, executable, tracked)

```bash
#!/usr/bin/env bash
# Resolve the ONE shared project venv interpreter, whether we're in the main
# checkout or a git worktree, and exec it with the given args. Used by the
# pre-push hooks in .pre-commit-config.yaml so the gate actually runs in a
# worktree instead of silently failing with ".venv/bin/python not found".
# See issue #315.
set -euo pipefail

# --git-common-dir points at the MAIN repo's .git even from a worktree.
# In a normal checkout it is the relative ".git"; in a worktree it is the
# main checkout's absolute .git. In both, <common>/../.venv is the one venv.
common_dir="$(git rev-parse --git-common-dir)"
venv_root="$(cd "$common_dir/.." && pwd)"
py="$venv_root/.venv/bin/python"

if [[ ! -x "$py" ]]; then
  cat >&2 <<EOF
error: project venv interpreter not found at
  $py

The pre-push gate (mypy/pytest/validate-trait-config) runs from the project
.venv. Create it in the MAIN checkout ($venv_root):

  (cd "$venv_root" && ./setup.sh)

This one venv serves every git worktree — you do NOT need a .venv per worktree.
Do not bypass the gate with --no-verify.
EOF
  exit 1
fi

exec "$py" "$@"
```

Key properties:
- **Runs the real checks in a worktree** — resolves the main `.venv` and
  `exec`s it, so mypy/pytest/validate-trait-config execute against the real
  installed deps exactly as they do in the main checkout.
- **Hard-fails loudly, never silently skips** — if the venv genuinely doesn't
  exist (fresh clone, `setup.sh` not yet run), it prints a single actionable
  message naming the exact path and the `./setup.sh` fix, then exits non-zero.
  This replaces the opaque "Executable `.venv/bin/python` not found" with a
  message that says what to do.
- **`set -euo pipefail` + `exec`** — the shim adds no interpreter overhead
  (it `exec`s, not sub-shells the check) and propagates the check's exit code
  verbatim.

### `.pre-commit-config.yaml` (modify three entries)

```yaml
      - id: mypy
        entry: scripts/venv-python -m mypy .
        language: system
        ...
      - id: pytest
        entry: scripts/venv-python -m pytest
        language: system
        ...
      - id: validate-trait-config
        entry: scripts/venv-python scripts/validate_trait_config.py
        language: system
        ...
```

pre-commit executes `entry` with the repo root as cwd; `scripts/venv-python` is
a tracked, executable, slash-containing relative path, so it resolves against
that cwd in every worktree and in the main checkout. The `language: system`,
`pass_filenames`, `always_run`, and `files` settings are unchanged. The `ruff`
and `gitleaks` hooks are unaffected (they use their own pinned envs, not the
project venv).

### `setup.sh` (touch: comment only)

The `.venv/bin/pre-commit install --hook-type pre-push` step is unchanged. Add a
one-line comment noting the shim + that one main-checkout venv serves all
worktrees, so the next reader doesn't re-add per-worktree venvs.

### CI (`.github/workflows/ci.yml`) — no change required

CI builds `.venv` at the checkout root and runs `pre-commit`; the shim resolves
`.git/../.venv` = `.venv` there. Confirmed by the same rev-parse logic. The
plan includes a CI dry-run confirmation step, not a code change.

## Out of scope

- **#323** (tests inheriting the deployed `.env` via up-tree `load_dotenv()`).
  Separate fix in `conftest.py` / dotenv gating. Cross-referenced only.
- Auto-creating a `.venv` inside a worktree (rejected — duplicates disk, drifts
  deps, and the shared-venv model is strictly better).
- Windows shells (repo is Linux-only per `setup.sh`; the shim is bash).
- Changing which checks run or their args — this is purely interpreter
  resolution.

## Open questions / decisions for maintainer

1. **Shim vs. inline `bash -c`.** The issue offered an inline
   `entry: bash -c '"$(git rev-parse --git-common-dir)/../.venv/bin/python" -m pytest'`
   as an alternative. A shared `scripts/venv-python` is proposed instead because
   (a) it centralizes the "run ./setup.sh" guidance in one place, (b) it avoids
   triplicating brittle quoting across three entries, and (c) it's unit-testable.
   Confirm the shim is preferred.
2. **Should `setup.sh`, when run from inside a worktree, refuse / redirect to the
   main checkout** rather than creating a stray worktree-local `.venv`? Proposed:
   leave `setup.sh` behavior as-is (it's normally run in the main checkout); the
   shim's error message already points users to the main checkout. A guard in
   `setup.sh` is a nice-to-have, not required.
3. **Belt-and-suspenders local venv.** If a worktree *does* have its own `.venv`
   (someone symlinked one, the old workaround), should the shim prefer it over
   the common one? Proposed: NO — always resolve the single common-dir venv for
   determinism; a stale worktree-local symlink is exactly the drift we're
   removing. Confirm.

## Testing

**Unit / integration (`tests/test_venv_python_shim.py`):**
- Happy path: run `scripts/venv-python -c "import sys; print(sys.executable)"`
  via `subprocess.run` from the repo root; assert exit 0 and that the printed
  executable path ends in `.venv/bin/python`.
- Loud-fail path: `git init` a throwaway temp dir with NO `.venv`, copy the shim
  into it, run it there; assert non-zero exit AND that stderr contains
  `setup.sh` and the missing interpreter path (proves it hard-fails with
  guidance, never silently skips / no-ops green).
- Worktree path (integration): from a temp `git worktree add` of the throwaway
  repo *that has* a fake `.venv/bin/python` at its main root, run the shim in the
  worktree; assert it resolves to the main root's fake interpreter (proves
  `--git-common-dir` resolution). Use a trivial executable stub as the fake
  `python` so no real venv is needed.

**Manual smoke:**
- From a real worktree with the main `.venv` present:
  `.venv-less` worktree → `git push` (dry, or trigger `pre-commit run
  --hook-stage pre-push --all-files`) → confirm mypy/pytest actually execute
  (see test counts), not "Executable not found".
- Temporarily rename the main `.venv` → run the hook → confirm the single
  actionable "run ./setup.sh" message and non-zero exit.

**Regression:** full `pre-commit run --all-files --hook-stage pre-push` in the
main checkout stays green; CI job unchanged and green.
