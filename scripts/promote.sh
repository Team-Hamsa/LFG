#!/usr/bin/env bash
# promote.sh — promote staging (main) to prod: fast-forward the deploy
# branch to main. The prod deployer (lfg-deployer) picks the move up within
# ~60s and drain-restarts the prod stack. (#223)
#
# Usage: scripts/promote.sh [--yes]
set -euo pipefail

REMOTE="${PROMOTE_REMOTE:-origin}"
YES=0
if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [--yes]" >&2
  exit 2
fi
case "${1:-}" in
  "") ;;
  --yes) YES=1 ;;
  *)
    echo "Usage: $0 [--yes]" >&2
    exit 2
    ;;
esac

git fetch "$REMOTE" --prune

MAIN="$(git rev-parse "$REMOTE/main")"
DEPLOY="$(git rev-parse "$REMOTE/deploy" 2>/dev/null || true)"

if [ -z "$DEPLOY" ]; then
  echo "ERROR: $REMOTE/deploy does not exist. Create it once with:" >&2
  echo "  git push $REMOTE main:deploy" >&2
  exit 1
fi

if [ "$MAIN" = "$DEPLOY" ]; then
  echo "deploy is already up to date with main ($MAIN). Nothing to promote."
  exit 0
fi

if ! git merge-base --is-ancestor "$DEPLOY" "$MAIN"; then
  echo "ERROR: $REMOTE/deploy is NOT an ancestor of $REMOTE/main — the push" >&2
  echo "would not be a fast-forward. Someone force-pushed or committed to" >&2
  echo "deploy directly. Resolve manually before promoting." >&2
  exit 1
fi

echo "Promoting the following commits to prod (deploy):"
echo
git log --oneline "$DEPLOY..$MAIN"
echo

if [ "$YES" -ne 1 ]; then
  printf "Fast-forward %s/deploy to %s/main? [y/N] " "$REMOTE" "$REMOTE"
  read -r answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# Push the MAIN sha captured above (not a possibly-newer origin/main) — you
# promote what you reviewed in the range printed above, not whatever landed
# on main in the interim. --force-with-lease guards against deploy having
# moved (force-push or direct commit) between the snapshot and this push;
# it is NOT a --force — the push still fails unless it is a fast-forward
# (or origin/deploy still matches DEPLOY, when a lease is honored).
git push --force-with-lease="refs/heads/deploy:$DEPLOY" "$REMOTE" "$MAIN:refs/heads/deploy"
echo "Promoted. lfg-deployer will deploy prod within ~60s (watch: pm2 logs lfg-deployer)."
