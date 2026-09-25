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
- **gitleaks** — secret scanning via `scripts/gitleaks-scan`: the pushed commits
  at pre-push, every tracked file at HEAD in CI. Rules are `.gitleaks.toml`: the
  default set plus XRPL family seeds. Tests that need a seed generate one with
  `xrpl.core.keypairs.generate_seed()`; a literal seed fails the gate.
- **pytest** — the whole suite, via `scripts/run-tests -n auto --dist loadfile`
- **validate-trait-config** — validates `trait_config.yaml` against `layers/`

**Never bypass the gate with `--no-verify`.** If a check is wrong or blocking
you unfairly, fix it or raise it in your PR — don't skip it.

## Running tests

```bash
scripts/run-tests -n auto --dist loadfile    # exactly what the gate runs
scripts/run-tests                            # serial — clearer tracebacks
scripts/run-tests tests/test_market_flow.py  # one file
python3 -m pytest                            # plain pytest still works
```

`scripts/run-tests` chooses `TMPDIR` and nothing else: it forwards every
argument to pytest untouched, so the parallel flags are the caller's to pass
(the pre-push hook passes them for you). Leave them off when you want a serial
run — the tracebacks are far easier to read.

The gate runs the whole suite every time, and that is deliberate: at ~5,800
tests and ~30 s it is cheaper to run everything than to maintain a map of which
tests a change can skip. Two things make that affordable:

- **tmpfs**, which the wrapper picks. The suite's stores are SQLite, so every
  commit `fsync`s. On a box whose checkout shares an ext4 journal with a busy
  writer (the deploy box runs an XRPL validator on the same volume) the suite
  spends its wall clock in `jbd2_log_wait_commit` — 881 s at 20% CPU, versus
  167 s at 83% with `TMPDIR` on a tmpfs. Same tests, same machine. The wrapper
  takes a private `mktemp -d` under `/dev/shm` (never a predictable path —
  `/dev/shm` is world-writable, and the suite executes shims it writes under
  `tmp_path`), proves exec works there, and gets out of the way on any doubt:
  an explicit `TMPDIR` from you always wins, and a failed check just means the
  default and a slower run. A green run leaves that directory empty, because
  pytest keeps `tmp_path` trees only for FAILED tests — so after a red run,
  `ls -dt /dev/shm/lfg-pytest.*` finds the artefacts. They are reaped on the
  next run that is more than six hours later.
- **`-n auto`** (pytest-xdist), with `--dist loadfile` so a module's tests — and
  its module-level state — stay on one worker.

**The suite must stay order-independent.** It is, as of 2026-09-20, and running
it under `-n auto` is what keeps it honest: anything that depends on an earlier
*file* having run shows up immediately. The two historic offenders both live in
the root `conftest.py` now — every import-time-mandatory env var is pinned
there, and an autouse fixture guarantees a current event loop (`asyncio.run()`
ends with `set_event_loop(None)`, which used to poison every later test that
used the `get_event_loop()` idiom). Add new global state to `conftest.py`, not
to whichever test file happened to notice it was missing.

To iterate on a single area, pass a path, e.g.
`scripts/run-tests webapp/test_smoke.py`.

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
