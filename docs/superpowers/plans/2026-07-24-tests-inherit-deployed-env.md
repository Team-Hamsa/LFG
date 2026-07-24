# Tests inherit the deployed .env — root fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the pytest suite from inheriting the deployed `.env`, so config constants
are exercised against shipped defaults on every machine (killing the #312/#323 bug class),
and write the convention down so it doesn't recur.

**Architecture:** Two independent seams.
1. **Gate the load** — `lfg_core/config.py` skips `load_dotenv()` when `LFG_SKIP_DOTENV` is
   truthy; the root `conftest.py` sets `LFG_SKIP_DOTENV=1` plus all `_require(...)`-mandatory
   vars and layer knobs *before* config is first imported.
2. **Convention** — a `CLAUDE.md` subsection documenting "assert `*_DEFAULT`, pin in
   conftest not a preamble."

**Tech Stack:** Python 3 / pytest / python-dotenv; no client changes.

## Global Constraints

- **SourceTag = 2606160021 + provenance memos** must remain on every XRPL tx. This change
  builds no tx; it must not perturb the existing SourceTag/memos invariant tests
  (`tests/test_discord_sourcetag_invariant.py`, `tests/test_memos*.py`) — they must stay
  green.
- **Pre-push gate** (ruff --fix, ruff-format, mypy from `.venv`, gitleaks, pytest,
  validate-trait-config) must pass. Never `--no-verify`. In a worktree the gate needs the
  `.venv` symlink (`ln -sfn <main>/.venv .venv`) or it silently skips (see #315).
- **No app.js / client change** in this plan — no cache-buster bump needed.
- Every conftest env assignment uses `os.environ.setdefault(...)` so explicit shell exports
  still win.

---

### Task 1: Gate `load_dotenv()` on `LFG_SKIP_DOTENV`

**Files:**
- Modify: `lfg_core/config.py`
- Create: `tests/test_config_dotenv_gate.py`

**Interfaces:**
- Produces: config module honors `LFG_SKIP_DOTENV` (truthy ⇒ `load_dotenv()` not called).
- Consumes: `os.getenv("LFG_SKIP_DOTENV")`.

- [ ] **Step 1: Write the failing test(s).** `tests/test_config_dotenv_gate.py`, env-guard
      preamble at module top:
      ```python
      import os
      os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
      os.environ.setdefault("LAYER_SOURCE", "local")

      import subprocess  # noqa: E402
      import sys  # noqa: E402
      import textwrap  # noqa: E402
      import pytest  # noqa: E402

      _MANDATORY = {
          "XUMM_API_KEY": "test", "XUMM_API_SECRET": "test",
          "SEED": "sEdTM1uX8pu2do5XvTnutH6HsouMaM2",
          "TOKEN_ISSUER_ADDRESS": "rrrrrrrrrrrrrrrrrrrrrhoLvTp",
          "TOKEN_CURRENCY_HEX": "4C46474F00000000000000000000000000000000",
          "BUNNY_CDN_ACCESS_KEY": "test", "BUNNY_CDN_STORAGE_ZONE": "test",
          "LAYER_SOURCE": "local", "BUNNY_PULL_ZONE": "nft.pullzone.example",
          "XRPL_NETWORK": "testnet",
      }

      @pytest.mark.parametrize("skip,expected", [("1", "False"), ("0", "True")])
      def test_dotenv_gate_respects_skip(tmp_path, skip, expected):
          (tmp_path / ".env").write_text("BULK_MINT_UI_ENABLED=1\n")
          env = {**os.environ, **_MANDATORY, "LFG_SKIP_DOTENV": skip}
          repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
          out = subprocess.run(
              [sys.executable, "-c", textwrap.dedent(
                  "from lfg_core import config; print(config.BULK_MINT_UI_ENABLED)")],
              cwd=tmp_path, env={**env, "PYTHONPATH": repo_root},
              capture_output=True, text=True, check=True)
          assert out.stdout.strip() == expected, out.stderr
      ```
      Subprocess is mandatory — config freezes at import, so an in-process re-import won't
      re-read the planted `.env`. `cwd=tmp_path` makes python-dotenv's upward walk find the
      planted file.
- [ ] **Step 2: Run to verify they fail.** `.venv/bin/python -m pytest
      tests/test_config_dotenv_gate.py -q` — the `skip="1"` case fails today (config always
      loads the `.env`, prints `True`, expected `False`).
- [ ] **Step 3: Implement.** In `lfg_core/config.py`, replace the bare `load_dotenv()` at
      line 11 with the gate:
      ```python
      # load_dotenv() walks UP from CWD, so a checkout/worktree under the deployment
      # tree inherits the LIVE .env. The pytest suite opts out (LFG_SKIP_DOTENV=1 in
      # the root conftest.py) so config exercises shipped defaults, never the box's .env.
      if os.getenv("LFG_SKIP_DOTENV", "0") in ("0", "false", "False"):
          load_dotenv()
      ```
- [ ] **Step 4: Run to verify they pass.** `.venv/bin/python -m pytest
      tests/test_config_dotenv_gate.py -q` — both parametrizations green.
- [ ] **Step 5: Wider suite / regression run.** `.venv/bin/python -m pytest
      tests/test_config_economy_validate.py tests/test_discord_config.py -q` — config-facing
      tests unaffected (conftest not yet setting the skip; the runtime default is still
      "load", so nothing changes for the existing suite yet).
- [ ] **Step 6: Commit.** `feat(config): gate load_dotenv on LFG_SKIP_DOTENV so tests can
      opt out of the deployed .env (#323)`

---

### Task 2: Set `LFG_SKIP_DOTENV` + mandatory vars centrally in `conftest.py`

**Files:**
- Modify: `conftest.py`
- Create: `tests/test_env_isolation.py`

**Interfaces:**
- Produces: whole suite runs with `.env` skipped; `_require(...)` vars supplied by conftest.
- Consumes: the Task-1 gate.

- [ ] **Step 1: Write the failing test(s).** `tests/test_env_isolation.py` with **no**
      env-guard preamble beyond importing config (deliberately — it proves conftest alone
      suffices):
      ```python
      from lfg_core import config

      def test_suite_skips_dotenv():
          import os
          assert os.getenv("LFG_SKIP_DOTENV") not in (None, "0", "false", "False")

      def test_shipped_defaults_not_env_masked():
          # Reads the FROZEN constant on purpose: the pre-#312 probe. Passes only
          # because conftest pinned the default and the .env was never loaded.
          assert config.BULK_MINT_UI_ENABLED is False
      ```
- [ ] **Step 2: Run to verify they fail.** With a hostile `.env` planted at the repo root
      (`printf 'BULK_MINT_UI_ENABLED=1\n' >> .env` — or verify the deploy box's real one is
      present), `.venv/bin/python -m pytest tests/test_env_isolation.py -q` fails
      `test_suite_skips_dotenv` (conftest doesn't set the var yet) and, on a box whose `.env`
      sets the flag, `test_shipped_defaults_not_env_masked`.
- [ ] **Step 3: Implement.** At the **very top** of `conftest.py`, above the existing
      `os.environ.setdefault("ECONOMY_ENABLED", ...)` block (order matters — must precede any
      import that pulls in `lfg_core.config`), add:
      ```python
      # --- Isolate the suite from the deployed .env (#323) ---
      os.environ.setdefault("LFG_SKIP_DOTENV", "1")
      os.environ.setdefault("XUMM_API_KEY", "test")
      os.environ.setdefault("XUMM_API_SECRET", "test")
      os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway testnet seed
      os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
      os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
      os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
      os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
      os.environ.setdefault("LAYER_SOURCE", "local")
      os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
      ```
      Keep the existing `ECONOMY_ENABLED` / `XRPL_NETWORK` / `ECONOMY_NETWORK` / `XUMM_*` /
      `SHOP_*` pins below (they still matter and setdefault-compose fine). Update the file's
      header comment to state the `.env` is now skipped, not merely overridden.
- [ ] **Step 4: Run to verify they pass.** `.venv/bin/python -m pytest
      tests/test_env_isolation.py -q` green, with the hostile `.env` still planted.
- [ ] **Step 5: Wider suite / regression run — THE AUDIT.** This is the "needs an audit
      before doing B" gate from the issue. Run the full suite twice and diff:
      ```bash
      # (a) hostile .env present at repo root
      printf 'BULK_MINT_UI_ENABLED=1\nXRPL_NETWORK=mainnet\nECONOMY_ENABLED=0\nSHOP_MAX_BRIX=99999\n' > .env.hostile
      cp .env .env.bak 2>/dev/null; cp .env.hostile .env
      .venv/bin/python -m pytest -q | tee /tmp/run_hostile.txt
      # (b) no .env at all
      rm .env
      .venv/bin/python -m pytest -q | tee /tmp/run_noenv.txt
      # restore
      mv .env.bak .env 2>/dev/null; rm -f .env.hostile
      diff <(grep -E 'passed|failed' /tmp/run_hostile.txt) <(grep -E 'passed|failed' /tmp/run_noenv.txt)
      ```
      Both runs MUST report identical pass counts and all-green. Any module that diverges is
      silently depending on a real `.env` value — fix it to assert a `config.*_DEFAULT`
      constant (via `config.env_flag`) or monkeypatch the value, then re-run until the two
      runs match. Record the audit result in the PR description.
- [ ] **Step 6: Commit.** `test(conftest): skip the deployed .env suite-wide and supply
      mandatory vars centrally (#323)`

---

### Task 3: Write down the convention in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` (repo root)

**Interfaces:** docs only.

- [ ] **Step 1: (no test — docs).** N/A; verification is the pre-push gate staying green.
- [ ] **Step 2: N/A.**
- [ ] **Step 3: Implement.** Add a short subsection near the existing "Test env-guard
      convention" material stating:
      - The suite runs with `LFG_SKIP_DOTENV=1` set in the root `conftest.py`; the deployed
        `.env` never reaches a test. Mandatory `_require(...)` vars + layer knobs are supplied
        centrally by `conftest.py`, so **new test files no longer need the copy-pasted
        env-guard preamble** (existing ones are harmless no-ops).
      - To assert a **shipped default**, never read the frozen `config.X` constant — assert
        `config.env_flag("X", config.X_DEFAULT)` or the raw `X_DEFAULT` (#312 pattern), or
        monkeypatch the value under test. Reading the constant tests whatever the ambient env
        froze at import.
      - A per-module preamble pin does **not** fix an import-frozen default (pytest imports
        all modules at collection, alphabetically, before running); only `conftest.py` runs
        early enough. If a new env default needs a suite-wide value, pin it in `conftest.py`.
- [ ] **Step 4: Verify.** `git diff CLAUDE.md` reads correctly; no code affected.
- [ ] **Step 5: N/A.**
- [ ] **Step 6: Commit.** `docs(CLAUDE): document the test .env-isolation convention and
      *_DEFAULT assertion rule (#323)`

---

### Final Task: Full gate + PR

- [ ] Run the full pre-push gate locally: `.venv/bin/python -m pytest` (all 218 modules),
      `ruff check .`, `ruff format --check .`, `.venv/bin/python -m mypy .`. All green.
      (In a worktree, ensure the `.venv` symlink exists first — see #315 — or the gate
      silently skips mypy/pytest.)
- [ ] Confirm `main.py` runtime is unaffected: `LFG_SKIP_DOTENV` is unset outside pytest, so
      `python -c "import main"` still loads the real `.env`.
- [ ] Push the branch and `gh pr create` against `Team-Hamsa/LFG`, base `main`, **non-draft**.
      No AI attribution in the commits or PR body (no `Co-Authored-By`, no "Generated with"
      footer). Reference #323; cross-reference the sibling #315 and the parent #312.
- [ ] In the PR body, paste the Task-2 Step-5 audit result (identical pass counts under
      hostile-.env vs no-.env).
- [ ] Wait for **Greptile** and **CodeRabbit**. Resolve every actionable finding: fix in
      code AND reply on the finding's thread naming the fixing commit (or why declined). A PR
      isn't clean until both bots' findings are triaged. Merge only after review passes.
