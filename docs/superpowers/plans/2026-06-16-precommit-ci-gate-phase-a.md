# Pre-push CI Gate — Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a non-blocking pre-commit gate (ruff, ruff-format, mypy --strict, gitleaks, pytest) wired to run identically at the local pre-push stage and in restored GitHub Actions CI, and produce a baseline violation report to size Phase B.

**Architecture:** A single `.pre-commit-config.yaml` is the source of truth. Tool config lives in `pyproject.toml`. CI runs the same config via `pre-commit run --all-files`, non-blocking for now. The local hook is NOT installed yet (would block pushes mid-cleanup). Phase A ends by capturing the baseline report.

**Tech Stack:** pre-commit, ruff, mypy (strict), gitleaks, pytest, GitHub Actions, Python 3.10.

## Global Constraints

- Python version floor: **3.10** (`.venv` is 3.10.12; mypy `python_version = "3.10"`, ruff `target-version = "py310"`).
- Runtime deps stay in `requirements.txt`; **dev-only tooling goes in `requirements-dev.txt`**.
- mypy: `strict = true`, with per-module `ignore_missing_imports` overrides for untyped third-party libs; `legacy/` and `backup/` excluded.
- pre-commit hooks staged **`[pre-push]`** only (commits stay fast).
- CI is **non-blocking (`continue-on-error: true`)** throughout Phase A and B; the flip to blocking happens after Phase B, not here.
- Do **NOT** run `pre-commit install` during Phase A (installing the local hook would block pushes before the code is green).
- Trivial-change commit policy: these files (config/CI/docs) may be committed directly to `main` per repo policy; no PR needed.

---

### Task 1: Tool config in `pyproject.toml`

**Files:**
- Create: `pyproject.toml`

**Interfaces:**
- Produces: `[tool.ruff]`, `[tool.ruff.lint]`, `[tool.mypy]`, and `[[tool.mypy.overrides]]` config consumed by Task 3's pre-commit hooks and Task 5's CI run.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[tool.ruff]
target-version = "py310"
line-length = 100
extend-exclude = ["legacy", "backup", ".venv", "generated"]

[tool.ruff.lint]
# Standard sane defaults: pyflakes(F), pycodestyle(E,W), isort(I),
# pyupgrade(UP), flake8-bugbear(B), comprehensions(C4).
select = ["E", "W", "F", "I", "UP", "B", "C4"]
ignore = ["E501"]  # line-length enforced by formatter, not linter

[tool.mypy]
python_version = "3.10"
strict = true
exclude = ["^legacy/", "^backup/", "^\\.venv/", "^generated/"]
# Keep output actionable during the Phase-B grind:
show_error_codes = true
pretty = true

# Third-party libs without type information — ignore missing imports.
# (Finalize this list from the actual baseline errors in Task 6.)
[[tool.mypy.overrides]]
module = [
    "discord.*",
    "xrpl.*",
    "xumm.*",
    "bunnycdn_storage.*",
    "BunnyCDN.*",
    "cv2.*",
    "ffmpeg.*",
    "qrcode.*",
]
ignore_missing_imports = true
```

- [ ] **Step 2: Verify it parses**

Run: `.venv/bin/python -c "import tomllib; tomllib.load(open('pyproject.toml','rb')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add ruff + mypy(strict) config in pyproject.toml"
```

---

### Task 2: Dev tooling in `requirements-dev.txt`

**Files:**
- Create: `requirements-dev.txt`

**Interfaces:**
- Produces: `requirements-dev.txt` installed by CI (Task 5) and by developers locally.

- [ ] **Step 1: Create `requirements-dev.txt`**

```text
# Dev-only tooling. Install with: pip install -r requirements-dev.txt
# Runtime deps stay in requirements.txt.

# Manages the pre-push hook gate (ruff, ruff-format, mypy, gitleaks, pytest).
pre-commit>=3.7,<5

