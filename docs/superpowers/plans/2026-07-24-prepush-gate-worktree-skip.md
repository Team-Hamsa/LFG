# Pre-push Gate Worktree venv Resolution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the blocking pre-push gate (mypy / pytest / validate-trait-config)
actually RUN inside a git worktree — by resolving the single shared project
`.venv` via `git rev-parse --git-common-dir` — and HARD-FAIL loudly with a
"run ./setup.sh" message when the venv genuinely can't be found, so a worktree
can never silently push unverified code (issue #315).

**Architecture:** One independent seam.
- A shared shim `scripts/venv-python` resolves `<git-common-dir>/../.venv/bin/python`
  and `exec`s it (or hard-fails). Works identically in main checkout and worktree.
- `.pre-commit-config.yaml`'s three `.venv/bin/python` hook entries call the shim.
- `setup.sh` gains a clarifying comment only. CI needs no change (verified).

**Tech Stack:** Bash shim; Python 3 / pytest for the tests; pre-commit YAML.

## Global Constraints

- **No transaction is built in this change**, so SourceTag=2606160021 and
  provenance memos are N/A here — but the general rule still holds for any tx
  code the repo contains: never omit them.
- **The pre-push gate (ruff / ruff-format / mypy / gitleaks / pytest /
  validate-trait-config) must pass; never bypass with `--no-verify`.** This
  change is *about* that gate — after implementing, the gate must run and pass
  in both the main checkout and a worktree.
- **No `app.js` change here**, so no `webapp/client/index.html` cache-buster bump
  is required. (Rule noted for completeness.)
- New/changed test files that import `lfg_core` at module top must carry the
  tests/ env-guard preamble (`os.environ.setdefault("BUNNY_PULL_ZONE", ...)` /
  `LAYER_SOURCE`). The shim test below does NOT import `lfg_core`, so it needs
  the preamble only if that changes.

---

### Task 1: Shared `scripts/venv-python` shim + hook rewiring (TDD)

**Files:**
- Create: `scripts/venv-python` (bash, executable)
- Create: `tests/test_venv_python_shim.py`
- Modify: `.pre-commit-config.yaml` (three `entry:` lines)
- Modify: `setup.sh` (comment only)

**Interfaces:**
- Produces: an executable `scripts/venv-python` that takes the same args as
  `python` (e.g. `-m pytest`, `-m mypy .`, `scripts/validate_trait_config.py`)
  and either `exec`s the shared venv interpreter or exits non-zero with an
  actionable stderr message.
- Consumes: `git rev-parse --git-common-dir` (present wherever a git hook runs).

- [ ] **Step 1: Write the failing test(s)** — `tests/test_venv_python_shim.py`,
  subprocess-driven (no `lfg_core` import, no env preamble needed):

  ```python
  import os
  import stat
  import subprocess
  import sys
  from pathlib import Path

  REPO_ROOT = Path(__file__).resolve().parent.parent
  SHIM = REPO_ROOT / "scripts" / "venv-python"


  def test_shim_is_executable():
      assert SHIM.exists(), "scripts/venv-python must exist"
      assert os.access(SHIM, os.X_OK), "scripts/venv-python must be executable"


  def test_shim_resolves_shared_venv_in_main_checkout():
      # Run from the real repo root: resolves this checkout's .venv/bin/python.
      out = subprocess.run(
          [str(SHIM), "-c", "import sys; print(sys.executable)"],
          cwd=REPO_ROOT,
          capture_output=True,
          text=True,
      )
      assert out.returncode == 0, out.stderr
      assert out.stdout.strip().endswith(".venv/bin/python"), out.stdout


  def _make_git_repo(path: Path) -> None:
      subprocess.run(["git", "init", "-q", str(path)], check=True)


  def test_shim_hard_fails_loudly_without_venv(tmp_path):
      repo = tmp_path / "norepovenv"
      repo.mkdir()
      _make_git_repo(repo)
      (repo / "scripts").mkdir()
      shim_copy = repo / "scripts" / "venv-python"
      shim_copy.write_text(SHIM.read_text())
      shim_copy.chmod(shim_copy.stat().st_mode | stat.S_IXUSR)

      out = subprocess.run(
          [str(shim_copy), "-c", "print('should not run')"],
          cwd=repo,
          capture_output=True,
          text=True,
      )
      assert out.returncode != 0, "must fail when no .venv exists"
      assert "should not run" not in out.stdout
      assert "setup.sh" in out.stderr
      assert ".venv/bin/python" in out.stderr


  def test_shim_resolves_common_venv_from_worktree(tmp_path):
      # Main repo with a FAKE venv interpreter; add a worktree; shim run inside
      # the worktree must resolve the MAIN repo's fake interpreter.
      main = tmp_path / "main"
      main.mkdir()
      _make_git_repo(main)
      # minimal commit so `git worktree add` has a base
      (main / "README").write_text("x\n")
      subprocess.run(["git", "-C", str(main), "add", "-A"], check=True)
      subprocess.run(
          ["git", "-C", str(main), "-c", "user.email=t@t", "-c", "user.name=t",
           "commit", "-qm", "init"],
          check=True,
      )
      # fake shared venv interpreter that just prints a sentinel
      venv_bin = main / ".venv" / "bin"
      venv_bin.mkdir(parents=True)
      fake = venv_bin / "python"
      fake.write_text("#!/usr/bin/env bash\necho MAIN_VENV_HIT\n")
      fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
      # shim inside main
      (main / "scripts").mkdir()
      shim_copy = main / "scripts" / "venv-python"
      shim_copy.write_text(SHIM.read_text())
      shim_copy.chmod(shim_copy.stat().st_mode | stat.S_IXUSR)

      wt = tmp_path / "wt"
      subprocess.run(
          ["git", "-C", str(main), "worktree", "add", "-q", str(wt)],
          check=True,
      )
      out = subprocess.run(
          [str(wt / "scripts" / "venv-python"), "-c", "ignored"],
          cwd=wt,
          capture_output=True,
          text=True,
      )
      assert out.returncode == 0, out.stderr
      assert "MAIN_VENV_HIT" in out.stdout, (out.stdout, out.stderr)
  ```

- [ ] **Step 2: Run to verify they fail** —
  `.venv/bin/python -m pytest tests/test_venv_python_shim.py -q`
  Expect collection/exec failures (shim file does not exist yet →
  `test_shim_is_executable` fails; subprocess tests error on missing file).

- [ ] **Step 3: Implement** —
  1. Create `scripts/venv-python` with the shim from the spec
     (`#!/usr/bin/env bash`, `set -euo pipefail`, resolve
     `common_dir="$(git rev-parse --git-common-dir)"`,
     `venv_root="$(cd "$common_dir/.." && pwd)"`,
     `py="$venv_root/.venv/bin/python"`, `[[ -x "$py" ]]` else loud error to
     stderr naming `$py` + `(cd "$venv_root" && ./setup.sh)`, `exec "$py" "$@"`).
  2. `chmod +x scripts/venv-python` (commit the exec bit — verify with
     `git ls-files -s scripts/venv-python` shows mode `100755`).
  3. Rewrite the three `.pre-commit-config.yaml` entries:
     `entry: scripts/venv-python -m mypy .`,
     `entry: scripts/venv-python -m pytest`,
     `entry: scripts/venv-python scripts/validate_trait_config.py`.
     Leave `language: system`, `pass_filenames`, `always_run`, `files` as-is.
  4. Add a one-line comment in `setup.sh` near the `pre-commit install` step:
     the gate resolves the interpreter via `scripts/venv-python`, and this one
     main-checkout `.venv` serves every worktree (no per-worktree venv).

- [ ] **Step 4: Run to verify they pass** —
  `.venv/bin/python -m pytest tests/test_venv_python_shim.py -q` → all green.

- [ ] **Step 5: Wider suite / regression run** —
  - `.venv/bin/python -m pytest -q` (full suite still green).
  - `.venv/bin/pre-commit run --all-files --hook-stage pre-push` from the main
    checkout → mypy/pytest/validate-trait-config still run and pass (proves the
    rewired entries work in the normal case).

- [ ] **Step 6: Commit** —
  `fix(gate): resolve pre-push venv via git-common-dir so worktrees run the gate (#315)`

---

### Task 2: Verify the worktree + missing-venv behavior end-to-end (manual)

**Files:** none (verification only; capture evidence in the PR body).

- [ ] **Step 1:** From an actual `git worktree` of this repo that has NO local
  `.venv`, run `.venv/bin/pre-commit run --hook-stage pre-push --all-files`
  using the worktree's checkout (or trigger a real `git push` on a throwaway
  branch). Confirm mypy AND pytest **execute** (visible test counts / mypy
  summary), NOT "Executable `.venv/bin/python` not found".
