"""Per-stack polling deployer (#223).

Runs under pm2 as stg-deployer / lfg-deployer. Polls `git fetch`; when the
stack's tracked branch moves and the update is a fast-forward, advances the
checkout, pip-installs on requirements changes, and drain-aware-restarts the
stack's pm2 processes (generalizing the retired post-merge hook).

STDLIB ONLY by design: this must run before `pip install` and must never be
broken by app code. Do not import lfg_core or any third-party package.
"""

from __future__ import annotations

import argparse  # noqa: F401
import json  # noqa: F401
import re
import subprocess
import sys  # noqa: F401
import time  # noqa: F401
import urllib.request  # noqa: F401
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
        restart_processes=("stg-bot", "stg-activity", "stg-telegram", "stg-index-testnet"),
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
        restart_processes=("lfg-bot", "lfg-activity", "lfg-telegram", "lfg-index-mainnet"),
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
