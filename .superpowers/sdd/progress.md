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
3. Shared brand module        [ ]
4. Renderer                   [ ]
5. CI + README wiring         [ ]
6. Docs (CLAUDE.md)           [ ]

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
