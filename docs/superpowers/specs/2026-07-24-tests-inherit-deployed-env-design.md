# Tests inherit the deployed .env — kill the config-default bug class at the root — design

**Date:** 2026-07-24
**Status:** draft (triage — needs maintainer review)
**Issue:** #323

## Problem

Three facts about how config is loaded compose into a recurring, machine-only test failure class:

1. `lfg_core/config.py:11` calls a bare `load_dotenv()`. python-dotenv walks **up** the
   directory tree from CWD, so every git worktree under `~/LFG/` (and the deployment
   checkout itself) inherits the deployed `.env` — not one flag but the entire live
   config: `ECONOMY_ENABLED`, `XRPL_NETWORK`, `NFT_FLAGS`, `BULK_MINT_UI_ENABLED`, the
   `SHOP_*` knobs, everything.
2. Config constants are computed at import time and **frozen** (`BULK_MINT_UI_ENABLED =
   env_flag(...)` at `config.py:145`, `SHOP_*`, etc.). Once `lfg_core.config` is imported,
   no later `os.environ` mutation affects them.
3. pytest imports **every** test module at collection, alphabetically, before running a
   single test. So a per-module env pin in one test's preamble routinely runs *after* an
   earlier module already triggered `lfg_core.config`'s import and froze the constants.

Consequence: any test that asserts a shipped default by reading a config constant is one
`.env` line away from failing — on developer/deploy boxes only, never in CI (the runner
has no `.env`). This is exactly what #312 hit on 2026-07-21 when the prod `.env` carried
`BULK_MINT_UI_ENABLED=1`: the pre-push pytest gate failed on unrelated branches until
worked around with `BULK_MINT_UI_ENABLED=0 git push`. Memory records the same class three
times ("Trait Shop currency-pair bug", "Diamond body", "Closet hid None").

### What already partly mitigates it

- The repo-root `conftest.py` is the designated chokepoint: it runs before any test module
  and before config's import, and `os.environ.setdefault(...)` there beats the later
  `load_dotenv()` (which never overrides an already-set var). It currently pins a **manual
  allowlist** of only 8 vars (`ECONOMY_ENABLED`, `XRPL_NETWORK`, `ECONOMY_NETWORK`,
  `XUMM_WS_WATCH`, `XUMM_STATUS_CACHE_SECONDS`, `BULK_MINT_UI_ENABLED`, `SHOP_BASE_BRIX`,
  `SHOP_MIN_BRIX`, `SHOP_MAX_BRIX`, `SHOP_OFFER_TTL_SECONDS`).
- Config exposes named `*_DEFAULT` constants (`config.py:131-144`,
  `BULK_MINT_UI_ENABLED_DEFAULT`, `MARKET_ENABLED_DEFAULT`, `TRAIT_TAXON_DEFAULT`, …) so a
  test can assert the *shipped* default via `config.env_flag(name, X_DEFAULT)` instead of
  the frozen constant — the #312 route.
- ~116 of 218 test modules carry a copy-pasted env-guard preamble that
  `os.environ.setdefault`s the `_require(...)`-mandatory vars (`SEED`,
  `TOKEN_ISSUER_ADDRESS`, `TOKEN_CURRENCY_HEX`, `XUMM_API_KEY/SECRET`, `BUNNY_CDN_*`) plus
  `LAYER_SOURCE` / `BUNNY_PULL_ZONE`.

Neither mitigation is a root fix: the conftest allowlist and the `*_DEFAULT` pattern are
both **manual** — every new env-sensitive default needs someone to remember a line, and
nothing enforces it. The deployed `.env` still reaches the suite by default.

## Constraints discovered

- **Import-time freeze is load-bearing.** The fix must set the environment *before*
  `lfg_core.config` is first imported. Only the root `conftest.py` (loaded by pytest before
  collecting any module) is early enough — a per-module preamble is not.
- **`_require(...)` mandatory vars.** `config.py` calls `_require("XUMM_API_KEY")`,
  `_require("XUMM_API_SECRET")`, `_require("SEED")`, `_require("TOKEN_ISSUER_ADDRESS")`,
  `_require("TOKEN_CURRENCY_HEX")`, `_require("BUNNY_CDN_ACCESS_KEY")`,
  `_require("BUNNY_CDN_STORAGE_ZONE")` at module scope — a missing value raises `ValueError`
  and the import (hence the whole suite) dies. If we stop loading `.env`, conftest MUST
  supply these, or every module that today relies on the real `.env` reaching them breaks.
