# tests/test_git_config_guard.py
# Git exports GIT_DIR to its hooks, and the pre-push gate runs the suite from
# one, so a temp-repo `git config` that inherits the environment writes the
# OUTER repo's config — the one file every worktree shares. #373's first draft
# did exactly that on 2026-08-16, and every commit made from ~/LFG or any of
# its worktrees was authored `t <t@t>` until 2026-09-24. The root conftest now
# diffs that config across the session and fails the run on any change.
import os
import subprocess
from pathlib import Path

import conftest

_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _scrubbed_env() -> dict[str, str]:
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **_IDENTITY}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=_scrubbed_env())


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    return repo, wt


def _armed(path: str | None) -> conftest._GitConfigGuard:
    guard = conftest._GitConfigGuard(path)
    guard.entries_at_start = guard.entries()
    return guard


def test_hook_env_git_config_lands_in_the_file_the_guard_watches(tmp_path, monkeypatch):
    repo, wt = _repo_with_worktree(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # What git exports to a pre-push hook run from a worktree.
    monkeypatch.setenv("GIT_DIR", str(repo / ".git" / "worktrees" / "wt"))

    watched = conftest._enclosing_git_config(str(wt))
    assert watched == os.path.realpath(repo / ".git" / "config")
    guard = _armed(watched)

    # The 2026-08-16 bug: a "temp repo" `git config` that kept the hook's env.
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=scratch, check=True, env=dict(os.environ)
    )
    assert guard.changes() == ["added user.name=t"]


def test_guard_reports_added_and_removed_entries(tmp_path):
    repo, _ = _repo_with_worktree(tmp_path)
    guard = _armed(str(repo / ".git" / "config"))

    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "--unset", "core.bare")

    assert guard.changes() == ["added user.email=t@t", "removed core.bare=false"]


def test_guard_ignores_branch_and_remote_churn(tmp_path):
    # Other git processes rewrite these while a run is in flight (worktree add
    # with tracking, push -u, gh pr checkout); they are not a test's leak.
    repo, _ = _repo_with_worktree(tmp_path)
    guard = _armed(str(repo / ".git" / "config"))

    _git(repo, "remote", "add", "origin", "https://example.invalid/r.git")
    _git(repo, "config", "branch.wt.remote", "origin")
    _git(repo, "config", "branch.wt.merge", "refs/heads/main")

    assert guard.changes() == []


def test_guard_is_inert_outside_a_checkout(tmp_path, monkeypatch):
    for var in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    assert conftest._enclosing_git_config(str(tmp_path)) is None
    assert _armed(None).changes() == []


def test_live_guard_watches_this_checkouts_shared_config():
    root = Path(__file__).resolve().parents[1]
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    guard = conftest._GIT_CONFIG_GUARD
    assert guard.path == os.path.realpath(root / common / "config")
    # Armed at session start: the snapshot is this file's content (no key is
    # assumed present — git writes none of them compulsorily).
    assert guard.entries_at_start == guard.entries()
