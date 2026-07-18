# Staging/Prod Stack Split Implementation Plan (#223)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two pm2 stacks on one box — staging (`~/LFG-staging`, `main`, testnet) and prod (`~/LFG`, new `deploy` branch, mainnet) — each auto-deployed by a polling deployer; promotion = fast-forwarding `deploy` to `main`.

**Architecture:** A dependency-free `scripts/deployer.py` (stdlib only, like `lfg_core/db_path.py`) polls `git fetch`, fast-forwards its stack's checkout when the tracked branch moves, pip-installs on requirements changes, and drain-aware-restarts the stack's pm2 processes (generalizing the retiring post-merge hook). Committed pm2 ecosystem files define both stacks. `scripts/promote.sh` wraps the ff push `main → deploy`.

**Tech Stack:** Python 3 stdlib (subprocess, urllib, argparse), pytest, pm2, bash.

**Spec:** `docs/superpowers/specs/2026-07-15-staging-prod-stacks-design.md`

## Global Constraints

- `deployer.py` imports **stdlib only** — no `lfg_core`, no dotenv, no third-party deps (it must run before pip install and never be broken by app code).
- Fast-forward only: the deployer must never rewrite a checkout on diverged history; it halts and logs unless `--force-reset` is passed.
- Prod fail-safe posture is preserved verbatim from the current post-merge hook: on drain timeout **or unreachable health endpoint**, prod REFUSES to restart and logs the manual command. Staging restarts anyway after its short timeout.
- Ports: prod Activity :8176, staging Activity :8177. Branches: staging tracks `main`, prod tracks `deploy`.
- Drain windows: prod 900 s, staging 120 s; poll every 10 s.
- All new test files that import `lfg_core` must carry the env-guard preamble — but the deployer tests must NOT import `lfg_core` at all, so no preamble is needed there.
- Pre-push gate (ruff/mypy/gitleaks/pytest) must pass; never `--no-verify`.

---

### Task 1: Deployer decision core (git seam, ff detection, path/requirements filters)

**Files:**
- Create: `scripts/deployer.py`
- Test: `tests/test_deployer_core.py`

**Interfaces:**
- Produces (used by Tasks 2–3):
  - `run_git(args: list[str], cwd: str) -> str` — check_output wrapper, stripped stdout, raises `subprocess.CalledProcessError`.
  - `@dataclass(frozen=True) StackConfig(name, checkout, branch, health_url, drain_max_wait, drain_poll, refuse_on_drain_failure, restart_processes: tuple[str, ...], pip: str)`
  - `STACKS: dict[str, StackConfig]` with keys `"staging"`, `"prod"`.
  - `fetch(cfg)`, `local_head(cfg) -> str`, `remote_head(cfg) -> str | None`, `is_fast_forward(cfg, old, new) -> bool`, `changed_files(cfg, old, new) -> list[str]`
  - `needs_restart(files: list[str]) -> bool`, `needs_pip(files: list[str]) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deployer_core.py
"""Deployer git/decision core (#223). No lfg_core imports — deployer.py is
stdlib-only by design, so no env-guard preamble here."""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import deployer


def _git(cwd, *args):
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).strip()


def _make_repos(tmp_path):
    """origin bare repo + working clone on branch main, one seed commit."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    (seed / "a.py").write_text("x = 1\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "seed"); _git(seed, "push")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    return origin, seed, clone


def _cfg(clone, branch="main"):
    return deployer.StackConfig(
        name="staging", checkout=str(clone), branch=branch,
        health_url="http://127.0.0.1:1/api/health", drain_max_wait=1,
        drain_poll=0, refuse_on_drain_failure=False,
        restart_processes=("stg-activity",), pip=".venv/bin/pip")


def test_remote_head_moves_after_push(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    deployer.fetch(cfg)
    assert deployer.remote_head(cfg) == deployer.local_head(cfg)
    (seed / "b.py").write_text("y = 2\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "more"); _git(seed, "push")
    deployer.fetch(cfg)
    assert deployer.remote_head(cfg) != deployer.local_head(cfg)


def test_is_fast_forward_true_for_descendant(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    old = deployer.local_head(cfg)
    (seed / "b.py").write_text("y = 2\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "more"); _git(seed, "push")
    deployer.fetch(cfg)
    assert deployer.is_fast_forward(cfg, old, deployer.remote_head(cfg))


def test_is_fast_forward_false_for_diverged(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    # local-only commit in the clone diverges it from a new origin commit
    (clone / "local.py").write_text("z = 3\n")
    _git(clone, "add", "."); _git(clone, "commit", "-m", "local")
    (seed / "b.py").write_text("y = 2\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "remote"); _git(seed, "push")
    deployer.fetch(cfg)
    assert not deployer.is_fast_forward(
        cfg, deployer.local_head(cfg), deployer.remote_head(cfg))


def test_changed_files_lists_the_delta(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    old = deployer.local_head(cfg)
    (seed / "docs").mkdir()
    (seed / "docs" / "n.md").write_text("hi\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "docs"); _git(seed, "push")
    deployer.fetch(cfg)
    assert deployer.changed_files(cfg, old, deployer.remote_head(cfg)) == ["docs/n.md"]


def test_needs_restart_filter():
    assert deployer.needs_restart(["lfg_core/config.py"])
    assert deployer.needs_restart(["webapp/client/app.js"])
    assert deployer.needs_restart(["lfg_service/app.py"])
    assert deployer.needs_restart(["surfaces/discord_bot/bot.py"])
    assert deployer.needs_restart(["scripts/onchain_listener.py"])
    assert deployer.needs_restart(["main.py"])
    assert deployer.needs_restart(["requirements.txt"])
    assert not deployer.needs_restart(["docs/HACKATHON.md", "README.md"])
    assert not deployer.needs_restart([".github/workflows/ci.yml"])
    assert not deployer.needs_restart([])


def test_needs_pip_only_on_requirements():
    assert deployer.needs_pip(["requirements.txt"])
    assert deployer.needs_pip(["requirements-dev.txt"])
    assert not deployer.needs_pip(["lfg_core/config.py"])


def test_stacks_registry_shape():
    assert deployer.STACKS["staging"].branch == "main"
    assert deployer.STACKS["prod"].branch == "deploy"
    assert deployer.STACKS["prod"].refuse_on_drain_failure is True
    assert deployer.STACKS["staging"].refuse_on_drain_failure is False
    assert deployer.STACKS["prod"].drain_max_wait == 900
    assert deployer.STACKS["staging"].drain_max_wait == 120
    assert "8177" in deployer.STACKS["staging"].health_url
    assert "8176" in deployer.STACKS["prod"].health_url
    for cfg in deployer.STACKS.values():
        assert not any(p.endswith("-deployer") for p in cfg.restart_processes)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_deployer_core.py -v`
