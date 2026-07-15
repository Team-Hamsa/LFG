"""End-to-end run_once against throwaway git repos (#223)."""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import deployer


def _git(cwd, *args):
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    ).strip()


def _make_repos(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    (seed / "a.py").write_text("x = 1\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    return seed, clone


def _cfg(clone, refuse=False):
    return deployer.StackConfig(
        name="t",
        checkout=str(clone),
        branch="main",
        health_url="u",
        drain_max_wait=1,
        drain_poll=0,
        refuse_on_drain_failure=refuse,
        restart_processes=("p1",),
        pip="/definitely/missing/pip",
    )


DRAINED = lambda u: b'{"active_sessions": 0}'  # noqa: E731


def _push(seed, relpath, content="x\n", msg="c"):
    p = seed / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", msg)
    _git(seed, "push")


def test_up_to_date(tmp_path):
    seed, clone = _make_repos(tmp_path)
    assert deployer.run_once(_cfg(clone), fetcher=DRAINED, runner=lambda c: 0) == "up_to_date"


def test_code_change_advances_and_restarts(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "lfg_core/new.py")
    calls = []

    def boom():
        raise OSError("no pm2")

    out = deployer.run_once(
        _cfg(clone), fetcher=DRAINED, runner=lambda c: calls.append(c) or 0, lister=boom
    )
    assert out == "restarted"
    assert (clone / "lfg_core" / "new.py").exists()
    assert ["pm2", "restart", "p1", "--update-env"] in calls


def test_docs_change_advances_without_restart(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "docs/note.md", "hi\n")
    calls = []
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED, runner=lambda c: calls.append(c) or 0)
    assert out == "advanced_no_restart"
    assert (clone / "docs" / "note.md").exists()
    assert calls == []


def test_requirements_change_runs_pip_before_restart(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "requirements.txt", "aiohttp\n")
    calls = []
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED, runner=lambda c: calls.append(c) or 0)
    assert out == "restarted"
    assert calls[0] == ["/definitely/missing/pip", "install", "-r", "requirements.txt"]


def test_pip_failure_blocks_restart(tmp_path):
    seed, clone = _make_repos(tmp_path)
    _push(seed, "requirements.txt", "aiohttp\n")
    out = deployer.run_once(
        _cfg(clone), fetcher=DRAINED, runner=lambda c: 1 if c[1] == "install" else 0
    )
    assert out == "pip_failed"


def test_diverged_halts_without_touching_checkout(tmp_path):
    seed, clone = _make_repos(tmp_path)
    (clone / "local.py").write_text("z\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "local")
    local = _git(clone, "rev-parse", "HEAD")
    _push(seed, "lfg_core/new.py")
    out = deployer.run_once(_cfg(clone), fetcher=DRAINED, runner=lambda c: 0)
    assert out == "halted_not_ff"
    assert _git(clone, "rev-parse", "HEAD") == local


def test_force_reset_recovers_diverged(tmp_path):
    seed, clone = _make_repos(tmp_path)
    (clone / "local.py").write_text("z\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "local")
    _push(seed, "lfg_core/new.py")
    out = deployer.run_once(_cfg(clone), force_reset=True, fetcher=DRAINED, runner=lambda c: 0)
    assert out == "restarted"
    assert _git(clone, "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")


def test_missing_remote_branch(tmp_path):
    seed, clone = _make_repos(tmp_path)
    cfg = deployer.StackConfig(
        name="t",
        checkout=str(clone),
        branch="deploy",
        health_url="u",
        drain_max_wait=1,
        drain_poll=0,
        refuse_on_drain_failure=False,
        restart_processes=("p1",),
        pip="pip",
    )
    assert deployer.run_once(cfg, fetcher=DRAINED, runner=lambda c: 0) == "no_remote_branch"


def test_prod_refusal_leaves_checkout_advanced(tmp_path):
    # The ff-merge happens first; a refused restart still leaves new code on
    # disk (matching the old hook: pull landed, restart deferred to a human).
    seed, clone = _make_repos(tmp_path)
    _push(seed, "lfg_core/new.py")
    out = deployer.run_once(
        _cfg(clone, refuse=True),
        fetcher=lambda u: (_ for _ in ()).throw(OSError()),
        runner=lambda c: 0,
    )
    assert out == "refused"
    assert (clone / "lfg_core" / "new.py").exists()
