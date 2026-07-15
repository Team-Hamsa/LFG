"""promote.sh fast-forwards deploy to main after confirmation (#223)."""

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
PROMOTE = os.path.join(REPO_ROOT, "scripts", "promote.sh")


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


def _setup(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "seed")
    _git(work, "push")
    _git(work, "push", "origin", "main:deploy")  # deploy starts at main
    (work / "b.py").write_text("y = 2\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "feature")
    _git(work, "push")
    return origin, work


def _run(work, *args, stdin=""):
    return subprocess.run(
        ["bash", PROMOTE, *args], cwd=work, text=True, input=stdin, capture_output=True
    )


def test_promote_yes_fast_forwards_deploy(tmp_path):
    origin, work = _setup(tmp_path)
    r = _run(work, "--yes")
    assert r.returncode == 0, r.stderr
    assert _git(work, "rev-parse", "origin/main") != ""
    _git(work, "fetch", "origin")
    assert _git(work, "rev-parse", "origin/deploy") == _git(work, "rev-parse", "origin/main")


def test_promote_shows_range_and_aborts_on_no(tmp_path):
    origin, work = _setup(tmp_path)
    r = _run(work, stdin="n\n")
    assert r.returncode != 0
    assert "feature" in r.stdout  # the pending commit is listed
    _git(work, "fetch", "origin")
    assert _git(work, "rev-parse", "origin/deploy") != _git(work, "rev-parse", "origin/main")


def test_promote_rejects_unknown_arg(tmp_path):
    origin, work = _setup(tmp_path)
    r = _run(work, "--bogus")
    assert r.returncode == 2
    assert "usage" in (r.stdout + r.stderr).lower()
    _git(work, "fetch", "origin")
    assert _git(work, "rev-parse", "origin/deploy") != _git(work, "rev-parse", "origin/main")


def test_promote_noop_when_already_promoted(tmp_path):
    origin, work = _setup(tmp_path)
    assert _run(work, "--yes").returncode == 0
    r = _run(work, "--yes")
    assert r.returncode == 0
    assert "up to date" in (r.stdout + r.stderr).lower()