Expected: FAIL / ERROR — `ModuleNotFoundError` or `AttributeError` (deployer missing).

- [ ] **Step 3: Write the implementation**

```python
# scripts/deployer.py
"""Per-stack polling deployer (#223).

Runs under pm2 as stg-deployer / lfg-deployer. Polls `git fetch`; when the
stack's tracked branch moves and the update is a fast-forward, advances the
checkout, pip-installs on requirements changes, and drain-aware-restarts the
stack's pm2 processes (generalizing the retired post-merge hook).

STDLIB ONLY by design: this must run before `pip install` and must never be
broken by app code. Do not import lfg_core or any third-party package.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass

HOME = "/home/hamsa"

# Mirrors the retired post-merge hook's filter, plus surfaces/ and scripts/
# (bot code and the pm2-run listener/snapshot scripts also need restarts).
_RESTART_RE = re.compile(
    r"^(webapp/|lfg_service/|lfg_core/|surfaces/|scripts/"
    r"|requirements[^/]*\.txt$|[^/]+\.py$)"
)
_PIP_RE = re.compile(r"^requirements[^/]*\.txt$")


@dataclass(frozen=True)
class StackConfig:
    name: str
    checkout: str
    branch: str
    health_url: str
    drain_max_wait: int
    drain_poll: int
    refuse_on_drain_failure: bool
    restart_processes: tuple[str, ...]
    pip: str


STACKS: dict[str, StackConfig] = {
    "staging": StackConfig(
        name="staging",
        checkout=f"{HOME}/LFG-staging",
        branch="main",
        health_url="http://127.0.0.1:8177/api/health",
        drain_max_wait=120,
        drain_poll=10,
        refuse_on_drain_failure=False,
        restart_processes=(
            "stg-bot", "stg-activity", "stg-telegram", "stg-index-testnet"),
        pip=f"{HOME}/LFG-staging/.venv/bin/pip",
    ),
    "prod": StackConfig(
        name="prod",
        checkout=f"{HOME}/LFG",
        branch="deploy",
        health_url="http://127.0.0.1:8176/api/health",
        drain_max_wait=900,
        drain_poll=10,
        refuse_on_drain_failure=True,
        restart_processes=(
            "lfg-bot", "lfg-activity", "lfg-telegram", "lfg-index-mainnet"),
        pip=f"{HOME}/LFG/.venv/bin/pip",
    ),
}
# lfg-snapshot / stg-snapshot are cron-run one-shots: pm2 relaunches them from
# disk each night, so they always pick up the new code without a restart here.
# The deployers themselves are excluded on purpose (never self-restart mid-run).


def log(msg: str) -> None:
    print(f"[deployer] {msg}", flush=True)


def run_git(args: list[str], cwd: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def fetch(cfg: StackConfig) -> None:
    run_git(["fetch", "origin", "--prune"], cfg.checkout)


def local_head(cfg: StackConfig) -> str:
    return run_git(["rev-parse", "HEAD"], cfg.checkout)


def remote_head(cfg: StackConfig) -> str | None:
    try:
        return run_git(["rev-parse", f"origin/{cfg.branch}"], cfg.checkout)
    except subprocess.CalledProcessError:
        return None  # branch doesn't exist on origin (yet)


def is_fast_forward(cfg: StackConfig, old: str, new: str) -> bool:
    try:
        run_git(["merge-base", "--is-ancestor", old, new], cfg.checkout)
        return True
    except subprocess.CalledProcessError:
        return False


def changed_files(cfg: StackConfig, old: str, new: str) -> list[str]:
    out = run_git(["diff", "--name-only", old, new], cfg.checkout)
    return [line for line in out.splitlines() if line]


def needs_restart(files: list[str]) -> bool:
    return any(_RESTART_RE.match(f) for f in files)


def needs_pip(files: list[str]) -> bool:
    return any(_PIP_RE.match(f) for f in files)
```

