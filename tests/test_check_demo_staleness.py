"""Tests for scripts/check_demo_staleness.py.

Lag math is exercised through the pure functions with injected timestamps —
no git and no wall clock. A temp git repo fixture (commits dated via
GIT_AUTHOR_DATE/GIT_COMMITTER_DATE) covers the end-to-end run(): strict vs
advisory exit codes and the missing-file error.
"""

import subprocess
from pathlib import Path

import pytest

from scripts.check_demo_staleness import (
    assess_gif,
    compute_lag_days,
    git_last_commit_ts,
    parse_readme_gifs,
    run,
)

DAY = 86400


# ---------------------------------------------------------------- pure logic


def test_lag_days_positive_when_client_newer():
    assert compute_lag_days(gif_commit_ts=0, client_commit_ts=10 * DAY) == 10.0


def test_lag_days_negative_when_gif_newer():
    assert compute_lag_days(gif_commit_ts=5 * DAY, client_commit_ts=0) == -5.0


def test_assess_gif_within_grace_not_stale():
    s = assess_gif("assets/demo/mint.gif", 0, 21 * DAY, max_lag_days=21.0)
    assert not s.stale
    assert s.lag_days == 21.0


def test_assess_gif_beyond_grace_stale():
    s = assess_gif("assets/demo/mint.gif", 0, 22 * DAY, max_lag_days=21.0)
    assert s.stale


def test_assess_gif_fresh_gif_never_stale():
    s = assess_gif("assets/demo/mint.gif", 100 * DAY, 0, max_lag_days=0.0)
    assert not s.stale


def test_parse_readme_gifs_dedup_and_order():
    text = (
        '<img src="assets/demo/mint.gif"> text\n'
        '<img src="assets/demo/swap.gif">\n'
        "again assets/demo/mint.gif\n"
        "unrelated assets/other/x.gif\n"
    )
    assert parse_readme_gifs(text) == ["assets/demo/mint.gif", "assets/demo/swap.gif"]


# ------------------------------------------------------------- git fixtures


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin",
    }
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    result = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed: {result.stderr}")


def _commit_file(repo: Path, rel: str, content: str, ts: int) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", f"add {rel}", date=f"@{ts} +0000")


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    """Repo with one GIF committed at t=0 and a client change 30 days later."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit_file(repo, "README.md", '<img src="assets/demo/mint.gif">\n', 1_000_000)
    _commit_file(repo, "assets/demo/mint.gif", "gifdata", 1_000_000)
    _commit_file(repo, "webapp/client/app.js", "v2", 1_000_000 + 30 * DAY)
    return repo


# --------------------------------------------------------------- end-to-end


def test_git_last_commit_ts(demo_repo: Path):
    assert git_last_commit_ts(demo_repo, "assets/demo/mint.gif") == 1_000_000
    assert git_last_commit_ts(demo_repo, "webapp/client/") == 1_000_000 + 30 * DAY
    assert git_last_commit_ts(demo_repo, "nonexistent/thing") is None


def test_advisory_mode_exits_zero_on_stale(demo_repo: Path, capsys):
    assert run(demo_repo, max_lag_days=21.0, strict=False) == 0
    assert "WARNING" in capsys.readouterr().out


def test_strict_mode_exits_nonzero_on_stale(demo_repo: Path):
    assert run(demo_repo, max_lag_days=21.0, strict=True) == 1


def test_within_grace_clean_even_strict(demo_repo: Path, capsys):
    assert run(demo_repo, max_lag_days=31.0, strict=True) == 0
    assert "WARNING" not in capsys.readouterr().out


def test_missing_gif_is_always_error(demo_repo: Path, capsys):
    (demo_repo / "assets/demo/mint.gif").unlink()
    assert run(demo_repo, max_lag_days=1000.0, strict=False) == 1
    assert "missing from disk" in capsys.readouterr().out