- **`SEED` must be a valid XRPL family seed.** `_seed_address()` (`config.py:51`) calls
  `Wallet.from_seed(SEED)` when `IS_TESTNET`; the throwaway seed already used in preambles
  (`sEdTM1uX8pu2do5XvTnutH6HsouMaM2`) parses, so reuse it.
- **`XRPL_NETWORK` / `ECONOMY_NETWORK` coherence.** conftest already pins both to `testnet`
  because `validate_economy_config` refuses to import with `ECONOMY_ENABLED=1` and
  `ECONOMY_NETWORK != XRPL_NETWORK`. Skipping `.env` (which is mainnet) makes those pins the
  effective network — the existing pins already assume this, so it holds.
- **`setdefault`, not `[]=`, everywhere.** An explicit shell export must still win so a run
  can force a value (e.g. `XRPL_NETWORK=mainnet pytest -k …`). All conftest pins use
  `setdefault`; the new ones must too.
- **No transactions in scope.** This is a test-harness change — no XRPL tx is built, so
  SourceTag / provenance-memo requirements don't apply here (they remain enforced by the
  existing invariant tests, which this change must not perturb).
- **Non-test runtime must be untouched.** `main.py` and the pm2 processes must keep loading
  the real `.env`. The gate must default to loading; only the test process opts out.

## Design

Two independent seams, matching the issue's Part A / Part B split.

### B — Structural: gate the `.env` load, opt out under pytest

**`lfg_core/config.py`** — replace the bare call:

```python
# load_dotenv() walks UP from CWD, so a checkout/worktree under the deployment
# tree inherits the LIVE .env. The test suite opts out (LFG_SKIP_DOTENV=1 set in
# the root conftest.py) so config exercises shipped defaults, never the box's .env.
if os.getenv("LFG_SKIP_DOTENV", "0") in ("0", "false", "False"):
    load_dotenv()
```

(Reuse the exact falsy denylist `env_flag` uses so semantics match. Keep `load_dotenv`
imported unconditionally.)

**Root `conftest.py`** — set the gate + the mandatory `_require` vars **at the very top,
before the existing pins and before anything imports `lfg_core.config`.** Fold the
copy-pasted preamble into this one chokepoint:

```python
# --- Isolate the suite from the deployed .env (#323) --------------------------
# Skip load_dotenv() entirely under pytest, then supply the _require(...)-mandatory
# vars and layer knobs centrally, so a hostile/live .env on any box can never reach
# a test and config always exercises shipped defaults. setdefault => explicit shell
# exports still win.
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
# (existing pins: ECONOMY_ENABLED / XRPL_NETWORK / ECONOMY_NETWORK / XUMM_* / SHOP_* …)
```

