<!-- Thanks for contributing to LFG! Keep this short — delete sections that don't apply. -->

## What & why

<!-- One or two sentences: what does this change and why? Link the issue it closes. -->

Closes #

## How it was tested

<!-- Commands run, surfaces exercised (Discord bot / Telegram / Activity), testnet vs mainnet. -->

## Checklist

- [ ] The pre-push gate passes locally (`ruff`, `ruff-format`, `mypy`, `gitleaks`, `pytest`, `validate-trait-config`) — never bypassed with `--no-verify`.
- [ ] Any new XRPL transaction or Xaman payload sets `SourceTag = 2606160021` (hackathon requirement).
- [ ] New/changed behavior has tests.
- [ ] Docs updated if this changes setup, env vars, or a user-facing flow.