(The `argparse`/`json`/`time`/`urllib` imports are used by Tasks 2–3; keeping
them now avoids churn — if ruff flags them as unused at this commit, add
`# noqa: F401` temporarily and remove it in Task 3.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_deployer_core.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/deployer.py tests/test_deployer_core.py
git commit -m "feat(deploy): deployer decision core — ff detection, restart/pip filters (#223)"
```

---

### Task 2: Drain state machine + pm2 restart

**Files:**
- Modify: `scripts/deployer.py` (append)
- Test: `tests/test_deployer_drain.py`

**Interfaces:**
- Consumes: `StackConfig`, `log` from Task 1.
- Produces (used by Task 3):
  - `active_sessions(url: str, fetcher=None) -> int | None` — `None` on unreachable/malformed; `fetcher(url) -> bytes` injectable for tests (default urllib, 3 s timeout).
  - `drain(cfg, fetcher=None, sleeper=time.sleep, clock=time.monotonic) -> str` — returns `"drained" | "timeout" | "unreachable"`.
  - `restart_stack(cfg, runner=None) -> bool` — `pm2 restart <name> --update-env` per process via injectable `runner(cmd: list[str]) -> int` (returncode); returns False if any restart failed.
  - `drain_and_restart(cfg, fetcher=None, runner=None, sleeper=time.sleep, clock=time.monotonic) -> str` — returns `"restarted" | "refused" | "restart_failed"`, encoding the staging-vs-prod posture.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deployer_drain.py
"""Drain/restart posture (#223): prod refuses on drain failure, staging
restarts anyway. No lfg_core imports."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import deployer


def _cfg(refuse, max_wait=30):
    return deployer.StackConfig(
        name="t", checkout="/nonexistent", branch="main",
        health_url="http://127.0.0.1:9/api/health", drain_max_wait=max_wait,
        drain_poll=10, refuse_on_drain_failure=refuse,
        restart_processes=("p1", "p2"), pip="pip")


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def sleep(self, n):
        self.t += n


def _fetcher_seq(values):
    """Each call yields the next canned /api/health body (or raises)."""
    it = iter(values)
    def fetch(url):
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    return fetch


def test_active_sessions_parses_count():
    body = b'{"ok": true, "active_sessions": 3, "detail": {}}'
    assert deployer.active_sessions("u", fetcher=lambda u: body) == 3


def test_active_sessions_none_on_unreachable_or_malformed():
    def boom(u):
        raise OSError("down")
    assert deployer.active_sessions("u", fetcher=boom) is None
    assert deployer.active_sessions("u", fetcher=lambda u: b"not json") is None
    assert deployer.active_sessions("u", fetcher=lambda u: b'{"ok": true}') is None


def test_drain_immediate_when_zero():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 0}'])
    assert deployer.drain(_cfg(True), fetcher=f, sleeper=clock.sleep, clock=clock) == "drained"


def test_drain_waits_then_drains():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 2}', b'{"active_sessions": 0}'])
    assert deployer.drain(_cfg(True), fetcher=f, sleeper=clock.sleep, clock=clock) == "drained"
    assert clock.t == 10  # slept one poll interval


def test_drain_timeout():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 1}'] * 100)
    assert deployer.drain(_cfg(True, max_wait=25), fetcher=f,
                          sleeper=clock.sleep, clock=clock) == "timeout"


def test_drain_unreachable_first_probe():
    clock = FakeClock()
    f = _fetcher_seq([OSError("down")])
    assert deployer.drain(_cfg(True), fetcher=f, sleeper=clock.sleep, clock=clock) == "unreachable"


def test_restart_stack_runs_pm2_per_process():
    calls = []
    def runner(cmd):
        calls.append(cmd)
        return 0
    assert deployer.restart_stack(_cfg(True), runner=runner) is True
    assert calls == [["pm2", "restart", "p1", "--update-env"],
                     ["pm2", "restart", "p2", "--update-env"]]


def test_restart_stack_reports_failure():
    assert deployer.restart_stack(_cfg(True), runner=lambda c: 1) is False


def test_prod_refuses_on_timeout_and_unreachable():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 1}'] * 100)
    calls = []
    out = deployer.drain_and_restart(_cfg(True, max_wait=25), fetcher=f,
                                     runner=lambda c: calls.append(c) or 0,
                                     sleeper=clock.sleep, clock=clock)
    assert out == "refused" and calls == []
    out = deployer.drain_and_restart(_cfg(True), fetcher=_fetcher_seq([OSError()]),
                                     runner=lambda c: calls.append(c) or 0,
                                     sleeper=clock.sleep, clock=clock)
    assert out == "refused" and calls == []


def test_staging_restarts_anyway_on_timeout():
    clock = FakeClock()
    f = _fetcher_seq([b'{"active_sessions": 1}'] * 100)
    calls = []
    out = deployer.drain_and_restart(_cfg(False, max_wait=25), fetcher=f,
                                     runner=lambda c: calls.append(c) or 0,
                                     sleeper=clock.sleep, clock=clock)
    assert out == "restarted" and len(calls) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_deployer_drain.py -v`
Expected: FAIL — `AttributeError: module 'scripts.deployer' has no attribute 'active_sessions'`.

- [ ] **Step 3: Append the implementation to `scripts/deployer.py`**

```python
def _default_fetcher(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.read()


def active_sessions(url, fetcher=None):
    # -> int | None ; None means unreachable or malformed (fail-unknown).
    fetcher = fetcher or _default_fetcher
    try:
        body = json.loads(fetcher(url))
        n = body["active_sessions"]
        return n if isinstance(n, int) else None
    except Exception:
        return None


def drain(cfg: StackConfig, fetcher=None, sleeper=time.sleep,
          clock=time.monotonic) -> str:
    deadline = clock() + cfg.drain_max_wait
    first = True
    while True:
        n = active_sessions(cfg.health_url, fetcher=fetcher)
        if n is None:
            # Unreachable (first probe or mid-drain): we cannot confirm the
            # session count — report it and let posture decide.
            log(f"{cfg.name}: /api/health unreachable"
                + (" on first probe" if first else " mid-drain"))
            return "unreachable"
        first = False
        if n == 0:
            return "drained"
        if clock() >= deadline:
            log(f"{cfg.name}: {n} session(s) still in flight after "
                f"{cfg.drain_max_wait}s")
            return "timeout"
        log(f"{cfg.name}: {n} in-flight session(s); waiting for drain…")
        sleeper(cfg.drain_poll)


def _default_runner(cmd: list[str]) -> int:
    return subprocess.call(cmd)


def restart_stack(cfg: StackConfig, runner=None) -> bool:
    runner = runner or _default_runner
    ok = True
    for name in cfg.restart_processes:
        rc = runner(["pm2", "restart", name, "--update-env"])
        if rc != 0:
            log(f"{cfg.name}: WARNING pm2 restart {name} failed (rc={rc})")
            ok = False
    return ok


def drain_and_restart(cfg: StackConfig, fetcher=None, runner=None,
                      sleeper=time.sleep, clock=time.monotonic) -> str:
    outcome = drain(cfg, fetcher=fetcher, sleeper=sleeper, clock=clock)
    if outcome != "drained" and cfg.refuse_on_drain_failure:
        # Prod posture (verbatim from the retired post-merge hook): never
        # cut off in-flight mint/swap/market work; hand it to a human.
        log(f"{cfg.name}: drain outcome={outcome}; REFUSING auto-restart. "
            f"Restart manually when safe: pm2 restart "
            f"{' '.join(cfg.restart_processes)} --update-env")
        return "refused"
    if outcome != "drained":
        log(f"{cfg.name}: drain outcome={outcome}; restarting anyway "
            "(staging posture)")
    return "restarted" if restart_stack(cfg, runner=runner) else "restart_failed"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_deployer_drain.py tests/test_deployer_core.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/deployer.py tests/test_deployer_drain.py
git commit -m "feat(deploy): drain state machine + pm2 restart with prod refuse posture (#223)"
```

---

### Task 3: `run_once` deploy step, `--force-reset`, main loop + CLI

**Files:**
- Modify: `scripts/deployer.py` (append `run_once`, `main`)
- Test: `tests/test_deployer_run_once.py`

**Interfaces:**
- Consumes: everything from Tasks 1–2.
- Produces:
  - `run_once(cfg, force_reset=False, fetcher=None, runner=None, sleeper=time.sleep, clock=time.monotonic) -> str` — one poll cycle; returns one of `"up_to_date" | "advanced_no_restart" | "restarted" | "refused" | "restart_failed" | "halted_not_ff" | "no_remote_branch" | "pip_failed"`. `runner` here is the same injectable used for pm2 AND pip (any non-git subprocess).
  - `main(argv=None)` — CLI: `deployer.py <stack> [--once] [--force-reset] [--interval N]` (default interval 60). `--force-reset` implies a single reset pass (`git reset --hard origin/<branch>`) and is only honored with `--once`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deployer_run_once.py
"""End-to-end run_once against throwaway git repos (#223)."""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import deployer


def _git(cwd, *args):
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).strip()