# Type checker (also pulled by the pre-commit mypy hook; pinned here so it can
# be run directly during the Phase-B grind: `.venv/bin/mypy .`).
mypy>=1.13,<2
```

- [ ] **Step 2: Install dev tooling into the venv**

Run: `.venv/bin/pip install -r requirements-dev.txt`
Expected: pre-commit and mypy install without error.

- [ ] **Step 3: Verify pre-commit is available**

Run: `.venv/bin/pre-commit --version`
Expected: prints a `pre-commit 3.x`/`4.x` version.

- [ ] **Step 4: Commit**

```bash
git add requirements-dev.txt
git commit -m "chore: add requirements-dev.txt (pre-commit, mypy)"
```

---

### Task 3: The gate — `.pre-commit-config.yaml`

**Files:**
- Create: `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: ruff/mypy config from `pyproject.toml` (Task 1).
- Produces: the canonical hook set run locally (pre-push) and by CI (Task 5) via `pre-commit run --all-files`.

- [ ] **Step 1: Create `.pre-commit-config.yaml`**

```yaml
# Single source of truth for the check gate. Runs at pre-push locally and in CI.
# Pin/refresh revs with: pre-commit autoupdate
default_stages: [pre-push]
default_install_hook_types: [pre-push]

repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.6
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.14.1
    hooks:
      - id: mypy
        # mypy reads config from pyproject.toml; run over the repo.
        args: []
        # Stubs/deps mypy needs in its isolated env (extend from Task 6 baseline):
        additional_dependencies: []

  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks

  - repo: local
    hooks:
      - id: pytest
        name: pytest
        entry: .venv/bin/python -m pytest
        language: system
        pass_filenames: false
        always_run: true
```

- [ ] **Step 2: Pin hook revs to latest**

Run: `.venv/bin/pre-commit autoupdate`
Expected: updates `rev:` lines to current releases (network required). If offline, keep the revs above.

- [ ] **Step 3: Validate the config**

Run: `.venv/bin/pre-commit validate-config`
Expected: no output / exit 0 (valid config).

- [ ] **Step 4: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "chore: add pre-commit gate (ruff, mypy, gitleaks, pytest) at pre-push"
```

---

### Task 4: gitignore `.pytest_cache`

**Files:**
- Modify: `.gitignore`

**Interfaces:**
- Produces: `.pytest_cache/` ignored (the other tool caches are already ignored).

- [ ] **Step 1: Confirm it is not already ignored**

Run: `grep -c 'pytest_cache' .gitignore`
Expected: `0`

- [ ] **Step 2: Append the ignore entry**

Add this line to `.gitignore` (under the existing cache entries):

```text
.pytest_cache/
```

- [ ] **Step 3: Verify**

Run: `git check-ignore .pytest_cache/ && echo ignored`
Expected: `ignored`

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore .pytest_cache"
```

---

### Task 5: Restore GitHub Actions CI (non-blocking)

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `requirements.txt`, `requirements-dev.txt` (Task 2), `.pre-commit-config.yaml` (Task 3).
- Produces: a CI workflow that runs the gate on pushes/PRs to `main`, non-blocking.

- [ ] **Step 1: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  gate:
    runs-on: ubuntu-latest
    # NON-BLOCKING during Phase A/B. Remove this line at the Phase-B flip.
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
          cache: pip

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt -r requirements-dev.txt

      - name: Run gate (ruff, mypy, gitleaks, pytest)
        # Override the local pytest hook entry (no .venv in CI) and run all hooks.
        env:
          PRE_COMMIT_PYTEST_ENTRY: "python -m pytest"
        run: pre-commit run --all-files --hook-stage pre-push --show-diff-on-failure
```

- [ ] **Step 2: Make the pytest hook CI-portable**

The local hook entry `.venv/bin/python -m pytest` won't exist in CI. Edit `.pre-commit-config.yaml`'s pytest hook `entry` to use a venv-agnostic invocation:

```yaml
      - id: pytest
        name: pytest
        entry: python -m pytest
        language: system
        pass_filenames: false
        always_run: true
