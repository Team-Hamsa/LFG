# SDD Progress — SourceTag metrics badge
Plan: docs/superpowers/plans/2026-07-22-sourcetag-metrics.md
Spec: docs/superpowers/specs/2026-07-22-sourcetag-metrics-design.md
Branch: feat/sourcetag-metrics (off origin/main e45cbd6)
Worktree: /home/hamsa/lfg-worktrees/sourcetag-metrics

Pre-flight adjudication (user): duplication of brand palette -> EXTRACT shared
scripts/_brand.py. Plan amended: new Task 3, old 3/4/5 renumbered to 4/5/6.
Controller also fixed a module-import trap (renderers must run as `python -m
scripts.X`, workflow step updated in Task 3).

## Tasks (6)
1. Collector — compute        [x]
2. Collector — publish        [x]
3. Shared brand module        [x]
4. Renderer                   [x]
5. CI + README wiring         [x]
6. Docs (CLAUDE.md)           [x]

## Minor findings (for final review triage)

## Log
Task 1: complete (commits 4d0a439..642e7c4, review clean — Spec OK, Quality Approved). 5 passed; mainnet check 16 wallets / 1943 txs.
  Minor (triage): module docstring forward-references --push and render_sourcetag_svg.py (both land in Tasks 2/4 — self-resolving).
Task 2: complete. validate_payload/is_unchanged/push_to_github + --out/--json/--push added; 15 passed
  (10 new). Deviation: the brief's validate_payload treated all ALLOWED_KEYS fields as
  required, which conflicts with the brief's own minimal push_to_github test payloads
  (e.g. {"total_tagged_txs": 99, "as_of": "now"}) — adjusted so unexpected keys are still
  rejected unconditionally, but per-key shape checks only fire when that key is present;
  also loosened as_of to a plain str check (the given tests pass a non-ISO "now" placeholder).
  Seed snapshot generated (mainnet): 1949 tagged txs / 17 unique wallets -> metrics/sourcetag.json.
Task 2: complete (commits 642e7c4..704353b, re-review clean — Spec OK, Quality Approved). 17 passed.
  ADJUDICATED: implementer first WEAKENED validate_payload to satisfy the plan's stub fixtures; controller rejected and dispatched a fix restoring strict semantics + rewriting fixtures (_valid_payload helper). Reviewer confirmed the weakening was undone, not relocated.
  USER DECISION: schema whitelist added because --push bypasses the local pre-push gate on a public repo.
  NOTE: live metric moved 16 -> 17 unique wallets (new signer rfC5iLU... at 2026-07-22 06:50); verified legitimate, not a regression.
Task 3: complete (commits 704353b..e07c0d7 incl. fix, re-review clean). 11 passed; dashboard.svg byte-identical (controller-verified independently).
  Fixes: stat_tiles([]) ZeroDivisionError guard; substring no-dep test -> AST test_module_imports_only_stdlib; 4 edge tests.
  Minor (triage): AST test silently skips relative imports (from . import x) - out of scope, harmless.
  NOTE: assets/dashboard.svg drifts on every render (live git-derived counts). Do NOT commit it from feature branches; CI owns it.
Task 4: complete (commit 350a1c0), review PENDING at time of writing. 8 passed.
  ADJUDICATED: implementer raised the card-overflow test bound 2+320 -> 8+320 because _brand.sticker_card's drop shadow is intentionally offset to y=8. Verified correct: shadow bottom 328 <= canvas 330; test still catches real overflow. Not a weakening.
Task 4: re-review clean after TWO fix rounds (6c3e129 zero-peak/int-coercion/overflow/malformed-json; 09b13ed +N-more fold no longer out-scales real bars). 16 passed.
  Minor (triage): no all-zero-counts+fold combined test; "+1 more" reads awkward.
Task 5: complete (commit d321e51, review Approved). Self-trigger loop verified closed; paths-ignore semantics confirmed (skips only if ALL files match).
  IMPORTANT for final triage: a missing/malformed metrics/sourcetag.json fails the whole update-loc job, blocking the UNRELATED LoC + dashboard badge commits. Spec says fail loudly; collateral damage on sibling badges is the open question. -> USER DECISION.
Task 6: complete (commits f9d7801 + 225a1bc fix, review Approved after adding the pre-push-bypass rationale + unregistered marker).
  Also fixed: unused sqlite3 import (ruff), ruff-format on tests/test_brand_module.py (8c0b8f4) - both would have failed the pre-push gate.
ALL 6 TASKS COMPLETE. Full gate green: 2416 passed/1 skipped, mypy 331 files clean, ruff+format clean.