def _make_repos(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    (seed / "a.py").write_text("x = 1\n")
    _git(seed, "add", "."); _git(seed, "commit", "-m", "seed"); _git(seed, "push")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    return seed, clone


def _cfg(clone, refuse=False):
    return deployer.StackConfig(
        name="t", checkout=str(clone), branch="main",
        health_url="u", drain_max_wait=1, drain_poll=0,
        refuse_on_drain_failure=refuse,
        restart_processes=("p1",), pip="/definitely/missing/pip")


DRAINED = lambda u: b'{"active_sessions": 0}'  # noqa: E731


def _push(seed, relpath, content="x\n", msg="c"):
    p = seed / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(seed, "add", "."); _git(seed, "commit", "-m", msg); _git(seed, "push")


def test_up_to_date(tmp_path):
    seed, clone = _make_repos(tmp_path)
    assert deployer.run_once(_cfg(clone), fetcher=DRAINED,
                             runner=lambda c: 0) == "up_to_date"


def test_code_change_advances_and_restarts(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "lfg_core/new.py")
    calls = []
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED,
                            runner=lambda c: calls.append(c) or 0)
    assert out == "restarted"
    assert (clone / "lfg_core" / "new.py").exists()
    assert ["pm2", "restart", "p1", "--update-env"] in calls


def test_docs_change_advances_without_restart(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "docs/note.md", "hi\n")
    calls = []
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED,
                            runner=lambda c: calls.append(c) or 0)
    assert out == "advanced_no_restart"
    assert (clone / "docs" / "note.md").exists()
    assert calls == []


def test_requirements_change_runs_pip_before_restart(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "requirements.txt", "aiohttp\n")
    calls = []
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED,
                            runner=lambda c: calls.append(c) or 0)
    assert out == "restarted"
    assert calls[0] == ["/definitely/missing/pip", "install", "-r",
                        "requirements.txt"]


def test_pip_failure_blocks_restart(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "requirements.txt", "aiohttp\n")
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED,
                            runner=lambda c: 1 if c[1] == "install" else 0)
    assert out == "pip_failed"