Result: with `.env` no longer loaded, the suite runs against the explicit defaults +
whatever conftest pins, identically on every machine and in CI. The ~116 per-file preambles
become **redundant** (they keep working — `setdefault` is a no-op once conftest set the
value — so they don't need mass deletion; new files simply omit them).

**Audit gate (the reason this is an issue, not a drive-by).** Before B can land, verify no
test silently depends on a *real* `.env` value reaching it. The verification is behavioral,
not by inspection: run the full suite twice —
1. with a deliberately hostile `.env` in the tree (`BULK_MINT_UI_ENABLED=1`,
   `XRPL_NETWORK=mainnet`, `ECONOMY_ENABLED=0`, `SHOP_MAX_BRIX=99999`), and
2. with no `.env` at all —
and require **identical pass results**. Under the fix both runs ignore the file. A module
that today reads a real deployment value will surface here as a diff between the two runs
(or as a failure once the throwaway defaults apply), and gets fixed to use a `*_DEFAULT`
constant or an explicit monkeypatch.

### A — Convention, written down

Add a short subsection to `CLAUDE.md` (next to the existing "Test env-guard convention"
note) stating the two rules the mechanism forces:

- **Asserting a shipped default:** never read the frozen `config.X` constant; assert
  `config.env_flag("X", config.X_DEFAULT)` (or the raw `X_DEFAULT`), or monkeypatch the
  value under test. Reading the constant tests whatever the ambient env froze, not the
  default.
- **Where the pin must live:** the suite runs with `LFG_SKIP_DOTENV=1` set in the root
  `conftest.py`, so the deployed `.env` never reaches a test. A per-module preamble pin does
  **not** work for import-frozen constants (collection order); if a new env default needs a
  suite-wide value, pin it in `conftest.py`, not a preamble.

## Out of scope

- Mass deletion of the ~116 redundant per-file preambles — they're harmless no-ops post-fix;
  removing them is optional cleanup for a separate PR.
- The pre-push venv-path bug (#315) — sibling in the "worktree diverges from `~/LFG`
  locally" family, but an orthogonal fix (`.pre-commit-config.yaml` venv resolution).
- A lint/CI check that forbids asserting frozen config constants — nice-to-have enforcement,
  noted as an open question.
- Changing how `main.py` / pm2 runtime loads `.env` (unchanged: they don't set
  `LFG_SKIP_DOTENV`).

## Open questions / decisions for maintainer

1. **conftest inline pins vs committed `tests/.env.test`.** This design folds the mandatory
   vars into `conftest.py` inline (consistent with the existing chokepoint). The issue also
   floated a committed `tests/.env.test` loaded explicitly (`load_dotenv("tests/.env.test",
   override=True)`). Inline is simpler and keeps one file authoritative; a `.env.test` is
   more grep-able and mirrors real env shape. Pick one.
2. **Enforcement.** Should a follow-up add a lightweight check (ruff custom rule / a
   `test_no_frozen_default_asserts.py` meta-test) that fails when a test asserts a bare
   `config.FLAG` known to have a `*_DEFAULT`? Or is the written convention enough?
3. **Preamble cleanup timing.** Delete the redundant preambles now (bigger diff, clearer
   codebase) or leave them as harmless belt-and-suspenders?
4. **Should `LFG_SKIP_DOTENV` also gate a `.env.test` load**, i.e. skip the real `.env` but
   still load a committed test one? (Only relevant if Q1 chooses `.env.test`.)

## Testing

- **Unit — the gate.** New `tests/test_config_dotenv_gate.py`: parametrized subprocess test.
  Create a `tmp_path` containing a `.env` with `BULK_MINT_UI_ENABLED=1`, run
  `python -c "from lfg_core import config; print(config.BULK_MINT_UI_ENABLED)"` with
  `cwd=tmp_path` and the mandatory vars in the child env. With `LFG_SKIP_DOTENV=1` → prints
  `False` (default, `.env` ignored); without it → prints `True` (`.env` honored). Proves the
  gate both directions. (Subprocess is required: the import freeze means an in-process
  re-import won't re-read.)
- **Unit — conftest supplies mandatory vars.** A test module with **no** env-guard preamble
  that imports `lfg_core.config` and `lfg_service.app` and asserts a shipped default (e.g.
  `config.BULK_MINT_UI_ENABLED is False`) passes — proving the chokepoint alone suffices.
  (`tests/test_config_telegram_miniapp.py` and others already import config with no
  preamble; the new gate test can double as the guard.)
- **Integration — the audit.** Full-suite run twice: (a) with a hostile `.env` planted in
  the repo root, (b) with `.env` moved aside. Require identical pass/fail. Run the original
  pre-#312 probe (assert `config.BULK_MINT_UI_ENABLED is False` reading the constant) — it
  must now pass in both, and in full-suite alphabetical order, without any preamble.
- **Regression.** Full `pytest` (all 218 modules) green under the pre-push gate. `ruff`,
  `ruff-format`, `mypy` clean. The SourceTag / memos invariant tests
  (`test_discord_sourcetag_invariant.py`, `test_memos*.py`) still pass unchanged.
- **Manual smoke.** From a fresh worktree with a mainnet `.env` up the tree, run
  `.venv/bin/python -m pytest tests/test_bulk_mint_ui_flag.py` — passes with no
  `BULK_MINT_UI_ENABLED=0` prefix. Confirm `main.py` still loads the real `.env` (start the
  bot locally / `python -c "import main"` reads env normally — `LFG_SKIP_DOTENV` unset
  outside pytest).