```

Locally, run via `.venv/bin/pre-commit ...` so `python` resolves to the venv, or activate the venv first. (This keeps one entry that works both places.)

- [ ] **Step 3: Re-validate config**

Run: `.venv/bin/pre-commit validate-config`
Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml .pre-commit-config.yaml
git commit -m "ci: restore GitHub Actions gate (non-blocking) running pre-commit"
```

---

### Task 6: Capture the baseline violation report

**Files:**
- Create: `docs/superpowers/plans/precommit-baseline-report.md` (the Phase-B worklist)

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a committed inventory of ruff + mypy violations that sizes the Phase-B plan, plus a finalized mypy third-party override list.

- [ ] **Step 1: Run ruff to inventory lint/format issues**

Run: `.venv/bin/ruff check . > /tmp/ruff-report.txt 2>&1; .venv/bin/ruff format --check . >> /tmp/ruff-report.txt 2>&1; tail -5 /tmp/ruff-report.txt`
Expected: a count of errors / "would reformat" files (non-zero is expected — this is the worklist).

- [ ] **Step 2: Run mypy strict to inventory type errors**

Run: `.venv/bin/mypy . > /tmp/mypy-report.txt 2>&1; tail -3 /tmp/mypy-report.txt`
Expected: `Found N errors in M files` (non-zero expected).

- [ ] **Step 3: Identify any missing third-party modules**

Run: `grep -oE 'Cannot find implementation or library stub for module named "[^"]+"' /tmp/mypy-report.txt | sort -u`
Expected: a list (possibly empty). Add any not already covered to the `[[tool.mypy.overrides]]` module list in `pyproject.toml`, then re-run Step 2 once to confirm they're silenced.

- [ ] **Step 4: Write the baseline report**

Create `docs/superpowers/plans/precommit-baseline-report.md` summarizing:
- ruff error count (by rule, from `/tmp/ruff-report.txt`) and number of files needing format.
- mypy error count and the top files by error count: `grep -oE '^[^:]+\.py' /tmp/mypy-report.txt | sort | uniq -c | sort -rn | head -20`.
- The finalized third-party override list.
- A proposed module-by-module remediation order for Phase B (e.g. `lfg_core/` → `main.py` → `ts_helpers.py` → `db_helpers.py` → `user_db.py` → `webapp/`).

- [ ] **Step 5: Confirm gitleaks and pytest are already green**

Run: `.venv/bin/pre-commit run gitleaks --all-files --hook-stage pre-push` and `.venv/bin/pre-commit run pytest --all-files --hook-stage pre-push`
Expected: both PASS (no tracked secrets; existing suite passes). If pytest fails, note it in the report as Phase-B work.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml docs/superpowers/plans/precommit-baseline-report.md
git commit -m "docs: capture pre-commit baseline violation report (Phase-B worklist)"
```

---

## Phase A Done — Definition of Done

- `pyproject.toml`, `requirements-dev.txt`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml` exist and are committed.
- `.venv/bin/pre-commit validate-config` passes.
- CI workflow runs (non-blocking) on push to `main`.
- Baseline report committed; mypy third-party overrides finalized.
- Local pre-push hook intentionally **NOT** installed yet.
- gitleaks clean; pytest status recorded.

**Next:** Phase B (grind to green) gets its own plan, sized by the baseline report. The flip to blocking CI + `pre-commit install` happens at the end of Phase B.

## Self-Review Notes

- **Spec coverage:** Tasks 1–5 implement the gate architecture (pyproject, dev deps, pre-commit config, gitignore, CI). Task 6 produces the baseline report that sizes Phase B. The "flip to blocking" and the Phase-B grind are explicitly out of scope (deferred to Phase B plan), matching the spec's scope boundary.
- **Non-blocking invariant:** `continue-on-error: true` (Task 5) and not installing the local hook (Global Constraints) both upheld.
- **Type/name consistency:** pytest hook `entry` reconciled to `python -m pytest` in Task 5 Step 2 (overriding the `.venv/bin/...` form from Task 3 Step 1) so it works in both local-venv and CI contexts — call out as the single canonical entry.
