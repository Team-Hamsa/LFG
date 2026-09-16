#!/usr/bin/env bash
# promote.sh — promote staging (main) to prod (deploy), all of it or a chosen
# subset of merges. The prod deployer (lfg-deployer) picks the move up within
# ~60s and drain-restarts the prod stack. (#223)
#
# Usage: scripts/promote.sh [--yes]                   # everything on main
#        scripts/promote.sh --list                    # what's pending
#        scripts/promote.sh --pick 508 509 [--yes]    # only these PRs/SHAs
#
# Logic lives in promote.py (stdlib only — no venv needed); see its docstring.
set -euo pipefail
exec python3 "$(dirname "$0")/promote.py" "$@"