def test_diverged_halts_without_touching_checkout(tmp_path):
    seed, clone = _make_repos(tmp_path)
    (clone / "local.py").write_text("z\n")
    _git(clone, "add", "."); _git(clone, "commit", "-m", "local")
    local = _git(clone, "rev-parse", "HEAD")
    _push(seed, "lfg_core/new.py")
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED, runner=lambda c: 0)
    assert out == "halted_not_ff"
    assert _git(clone, "rev-parse", "HEAD") == local


def test_force_reset_recovers_diverged(tmp_path):
    seed, clone = _make_repos(tmp_path)
    (clone / "local.py").write_text("z\n")
    _git(clone, "add", "."); _git(clone, "commit", "-m", "local")
    _push(seed, "lfg_core/new.py")
    out = deployer.run_once(_cfg(clone), force_reset=True,
                            fetcher=DRAINED, runner=lambda c: 0)
    assert out == "restarted"
    assert _git(clone, "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")


def test_missing_remote_branch(tmp_path):
    seed, clone = _make_repos(tmp_path)
    cfg = deployer.StackConfig(
        name="t", checkout=str(clone), branch="deploy", health_url="u",
        drain_max_wait=1, drain_poll=0, refuse_on_drain_failure=False,
        restart_processes=("p1",), pip="pip")
    assert deployer.run_once(cfg, fetcher=DRAINED,
                             runner=lambda c: 0) == "no_remote_branch"


