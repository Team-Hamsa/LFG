# Design: Pre-push CI Gate + Codebase Cleanup (LFG)

**Date:** 2026-06-16
**Status:** Approved (design); Phase B to get its own implementation plan
**Author:** Josh + Claude

## Problem

LFG currently has **no CI** — the GitHub Actions workflow did not survive the repo
flatten into standalone `~/LFG` (`.github/` is absent from the current tree, though
history shows prior `ci:` commits). Nothing verifies the code locally or remotely.
The codebase (~8,700 LOC Python, excluding `legacy/`/`backup/`) is largely untyped
and unlinted in any enforced way (`.ruff_cache`/`.mypy_cache` exist from ad-hoc runs,
but no config pins them and no gate enforces them).

Goal: catch failures **before push** instead of waiting for them to fail on GitHub,
and get the codebase squeaky-clean (lint + strict types + tests) **before the
hackathon** so feature development during the sprint runs on green.

## Goals

- One source of truth for checks, run identically **locally (pre-push)** and **in CI**.
- Standard check suite: ruff (lint), ruff-format (style), mypy (`--strict`), pytest, gitleaks (secrets).
- Restore GitHub Actions CI (absent today).
- Drive the whole codebase to pass the gate, then enforce it.

## Non-Goals

- Typing/linting `legacy/` and `backup/` (archived/dead code — excluded).
- Writing type stubs for third-party libraries we don't control (use ignore overrides instead).
- Applying this to Baysed-Lab yet (LFG is the pilot; replicate after it lands).

## Decisions (locked)

| Decision | Choice |
|----------|--------|
| Scope | Both halves: local pre-push hook **and** restored GitHub Actions CI |
| Check suite | ruff lint, ruff-format, mypy (strict), pytest, gitleaks |
| mypy strictness | `--strict` with per-module `ignore_missing_imports` overrides for third-party libs |
| Tooling | `pre-commit` framework, hooks staged at **pre-push** (not pre-commit) |
| Rollout | Two phases: stand up gate non-blocking → grind to green → flip to blocking |

## Architecture

Single source of truth: `.pre-commit-config.yaml`. It runs locally at the pre-push
stage and in CI via `pre-commit run --all-files` — same config both places, no drift.

```
LFG/
├── .pre-commit-config.yaml     # canonical check definitions (ruff, ruff-format, mypy, gitleaks, pytest)
├── pyproject.toml              # NEW: [tool.ruff] + [tool.mypy] config (strict + per-module overrides)
├── requirements-dev.txt        # NEW: pinned dev tooling (pre-commit, mypy, types-* stubs)
├── .github/workflows/ci.yml    # NEW: restored CI — runs the pre-commit gate
└── setup.sh                    # UPDATED: add `pre-commit install --hook-type pre-push`
```

### Component responsibilities

- **`.pre-commit-config.yaml`** — the gate. ruff + ruff-format + mypy + gitleaks as
  standard hooks; pytest as a `local` hook. All `stages: [pre-push]`. Commits stay
  fast; the gate runs only on push.
- **`pyproject.toml`** — config home for ruff and mypy.
  - `[tool.ruff]`: sensible standard ruleset.
  - `[tool.mypy]`: `strict = true`; `exclude` for `legacy/`, `backup/`.
  - `[[tool.mypy.overrides]]`: `ignore_missing_imports = true` for untyped libs
    (`discord.*`, `xrpl.*`, `xumm.*`, BunnyCDN client, etc. — finalized from the
    actual import errors at baseline).
- **`requirements-dev.txt`** — dev-only, pinned, so the bot's runtime install stays
  lean and local == CI. (pre-commit, and any `types-*` stub packages mypy requests.)
- **`.github/workflows/ci.yml`** — checkout → setup Python → install `requirements.txt`
  + `requirements-dev.txt` → run `pre-commit run --all-files`. Triggers: PRs to `main`
  and pushes to `main`. **Non-blocking (`continue-on-error`) during Phase A/B;
  blocking after the flip.**

## Rollout

### Phase A — Stand up the gate (non-blocking)

1. Add `pyproject.toml`, `requirements-dev.txt`, `.pre-commit-config.yaml`.
2. Add `.github/workflows/ci.yml` **non-blocking** (reports violations without failing PRs).
3. Do **not** `pre-commit install` the local hook yet (avoid blocking pushes mid-cleanup).
4. Run `pre-commit run --all-files` once to produce the **baseline violation report** —
   the full ruff + mypy inventory. This becomes the Phase-B worklist and sizes it.
5. Verify `.ruff_cache`/`.mypy_cache`/`.pytest_cache` are gitignored.

### Phase B — Grind to green (own implementation plan)

6. **Auto-fix first:** `ruff check --fix` + `ruff format` clear mechanical issues
   (committed separately for a reviewable diff).
7. **mypy by module:** annotate file-by-file until `mypy --strict` passes, in logical
   chunks (e.g. `lfg_core/` → `main.py` → `ts_helpers.py` → `db_helpers.py` → `webapp/`),
   each a digestible commit. `legacy/`/`backup/` excluded.
8. **gitleaks:** confirm clean scan (no secrets tracked — already verified).
9. **pytest:** ensure existing suite passes under the gate; add minimal tests only
   where a type fix changed behavior.

Phase B volume (mypy error count) is unknown until step 4 runs, so it gets its own
implementation plan sized by the baseline report.

### The flip (enforcement on)

10. When `pre-commit run --all-files` is fully green:
    - Remove `continue-on-error` from CI (now blocking on PRs).
    - Add `pre-commit install --hook-type pre-push` to `setup.sh`; run it locally.
    - Update global `~/.claude/CLAUDE.md`: run `pre-commit run --all-files` (or rely on
      the pre-push hook) before pushing LFG code.

## Error Handling & Edge Cases

- **Escape hatch:** `git push --no-verify` for genuine emergencies (CI still catches it).
- **Hook speed:** pytest at pre-push only (commits stay instant). If the suite grows
  slow, scope pre-push to fast tests and leave the full suite to CI.
- **Third-party stub gaps:** add the library to the mypy override block rather than
  scattering `# type: ignore`.
- **Cache hygiene:** confirm tool caches are gitignored during Phase A.

## Testing

- Phase A: `pre-commit run --all-files` executes all hooks; CI workflow runs green
  (non-blocking) on a test PR.
- Flip: a deliberately-broken change is blocked by both the local pre-push hook and CI.
- Existing pytest suites (`tests/test_rarity.py`, `webapp/test_smoke.py`) pass under the gate.

## Follow-on

- Replicate the gate to Baysed-Lab after the LFG pilot proves out (tracks alongside
  the existing Baysed CodeRabbit config work).