- [ ] **Step 2:** Temporarily rename the main checkout's `.venv`, re-run the
  hook, and confirm a single actionable message containing `setup.sh` and the
  missing path, with a non-zero exit — then restore `.venv`.
- [ ] **Step 3:** Confirm CI is unaffected: the `.git/../.venv` = `.venv`
  resolution holds in the CI checkout; no `ci.yml` edit needed. (If the CI run
  on the PR shows the gate skipping, THEN and only then adjust — expected: no
  change.)

---

### Final Task: Full gate + PR

- [ ] Run the full gate locally: `.venv/bin/pre-commit run --all-files
  --hook-stage pre-push` (ruff, ruff-format, mypy, gitleaks, pytest,
  validate-trait-config) — all green. Never `--no-verify`.
- [ ] Push the feature branch (this touches application/tooling config →
  a normal reviewed PR, not a direct-to-main trivial change).
- [ ] `gh pr create` **non-draft**, targeting `main`. No AI attribution in the
  commit trailers or PR body (per global rules).
- [ ] Wait for **Greptile** and **CodeRabbit**. On a clean Greptile review the
  verdict lives only in the `Greptile Review` check-run summary — check the run,
  don't assume a no-show means skipped.
- [ ] Close out every actionable bot finding on its own thread (reply naming the
  fixing commit or why declined) before merging.
- [ ] Cross-reference #323 in the PR description as the sibling test-env-isolation
  fix (separate PR).