def test_prod_refusal_leaves_checkout_advanced(tmp_path):
    # The ff-merge happens first; a refused restart still leaves new code on
    # disk (matching the old hook: pull landed, restart deferred to a human).
    seed, clone = _make_repos(tmp_path)
    _push(seed, "lfg_core/new.py")
    out = deployer.run_once(_cfg(clone, refuse=True),
                            fetcher=lambda u: (_ for _ in ()).throw(OSError()),
                            runner=lambda c: 0)
    assert out == "refused"
    assert (clone / "lfg_core" / "new.py").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_deployer_run_once.py -v`
Expected: FAIL — `AttributeError: ... no attribute 'run_once'`.

- [ ] **Step 3: Append the implementation to `scripts/deployer.py`**

```python
def run_once(cfg: StackConfig, force_reset: bool = False, fetcher=None,
             runner=None, sleeper=time.sleep, clock=time.monotonic) -> str:
    runner = runner or _default_runner
    fetch(cfg)
    new = remote_head(cfg)
    if new is None:
        log(f"{cfg.name}: origin/{cfg.branch} does not exist; nothing to do")
        return "no_remote_branch"
    old = local_head(cfg)
    if new == old:
        return "up_to_date"

    if force_reset:
        log(f"{cfg.name}: FORCE RESET {old[:12]} -> {new[:12]}")
        run_git(["reset", "--hard", new], cfg.checkout)
    elif is_fast_forward(cfg, old, new):
        log(f"{cfg.name}: fast-forwarding {old[:12]} -> {new[:12]}")
        run_git(["merge", "--ff-only", new], cfg.checkout)
    else:
        log(f"{cfg.name}: HALTED — origin/{cfg.branch} ({new[:12]}) is not a "
            f"fast-forward of local HEAD ({old[:12]}). A force-push or local "
            f"commit diverged the checkout. Fix manually, or run: "
            f"scripts/deployer.py {cfg.name} --once --force-reset")
        return "halted_not_ff"

    files = changed_files(cfg, old, new)
    if needs_pip(files):
        log(f"{cfg.name}: requirements changed; running pip install")
        for req in ("requirements.txt", "requirements-dev.txt"):
            if req in files:
                if runner([cfg.pip, "install", "-r", req]) != 0:
                    log(f"{cfg.name}: pip install -r {req} FAILED; "
                        "NOT restarting (old code keeps running)")
                    return "pip_failed"
    if not needs_restart(files):
        log(f"{cfg.name}: advanced to {new[:12]}; no restart-worthy changes")
        return "advanced_no_restart"
    return drain_and_restart(cfg, fetcher=fetcher, runner=runner,
                             sleeper=sleeper, clock=clock)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LFG per-stack polling deployer")
    ap.add_argument("stack", choices=sorted(STACKS))
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument("--force-reset", action="store_true",
                    help="reset --hard to origin/<branch>; requires --once")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)
    if args.force_reset and not args.once:
        ap.error("--force-reset requires --once (deliberate one-shot recovery)")
    cfg = STACKS[args.stack]
    if args.once:
        out = run_once(cfg, force_reset=args.force_reset)
        log(f"{cfg.name}: {out}")
        return 0 if out not in ("halted_not_ff", "pip_failed",
                                "restart_failed") else 1
    log(f"{cfg.name}: polling origin/{cfg.branch} every {args.interval}s")
    while True:
        try:
            out = run_once(cfg)
            if out != "up_to_date":
                log(f"{cfg.name}: cycle result: {out}")
        except Exception as exc:  # never die on a transient git/network error
            log(f"{cfg.name}: cycle error: {exc!r}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
```

Remove any temporary `# noqa: F401` left from Task 1 (all imports are used now).

- [ ] **Step 4: Run the full deployer suite + gate linters**

Run: `.venv/bin/pytest tests/test_deployer_core.py tests/test_deployer_drain.py tests/test_deployer_run_once.py -v && .venv/bin/ruff check scripts/deployer.py && .venv/bin/mypy scripts/deployer.py`
Expected: all pass, no lint/type errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/deployer.py tests/test_deployer_run_once.py
git commit -m "feat(deploy): run_once deploy cycle, --force-reset recovery, polling CLI (#223)"
```

---

### Task 4: `scripts/promote.sh`

**Files:**
- Create: `scripts/promote.sh` (mode 755)
- Test: `tests/test_promote_sh.py`

**Interfaces:**
- Produces: `scripts/promote.sh [--yes]` — shows `deploy..main` commit range, confirms, then `git push origin main:deploy`. Refuses if the push would not be a fast-forward. Env override `PROMOTE_REMOTE` (default `origin`) for tests.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_promote_sh.py
"""promote.sh fast-forwards deploy to main after confirmation (#223)."""

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
PROMOTE = os.path.join(REPO_ROOT, "scripts", "promote.sh")


def _git(cwd, *args):
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).strip()


def _setup(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", "."); _git(work, "commit", "-m", "seed"); _git(work, "push")
    _git(work, "push", "origin", "main:deploy")  # deploy starts at main
    (work / "b.py").write_text("y = 2\n")
    _git(work, "add", "."); _git(work, "commit", "-m", "feature"); _git(work, "push")
    return origin, work


def _run(work, *args, stdin=""):
    return subprocess.run(
        ["bash", PROMOTE, *args], cwd=work, text=True, input=stdin,
        capture_output=True)


def test_promote_yes_fast_forwards_deploy(tmp_path):
    origin, work = _setup(tmp_path)
    r = _run(work, "--yes")
    assert r.returncode == 0, r.stderr
    assert _git(work, "rev-parse", "origin/main") != ""
    _git(work, "fetch", "origin")
    assert (_git(work, "rev-parse", "origin/deploy")
            == _git(work, "rev-parse", "origin/main"))


def test_promote_shows_range_and_aborts_on_no(tmp_path):
    origin, work = _setup(tmp_path)
    r = _run(work, stdin="n\n")
    assert r.returncode != 0
    assert "feature" in r.stdout  # the pending commit is listed
    _git(work, "fetch", "origin")
    assert (_git(work, "rev-parse", "origin/deploy")
            != _git(work, "rev-parse", "origin/main"))


def test_promote_noop_when_already_promoted(tmp_path):
    origin, work = _setup(tmp_path)
    assert _run(work, "--yes").returncode == 0
    r = _run(work, "--yes")
    assert r.returncode == 0
    assert "up to date" in (r.stdout + r.stderr).lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_promote_sh.py -v`
Expected: FAIL — promote.sh missing.

- [ ] **Step 3: Write `scripts/promote.sh`**

```bash
#!/usr/bin/env bash
# promote.sh — promote staging (main) to prod: fast-forward the deploy
# branch to main. The prod deployer (lfg-deployer) picks the move up within
# ~60s and drain-restarts the prod stack. (#223)
#
# Usage: scripts/promote.sh [--yes]
set -euo pipefail

REMOTE="${PROMOTE_REMOTE:-origin}"
YES=0
[ "${1:-}" = "--yes" ] && YES=1

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

git push "$REMOTE" "$MAIN:refs/heads/deploy"
echo "Promoted. lfg-deployer will deploy prod within ~60s (watch: pm2 logs lfg-deployer)."
```

Then: `chmod +x scripts/promote.sh`

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_promote_sh.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/promote.sh tests/test_promote_sh.py
git commit -m "feat(deploy): promote.sh — confirmed ff of deploy branch to main (#223)"
```

---

### Task 5: pm2 ecosystem files for both stacks

**Files:**
- Create: `ecosystem.prod.config.js`
- Create: `ecosystem.staging.config.js`

**Interfaces:**
- Produces: `pm2 start ecosystem.prod.config.js` / `pm2 start ecosystem.staging.config.js` recreate each full stack. Process names/args mirror the live pm2 state captured 2026-07-15 (see below), plus the new deployers; `lfg-index-testnet` is intentionally ABSENT from prod (it moves to staging as `stg-index-testnet`).

- [ ] **Step 1: Write `ecosystem.prod.config.js`**

```js
// Prod stack (~/LFG, branch: deploy, mainnet). pm2 start ecosystem.prod.config.js
// NOTE: lfg-index-testnet moved to the staging stack (stg-index-testnet). (#223)
const CWD = "/home/hamsa/LFG";
const PY = `${CWD}/.venv/bin/python`;

module.exports = {
  apps: [
    { name: "lfg-bot", cwd: CWD, script: "main.py", interpreter: PY },
    { name: "lfg-activity", cwd: CWD, script: `${PY}`, args: ["-m", "webapp.server"], interpreter: "none" },
    { name: "lfg-telegram", cwd: CWD, script: "run_telegram.py", interpreter: PY },
    { name: "lfg-index-mainnet", cwd: CWD, script: "scripts/onchain_listener.py", interpreter: PY, args: ["--network", "mainnet", "listen"] },
    { name: "lfg-snapshot", cwd: CWD, script: "scripts/snapshot_balances.py", interpreter: PY, args: ["--network", "mainnet"], cron_restart: "10 0 * * *", autorestart: false },
    { name: "lfg-deployer", cwd: CWD, script: "scripts/deployer.py", interpreter: PY, args: ["prod"] },
  ],
};
```

- [ ] **Step 2: Write `ecosystem.staging.config.js`**

```js
// Staging stack (~/LFG-staging, branch: main, testnet, economy enabled).
// pm2 start ecosystem.staging.config.js
// stg-bot / stg-telegram need staging tokens in ~/LFG-staging/.env first —
// until then start the file and pm2 stop stg-bot stg-telegram. (#223)
const CWD = "/home/hamsa/LFG-staging";
const PY = `${CWD}/.venv/bin/python`;

module.exports = {
  apps: [
    { name: "stg-bot", cwd: CWD, script: "main.py", interpreter: PY },
    { name: "stg-activity", cwd: CWD, script: `${PY}`, args: ["-m", "webapp.server"], interpreter: "none" },
    { name: "stg-telegram", cwd: CWD, script: "run_telegram.py", interpreter: PY },
    { name: "stg-index-testnet", cwd: CWD, script: "scripts/onchain_listener.py", interpreter: PY, args: ["--network", "testnet", "listen"] },
    { name: "stg-snapshot", cwd: CWD, script: "scripts/snapshot_balances.py", interpreter: PY, args: ["--network", "testnet"], cron_restart: "10 0 * * *", autorestart: false },
    { name: "stg-deployer", cwd: CWD, script: "scripts/deployer.py", interpreter: PY, args: ["staging"] },
  ],
};
```

- [ ] **Step 3: Validate both files parse**

Run: `node -e "console.log(require('./ecosystem.prod.config.js').apps.map(a=>a.name).join(','))" && node -e "console.log(require('./ecosystem.staging.config.js').apps.map(a=>a.name).join(','))"`
Expected: `lfg-bot,lfg-activity,lfg-telegram,lfg-index-mainnet,lfg-snapshot,lfg-deployer` and the `stg-*` equivalent. (If `node` isn't on PATH, use `~/.nvm/versions/node/v20.20.0/bin/node`.)

- [ ] **Step 4: Commit**

```bash
git add ecosystem.prod.config.js ecosystem.staging.config.js
git commit -m "feat(deploy): committed pm2 ecosystem files for prod + staging stacks (#223)"
```

---

### Task 6: Env example, retire post-merge hook, CLAUDE.md rewrite

**Files:**
- Create: `docs/ops/env.staging.example`
- Delete: `scripts/hooks/post-merge`
- Modify: `CLAUDE.md` (the "Running (pm2-managed)" section)

**Interfaces:**
- Consumes: stack definitions from Tasks 1/5 (names, ports, branches).

- [ ] **Step 1: Write `docs/ops/env.staging.example`**

```bash
# Staging-stack .env deltas (#223). Copy ~/LFG/.env to ~/LFG-staging/.env,
# then apply these overrides. Everything not listed stays identical to prod
# (same XUMM app, same Bunny zone — uploads land under the same folder).

XRPL_NETWORK=testnet
ECONOMY_ENABLED=1
WEBAPP_PORT=8177
LFG_SERVICE_URL=http://localhost:8177

# Staging surfaces — create a separate Discord app (+ test-guild install)
# and a separate BotFather bot; until these are filled, keep stg-bot and
# stg-telegram stopped (pm2 stop stg-bot stg-telegram).
DISCORD_BOT_TOKEN=<staging discord bot token>
DISCORD_GUILD_ID=<test guild id>
ADMIN_LOG_CHANNEL_ID=<test guild admin channel>
TELEGRAM_BOT_TOKEN=<staging telegram bot token>
TELEGRAM_ANNOUNCE_CHAT_ID=<staging telegram channel>
# Distinct service tokens so a leaked staging token grants nothing on prod:
SERVICE_TOKEN_DISCORD=<new random value: openssl rand -hex 32>
SERVICE_TOKEN_TELEGRAM=<new random value: openssl rand -hex 32>

# Mini App / Activity ingress for staging (second Funnel route):
TELEGRAM_MINI_APP_URL=<staging public https url, path /lfg-staging>
```

- [ ] **Step 2: Delete the tracked post-merge hook and its installed copy**

```bash
git rm scripts/hooks/post-merge
```

Also grep for references so none dangle: `grep -rn "post-merge" --include="*.py" --include="*.md" --include="*.sh" . | grep -v docs/superpowers | grep -v .git/`. Update `setup.sh` if it installs the hook (check: `grep -n "post-merge" setup.sh`); remove that install step if present. (The live `~/LFG/.git/hooks/post-merge` is removed at rollout, not by this commit — see the runbook.)

- [ ] **Step 3: Rewrite the CLAUDE.md "Running (pm2-managed)" section**

Replace the existing single-table section with:

```markdown
### Running (two pm2 stacks, branch-driven — #223)

Two stacks on one box. **`main` = staging** (testnet, economy enabled,
`~/LFG-staging`); **`deploy` = prod** (mainnet, `~/LFG`). Each stack runs a
polling deployer (`scripts/deployer.py`, 60s) that fast-forwards its checkout
when its branch moves, pip-installs on requirements changes, and
drain-restarts the stack (prod refuses to restart if sessions won't drain —
manual `pm2 restart ... --update-env` then). Merging a PR to `main`
auto-deploys STAGING ONLY. Promote to prod with `scripts/promote.sh`
(confirmed fast-forward of `deploy` to `main`). The old post-merge
auto-restart hook is retired.

| prod (`~/LFG`, deploy, mainnet) | staging (`~/LFG-staging`, main, testnet) |
|---|---|
| `lfg-bot` | `stg-bot` (stopped until staging Discord token) |
| `lfg-activity` :8176 | `stg-activity` :8177 |
| `lfg-telegram` | `stg-telegram` (stopped until staging TG token) |
| `lfg-index-mainnet` | `stg-index-testnet` (moved out of prod) |
| `lfg-snapshot` (cron 00:10) | `stg-snapshot` (cron 00:10, testnet) |
| `lfg-deployer` | `stg-deployer` |

Ecosystem files: `ecosystem.prod.config.js` / `ecosystem.staging.config.js`.
Staging env deltas: `docs/ops/env.staging.example`. The `~/LFG` working copy
sits on `deploy` — do day-to-day dev in worktrees/feature branches, not by
switching `~/LFG` back to `main` (the deployer would halt on divergence).
Rollback: `git push origin <sha>:deploy --force-with-lease`, then on the box
`scripts/deployer.py prod --once --force-reset`.
```

Keep the surrounding Telegram-shim warning paragraph (it still applies to both stacks).

- [ ] **Step 4: Run the full gate**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check .`
Expected: full suite passes (no code behavior changed in this task).

- [ ] **Step 5: Commit**

```bash
git add docs/ops/env.staging.example CLAUDE.md setup.sh
git commit -m "docs(deploy): staging env example, retire post-merge hook, two-stack CLAUDE.md (#223)"
```

---

### Task 7: Rollout runbook

**Files:**
- Create: `docs/ops/staging-prod-rollout.md`

**Interfaces:** none (pure ops doc; executed manually by the operator after merge).

- [ ] **Step 1: Write `docs/ops/staging-prod-rollout.md`**

```markdown
# Rollout: staging/prod stack split (#223)

One-time steps, in order, AFTER the code lands on `main`. Steps 1–7 are safe
to run while prod serves traffic; only step 8 restarts prod processes.

## 1. Create the deploy branch (prod pins here)
    git push origin main:deploy

## 2. Pin ~/LFG to deploy
    cd ~/LFG && git fetch origin && git checkout -B deploy origin/deploy
(Identical tree to main at this moment — nothing running changes.)

## 3. Remove the retired post-merge hook from the live checkout
    rm -f ~/LFG/.git/hooks/post-merge

## 4. Build the staging checkout
    git clone git@github.com:Team-Hamsa/LFG.git ~/LFG-staging
    cd ~/LFG-staging && ./setup.sh
    cp ~/LFG/.env ~/LFG-staging/.env
    # then apply every override in docs/ops/env.staging.example
    $EDITOR ~/LFG-staging/.env

## 5. Move the testnet listener + start staging
    pm2 delete lfg-index-testnet
    pm2 start ~/LFG-staging/ecosystem.staging.config.js
    pm2 stop stg-bot stg-telegram        # until staging tokens exist
    pm2 save

## 6. Staging ingress (second Funnel route)
    tailscale serve --bg --set-path /lfg-staging http://127.0.0.1:8177
    tailscale funnel status   # verify /lfg (8176) and /lfg-staging (8177)

## 7. Start the prod deployer (no restarts yet — deploy == main)
    pm2 start ~/LFG/ecosystem.prod.config.js --only lfg-deployer
    pm2 save

## 8. Adopt the prod ecosystem file (first drain-restart of prod)
Existing lfg-* processes keep their old ad-hoc definitions until restarted
via the file. At a quiet moment:
    pm2 delete lfg-bot lfg-activity lfg-telegram lfg-index-mainnet lfg-snapshot
    pm2 start ~/LFG/ecosystem.prod.config.js
    pm2 save

## 9. Verify end-to-end
- Push a trivial commit to main → `pm2 logs stg-deployer` shows the
  fast-forward; a doc-only commit advances without restarts.
- `scripts/promote.sh` → `pm2 logs lfg-deployer` shows drain + restart.
- `curl -s localhost:8177/api/health` and `:8176/api/health` both OK.

## Later (non-blocking ops)
- Create the staging Discord app (install to a test guild) and BotFather
  bot; fill tokens in ~/LFG-staging/.env; `pm2 restart stg-bot stg-telegram`.
```

- [ ] **Step 2: Commit**

```bash
git add docs/ops/staging-prod-rollout.md
git commit -m "docs(deploy): rollout runbook for the two-stack split (#223)"
```

---

### Task 8: Full-gate verification + cross-stack collision audit

**Files:** none new — verification task.

- [ ] **Step 1: Run the complete pre-push gate**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: everything green (fix anything that isn't before proceeding).

- [ ] **Step 2: Cross-stack collision audit (spec requirement)**

Grep for absolute paths / hardcoded ports that would collide between the two checkouts, and record findings in the PR description:

```bash
grep -rn "8176\|/home/hamsa/LFG\b" lfg_core/ lfg_service/ webapp/ surfaces/ scripts/ --include="*.py" | grep -v test | grep -v deployer.py
grep -n "ECONOMY_RECORDS_DIR\|LFG_SERVICE_URL" lfg_core/config.py lfg_service/*.py surfaces/ -r
```

Expected outcome: everything is either env-driven (`WEBAPP_PORT`, `LFG_SERVICE_URL`) or checkout-relative (records dirs, DBs, layers) — both stacks get their own copies via cwd. Any absolute-path collision found = fix it in this task (make it env/cwd-relative) with a test.

- [ ] **Step 3: Commit any audit fixes**

```bash
git add -A && git commit -m "fix(deploy): cross-stack collision audit fixes (#223)"
```

(Skip the commit if the audit is clean.)
