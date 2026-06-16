# Pre-commit Gate — Baseline Violation Report

**Date:** 2026-06-16
**Purpose:** Inventory of violations at the moment the gate was stood up (Phase A),
to size and order the Phase-B "grind to green" effort.
**Tooling:** ruff 0.15.17, mypy 1.20.2 (strict), gitleaks v8.30.0, pytest.

## Summary

| Check | Result | Phase-B work? |
|-------|--------|---------------|
| **ruff lint** | **278 errors** (223 auto-fixable) | Yes — mostly mechanical |
| **ruff format** | **25 files** would reformat (3 already clean) | Yes — one `ruff format` pass |
| **mypy --strict** | **926 errors in 23 files** (28 checked) | Yes — the bulk of the work |
| **gitleaks** | ✅ **Passed** (no tracked secrets) | No |
| **pytest** | ⚠️ **105 passed, 1 failed** | Yes — fix 1 test |

## ruff — by rule

```
163  W293  blank-line-with-whitespace      [auto]
 29  W291  trailing-whitespace             [auto]
 24  I001  unsorted-imports                [auto]
 12  F401  unused-import                   [auto]
  7  C408  unnecessary-collection-call
  6  F841  unused-variable                 [auto]
  5  B007  unused-loop-control-variable
  4  B905  zip-without-explicit-strict
  4  F541  f-string-missing-placeholders   [auto]
  4  F821  undefined-name                  <-- POSSIBLE REAL BUGS, inspect
  4  UP006 non-pep585-annotation           [auto]
  4  W292  missing-newline-at-end-of-file  [auto]
  3  E722  bare-except
  3  UP035 deprecated-import
  2  UP015 redundant-open-modes            [auto]
  2  UP045 non-pep604-annotation-optional  [auto]
  1  B904  raise-without-from-inside-except
  1  C420  unnecessary-dict-comprehension
```

Most clears in one `ruff check --fix` + `ruff format` pass. **Manual attention:**
the 4 `F821 undefined-name` (also surface as mypy `name-defined`) — these are likely
genuine bugs, not style; inspect each before blanket-fixing.

## mypy --strict — by error code

```
362  no-untyped-def      missing function annotations  (mechanical)
282  no-untyped-call     calls into the above          (clears as defs get typed)
 63  type-arg            bare generics (e.g. `dict` -> `dict[str, X]`)
 52  attr-defined        attribute access mypy can't verify
 50  arg-type            wrong argument types          (inspect — possible bugs)
 46  assignment          incompatible assignments      (inspect — possible bugs)
 39  var-annotated       needs a variable annotation
  9  union-attr          attr on a possibly-None value (inspect — possible bugs)
  6  index
  5  import-untyped      add to mypy override list / install types-* stub
  4  name-defined        undefined names               (== ruff F821, REAL BUGS)
  3  return-value / 2 call-arg / 1 return / 1 operator
```

~644 of 926 (no-untyped-def + no-untyped-call) are *annotation churn* — they clear as
functions get typed module-by-module. The smaller buckets (`arg-type`, `assignment`,
`union-attr`, `name-defined`) are where real defects hide — review those, don't just
silence them.

## mypy errors by file (top)

```
310  webapp/test_smoke.py     <-- test file
168  tests/test_rarity.py     <-- test file
117  main.py
 67  lfg_core/xrpl_ops.py
 45  webapp/server.py
 41  lfg_core/xumm_ops.py
 41  lfg_core/swap_flow.py
 37  lfg_core/rarity.py
 36  ts_helpers.py
 26  lfg_core/mint_flow.py
 18  db_helpers.py
 17  rarity_admin.py
 17  lfg_core/layer_store.py
 15  lfg_core/swap_meta.py
 10  scripts/upload_layers_cdn.py
  9  lfg_core/swap_compose.py
  7  lfg_core/traits.py
  +  scripts/rebuild_collection_db/*
```

**478 of 926 errors live in the two test files.** Strict typing of test code is high-cost,
low-value. **Phase-B recommendation:** add a mypy per-module override relaxing strictness
for `tests/` and `webapp/test_smoke.py` (e.g. `disallow_untyped_defs = false`,
`disallow_untyped_calls = false`) — this removes ~478 errors up front and lets the grind
focus on the ~448 errors in real application code.

## Third-party overrides

No `Cannot find implementation or library stub` errors at baseline — the override list in
`pyproject.toml` (`discord.*`, `xrpl.*`, `xumm.*`, `bunnycdn_storage.*`, `cv2.*`,
`ffmpeg.*`, `qrcode.*`) plus installed packages cover all imports. The 5 `import-untyped`
hits should be checked in Phase B and either added to the override list or resolved with a
`types-*` stub package.

## Proposed Phase-B remediation order

1. **Mechanical sweep (1 commit each):** `ruff check --fix .`, then `ruff format .`.
2. **Inspect the real bugs first:** the 4 `F821`/`name-defined`, plus `arg-type`,
   `union-attr`, `assignment` clusters.
3. **Relax test strictness** via mypy overrides (removes ~478 errors).
4. **Type application code module-by-module**, smallest/leaf-first to reduce
   `no-untyped-call` cascades:
   `lfg_core/traits.py` → `swap_compose.py` → `swap_meta.py` → `layer_store.py` →
   `rarity.py` → `xumm_ops.py` → `swap_flow.py` → `mint_flow.py` → `xrpl_ops.py` →
   `db_helpers.py` → `user_db.py` → `ts_helpers.py` → `rarity_admin.py` →
   `webapp/server.py` → `main.py` → `scripts/**`.
5. **Fix the failing test:** `webapp/test_smoke.py::test_img_proxy_accepts_pull_zone_host`
   (currently `400 != 200` on the pull-zone host proxy path).
6. **The flip:** once `pre-commit run --all-files` is green — remove `continue-on-error`
   from CI, add `pre-commit install --hook-type pre-push` to `setup.sh`, update global
   CLAUDE.md.

**Estimated shape:** ~278 ruff (mostly one auto-pass) + ~448 application-code mypy errors
after relaxing tests + 1 test fix. Front-loaded mechanical wins, then steady module typing.
