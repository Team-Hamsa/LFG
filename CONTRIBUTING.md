# Contributing to LFG

Thanks for your interest in LFG — an XRPL NFT minting bot and Discord Activity.
Contributions of all kinds are welcome: bug fixes, features, docs, and tests.
This guide covers how to get set up and what the checks expect before your code
can merge.

## Getting set up

```bash
git clone https://github.com/Team-Hamsa/LFG.git
cd LFG
./setup.sh   # builds .venv, installs requirements + requirements-dev, installs the pre-push hook
```

`ffmpeg` must be on your system path (it composites trait layers). See the
[README](README.md) for the full prerequisites and environment-variable list.
Setting up the Discord Activity is documented separately in
[docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md).

## Fork, branch, PR flow

1. **Fork** the repo.
2. Create a branch off `main` (`git checkout -b feature/your-feature`).
3. Make your change, with tests where it makes sense.
4. Commit and push to your fork.
5. Open a **pull request** against `Team-Hamsa/LFG`. Describe what changed and
   why; link any related issue.

Keep PRs focused — one logical change per PR is easier to review and land.

## The pre-push gate (blocking)

`./setup.sh` installs a pre-push hook driven by `.pre-commit-config.yaml`. It
runs at the **pre-push** stage, and CI (`.github/workflows/ci.yml`) runs the
exact same gate — so if it passes locally, it passes in CI. Both block on
failure. The gate runs, in order:

- **ruff** (`--fix`) — lint with autofix
- **ruff-format** — formatting
- **mypy** — type-checking from the project `.venv` (against the real installed
  dep types)
- **gitleaks** — secret scanning
- **pytest** — the test suite
- **validate-trait-config** — validates `trait_config.yaml` against `layers/`

**Never bypass the gate with `--no-verify`.** If a check is wrong or blocking
you unfairly, fix it or raise it in your PR — don't skip it.

## Running tests

```bash
python3 -m pytest
```

Run the whole suite before pushing (that is what the gate and CI do). To iterate
on a single area, pass a path, e.g. `python3 -m pytest webapp/test_smoke.py`.

## XRPL transactions — the hackathon SourceTag

LFG is an entry in the **XRPL Make Waves Hackathon**, where transaction volume
is only credited when every transaction carries the project's assigned source
tag. If you add or change any code path that builds or submits an XRPL
transaction or a Xaman (XUMM) signing payload, it **must** set:

```
SourceTag = 2606160021
```

This applies to every transaction type without exception — `NFTokenMint`,
`NFTokenCreateOffer`, `NFTokenAcceptOffer`, `NFTokenBurn`, `NFTokenModify`,
`Payment`, `TrustSet`, AMM trades, and any XUMM payload `txjson`. In practice
the shared builders (`lfg_core/xumm_ops._create_xumm_payload` and the backend
`lfg_core/xrpl_ops` helpers) stamp it for you, along with the provenance
`Memos` — reuse those paths rather than hand-rolling a transaction, and the tag
comes along automatically.

## Where to find things

- **Project overview, architecture, and env vars** — [README.md](README.md)
- **Discord Activity setup** — [docs/ACTIVITY_SETUP.md](docs/ACTIVITY_SETUP.md)
- **What shipped during the hackathon** — [docs/HACKATHON.md](docs/HACKATHON.md)

## License

LFG is MIT-licensed (see the License section of the [README](README.md)). By
contributing, you agree that your contributions are licensed under the same
terms.
