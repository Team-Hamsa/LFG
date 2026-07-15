"""Deployer git/decision core (#223). No lfg_core imports — deployer.py is
stdlib-only by design, so no env-guard preamble here."""

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
            # Scrub GIT_* (a pre-push hook exports GIT_DIR etc., which would
            # point these subprocesses at the OUTER repo, not tmp_path).
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    ).strip()


def _make_repos(tmp_path):
    """origin bare repo + working clone on branch main, one seed commit."""
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
    return origin, seed, clone


def _cfg(clone, branch="main"):
    return deployer.StackConfig(
        name="staging",
        checkout=str(clone),
        branch=branch,
        health_url="http://127.0.0.1:1/api/health",
        drain_max_wait=1,
        drain_poll=0,
        refuse_on_drain_failure=False,
        restart_processes=("stg-activity",),
        pip=".venv/bin/pip",
    )


def test_remote_head_moves_after_push(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    deployer.fetch(cfg)
    assert deployer.remote_head(cfg) == deployer.local_head(cfg)
    (seed / "b.py").write_text("y = 2\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "more")
    _git(seed, "push")
    deployer.fetch(cfg)
    assert deployer.remote_head(cfg) != deployer.local_head(cfg)


def test_is_fast_forward_true_for_descendant(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    old = deployer.local_head(cfg)
    (seed / "b.py").write_text("y = 2\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "more")
    _git(seed, "push")
    deployer.fetch(cfg)
    assert deployer.is_fast_forward(cfg, old, deployer.remote_head(cfg))


def test_is_fast_forward_false_for_diverged(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    # local-only commit in the clone diverges it from a new origin commit
    (clone / "local.py").write_text("z = 3\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "local")
    (seed / "b.py").write_text("y = 2\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "remote")
    _git(seed, "push")
    deployer.fetch(cfg)
    assert not deployer.is_fast_forward(cfg, deployer.local_head(cfg), deployer.remote_head(cfg))


def test_changed_files_lists_the_delta(tmp_path):
    origin, seed, clone = _make_repos(tmp_path)
    cfg = _cfg(clone)
    old = deployer.local_head(cfg)
    (seed / "docs").mkdir()
    (seed / "docs" / "n.md").write_text("hi\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "docs")
    _git(seed, "push")
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
