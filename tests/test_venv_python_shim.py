"""Tests for scripts/venv-python — the pre-push gate's shared venv interpreter shim (#315).

The shim resolves the ONE shared project venv via `git rev-parse
--git-common-dir` so the pre-push hooks run identically in the main checkout
and in any git worktree, and it must HARD-FAIL loudly (non-zero, actionable
message) when no venv exists — never silently skip.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM = REPO_ROOT / "scripts" / "venv-python"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy(SHIM, scripts / "venv-python")
    (scripts / "venv-python").chmod((scripts / "venv-python").stat().st_mode | stat.S_IXUSR)
    (repo / "x.txt").write_text("x")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    return repo


def _fake_python(root: Path) -> Path:
    """Install a stub .venv/bin/python that echoes a marker and its args."""
    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    py.write_text('#!/usr/bin/env bash\necho "FAKE_PYTHON_AT:$0 ARGS:$*"\n')
    py.chmod(0o755)
    return py


def test_shim_is_executable() -> None:
    assert SHIM.exists()
    assert os.access(SHIM, os.X_OK)


def test_resolves_real_repo_venv() -> None:
    """From this repo, the shim execs the shared venv's python."""
    out = subprocess.run(
        [str(SHIM), "-c", "import sys; print(sys.executable)"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith(".venv/bin/python")


def test_main_checkout_resolution(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _fake_python(repo)
    out = subprocess.run(
        [str(repo / "scripts" / "venv-python"), "-m", "pytest"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "FAKE_PYTHON_AT:" in out.stdout
    assert "ARGS:-m pytest" in out.stdout


def test_worktree_resolves_main_checkout_venv(tmp_path: Path) -> None:
    """From a worktree with NO local .venv, the shim uses the main checkout's venv."""
    repo = _make_repo(tmp_path)
    fake = _fake_python(repo)
    wt = tmp_path / "wt"
    _git("worktree", "add", str(wt), "-b", "wtbranch", cwd=repo)
    out = subprocess.run(
        [str(wt / "scripts" / "venv-python"), "-m", "mypy", "."],
        cwd=wt,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert f"FAKE_PYTHON_AT:{fake}" in out.stdout


def test_missing_venv_fails_loudly(tmp_path: Path) -> None:
    """No venv anywhere → non-zero exit + actionable message (never a silent skip)."""
    repo = _make_repo(tmp_path)
    out = subprocess.run(
        [str(repo / "scripts" / "venv-python"), "-m", "pytest"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "setup.sh" in out.stderr
    assert ".venv/bin/python" in out.stderr


def test_worktree_missing_main_venv_fails_loudly(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt2"
    _git("worktree", "add", str(wt), "-b", "wtbranch2", cwd=repo)
    out = subprocess.run(
        [str(wt / "scripts" / "venv-python"), "-m", "pytest"],
        cwd=wt,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "setup.sh" in out.stderr
