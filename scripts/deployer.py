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
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
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
    # Scrub inherited GIT_* vars (e.g. GIT_DIR/GIT_INDEX_FILE exported by a
    # git hook environment) so git always operates on `cwd`, never the repo
    # that happened to spawn us.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, timeout=120, env=env
    ).strip()


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


def _default_fetcher(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.read()  # type: ignore[no-any-return]


def active_sessions(
    url: str,
    fetcher: Callable[[str], bytes] | None = None,
) -> int | None:
    # -> int | None ; None means unreachable or malformed (fail-unknown).
    fetcher = fetcher or _default_fetcher
    try:
        body = json.loads(fetcher(url))
        n = body["active_sessions"]
        return n if isinstance(n, int) else None
    except Exception:
        return None


def drain(
    cfg: StackConfig,
    fetcher: Callable[[str], bytes] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    deadline = clock() + cfg.drain_max_wait
    first = True
    while True:
        n = active_sessions(cfg.health_url, fetcher=fetcher)
        if n is None:
            # Unreachable (first probe or mid-drain): we cannot confirm the
            # session count — report it and let posture decide.
            log(
                f"{cfg.name}: /api/health unreachable"
                + (" on first probe" if first else " mid-drain")
            )
            return "unreachable"
        first = False
        if n == 0:
            return "drained"
        if clock() >= deadline:
            log(f"{cfg.name}: {n} session(s) still in flight after {cfg.drain_max_wait}s")
            return "timeout"
        log(f"{cfg.name}: {n} in-flight session(s); waiting for drain…")
        sleeper(cfg.drain_poll)


def _default_runner(cmd: list[str]) -> int:
    return subprocess.call(cmd, timeout=300)


def _default_lister() -> str:
    return subprocess.check_output(["pm2", "jlist"], text=True, timeout=300)


def pm2_online(jlist_json: str) -> set[str]:
    """Names of pm2-managed processes currently in the `online` state."""
    data = json.loads(jlist_json)
    return {
        proc["name"]
        for proc in data
        if isinstance(proc, dict) and proc.get("pm2_env", {}).get("status") == "online"
    }


def restart_stack(
    cfg: StackConfig,
    runner: Callable[[list[str]], int] | None = None,
    lister: Callable[[], str] | None = None,
) -> bool:
    runner = runner or _default_runner
    lister = lister or _default_lister
    try:
        online = pm2_online(lister())
        targets = [p for p in cfg.restart_processes if p in online]
        for name in cfg.restart_processes:
            if name not in online:
                log(f"{cfg.name}: skipping restart of {name} (not online)")
    except Exception as exc:  # pm2 unreachable/unparsable: fall back to old behavior
        log(f"{cfg.name}: WARNING pm2 jlist failed ({exc!r}); restarting full configured list")
        targets = list(cfg.restart_processes)
    if not targets and cfg.restart_processes:
        # Legitimate no-op: offline processes load the new code from disk
        # when next started — but say so instead of implying restarts ran.
        log(
            f"{cfg.name}: no configured processes online; nothing restarted "
            "(new code takes effect when they start)"
        )
        return True
    ok = True
    for name in targets:
        rc = runner(["pm2", "restart", name, "--update-env"])
        if rc != 0:
            log(f"{cfg.name}: WARNING pm2 restart {name} failed (rc={rc})")
            ok = False
    return ok


def drain_and_restart(
    cfg: StackConfig,
    fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[[list[str]], int] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    lister: Callable[[], str] | None = None,
) -> str:
    outcome = drain(cfg, fetcher=fetcher, sleeper=sleeper, clock=clock)
    if outcome != "drained" and cfg.refuse_on_drain_failure:
        # Prod posture (verbatim from the retired post-merge hook): never
        # cut off in-flight mint/swap/market work; hand it to a human.
        log(
            f"{cfg.name}: drain outcome={outcome}; REFUSING auto-restart. "
            f"Restart manually when safe: pm2 restart "
            f"{' '.join(cfg.restart_processes)} --update-env"
        )
        return "refused"
    if outcome != "drained":
        log(f"{cfg.name}: drain outcome={outcome}; restarting anyway (staging posture)")
    return "restarted" if restart_stack(cfg, runner=runner, lister=lister) else "restart_failed"


def run_once(
    cfg: StackConfig,
    force_reset: bool = False,
    fetcher: Callable[[str], bytes] | None = None,
    runner: Callable[[list[str]], int] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    lister: Callable[[], str] | None = None,
) -> str:
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
        log(
            f"{cfg.name}: HALTED — origin/{cfg.branch} ({new[:12]}) is not a "
            f"fast-forward of local HEAD ({old[:12]}). A force-push or local "
            f"commit diverged the checkout. Fix manually, or run: "
            f"scripts/deployer.py {cfg.name} --once --force-reset"
        )
        return "halted_not_ff"

    files = changed_files(cfg, old, new)
    if needs_pip(files):
        log(f"{cfg.name}: requirements changed; running pip install")
        for req in ("requirements.txt", "requirements-dev.txt"):
            if req in files:
                if runner([cfg.pip, "install", "-r", req]) != 0:
                    log(
                        f"{cfg.name}: pip install -r {req} FAILED; "
                        "NOT restarting (old code keeps running)"
                    )
                    return "pip_failed"
    if not needs_restart(files):
        log(f"{cfg.name}: advanced to {new[:12]}; no restart-worthy changes")
        return "advanced_no_restart"
    return drain_and_restart(
        cfg, fetcher=fetcher, runner=runner, sleeper=sleeper, clock=clock, lister=lister
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LFG per-stack polling deployer")
    ap.add_argument("stack", choices=sorted(STACKS))
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument(
        "--force-reset",
        action="store_true",
        help="reset --hard to origin/<branch>; requires --once",
    )
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)
    if args.force_reset and not args.once:
        ap.error("--force-reset requires --once (deliberate one-shot recovery)")
    cfg = STACKS[args.stack]
    if args.once:
        out = run_once(cfg, force_reset=args.force_reset)
        log(f"{cfg.name}: {out}")
        return 0 if out not in ("halted_not_ff", "pip_failed", "restart_failed") else 1
    log(f"{cfg.name}: polling origin/{cfg.branch} every {args.interval}s")
    pending_restart = False  # sticky: a refused restart was never applied
    while True:
        try:
            out = run_once(cfg)
            if out != "up_to_date":
                log(f"{cfg.name}: cycle result: {out}")
            if out == "refused":
                pending_restart = True
            elif out != "up_to_date":
                # Any other real outcome (restarted, advanced, halted, ...)
                # supersedes the stale refusal.
                pending_restart = False
            elif pending_restart:
                # HEAD == origin/<branch>, but the refused restart from a
                # prior cycle was never applied — remind every cycle until
                # some cycle resolves it.
                log(
                    f"{cfg.name}: REMINDER — a prior drain-and-restart was "
                    f"refused; restart manually if not already done: pm2 "
                    f"restart {' '.join(cfg.restart_processes)} --update-env"
                )
        except Exception as exc:  # never die on a transient git/network error
            log(f"{cfg.name}: cycle error: {exc!r}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
