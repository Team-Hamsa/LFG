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
1. Collector — compute        [ ]
2. Collector — publish        [ ]
3. Shared brand module        [ ]
4. Renderer                   [ ]
5. CI + README wiring         [ ]
6. Docs (CLAUDE.md)           [ ]

## Minor findings (for final review triage)

## Log
