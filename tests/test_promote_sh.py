"""promote.sh fast-forwards deploy to main after confirmation (#223)."""

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
PROMOTE = os.path.join(REPO_ROOT, "scripts", "promote.sh")


def _scrubbed_env():
    # Scrub GIT_* (a pre-push hook exports GIT_DIR etc., which would point
    # these subprocesses at the OUTER repo, not tmp_path).
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        # Pin explicitly: an inherited PROMOTE_REMOTE could redirect these
        # tests at a different remote than the "origin" they set up.
        "PROMOTE_REMOTE": "origin",
    }


def _git(cwd, *args):
    return subprocess.check_output(
        ["git", *args],
        cwd=cwd,
        text=True,
        env=_scrubbed_env(),
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
        ["bash", PROMOTE, *args],
        cwd=work,
        text=True,
        input=stdin,
        capture_output=True,
        env=_scrubbed_env(),
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


def test_promote_rejects_extra_positional_args(tmp_path):
    origin, work = _setup(tmp_path)
    r = _run(work, "--yes", "extra")
    assert r.returncode == 2
    assert "usage" in (r.stdout + r.stderr).lower()
    _git(work, "fetch", "origin")
    assert _git(work, "rev-parse", "origin/deploy") != _git(work, "rev-parse", "origin/main")


def test_promote_rejects_non_fast_forward(tmp_path):
    origin, work = _setup(tmp_path)
    # Simulate someone committing directly to deploy: rewind a second clone
    # to origin/deploy and add a commit only deploy has, so deploy is no
    # longer an ancestor of main.
    second = tmp_path / "second"
    _git(tmp_path, "clone", str(origin), str(second))
    _git(second, "checkout", "-B", "deploy", "origin/deploy")
    (second / "diverged.py").write_text("z = 1\n")
    _git(second, "add", ".")
    _git(second, "commit", "-m", "direct commit to deploy")
    _git(second, "push", "origin", "deploy")

    r = _run(work, "--yes")
    assert r.returncode == 1
    assert "not a fast-forward" in (r.stdout + r.stderr).lower() or "NOT an ancestor" in (
        r.stdout + r.stderr
    )
    _git(work, "fetch", "origin")
    deploy_after = _git(work, "rev-parse", "origin/deploy")
    _git(second, "fetch", "origin")
    deploy_expected = _git(second, "rev-parse", "origin/deploy")
    assert deploy_after == deploy_expected  # no push happened


def test_promote_noop_when_already_promoted(tmp_path):
    origin, work = _setup(tmp_path)
    assert _run(work, "--yes").returncode == 0
    r = _run(work, "--yes")
    assert r.returncode == 0
    assert "up to date" in (r.stdout + r.stderr).lower()


# --- selective promotion (--list / --pick) ---------------------------------


def _merge_pr(work, n, filename, content):
    """Land a PR-style merge commit on main touching one file."""
    branch = f"pr{n}"
    _git(work, "checkout", "-q", "-b", branch, "main")
    path = work / filename
    path.write_text(content)
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", f"change for {n}")
    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from x/{branch}")
    _git(work, "push", "-q", "origin", "main")
    return _git(work, "rev-parse", "HEAD")


def _setup_prs(tmp_path):
    """deploy == main, then three PRs (#11, #12, #13) land on main."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(origin), str(work))
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "seed")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "push", "-q", "origin", "main:deploy")
    shas = {
        11: _merge_pr(work, 11, "eleven.py", "a = 11\n"),
        12: _merge_pr(work, 12, "twelve.py", "a = 12\n"),
        13: _merge_pr(work, 13, "thirteen.py", "a = 13\n"),
    }
    return origin, work, shas


def _run_gated(work, *args, gate="true", stdin=""):
    env = {**_scrubbed_env(), "PROMOTE_GATE_CMD": gate}
    return subprocess.run(
        ["bash", PROMOTE, *args], cwd=work, text=True, input=stdin, capture_output=True, env=env
    )


def _deploy_files(work):
    _git(work, "fetch", "-q", "origin")
    return set(_git(work, "ls-tree", "--name-only", "origin/deploy").split())


def test_list_shows_pending_prs(tmp_path):
    _, work, _ = _setup_prs(tmp_path)
    r = _run_gated(work, "--list")
    assert r.returncode == 0, r.stderr
    assert [line.split()[1] for line in r.stdout.splitlines()] == ["#11", "#12", "#13"]


def test_pick_skipping_middle_cherry_picks_and_gates(tmp_path):
    _, work, shas = _setup_prs(tmp_path)
    old_deploy = _git(work, "rev-parse", "origin/deploy")
    marker = tmp_path / "gate-ran"
    r = _run_gated(work, "--pick", "13", "11", "--yes", gate=f"touch {marker}")
    assert r.returncode == 0, r.stderr
    assert marker.exists()  # the gate ran on the pick build
    files = _deploy_files(work)
    assert {"eleven.py", "thirteen.py"} <= files
    assert "twelve.py" not in files
    # deploy only moved forward (the deployer needs a fast-forward)
    assert _run_ok(work, "merge-base", "--is-ancestor", old_deploy, "origin/deploy")
    # picks recorded, in main's order
    log = _git(work, "log", "--format=%B", "origin/main..origin/deploy")
    assert f"cherry picked from commit {shas[11]}" in log
    assert log.index(shas[13]) < log.index(shas[11])  # newest first in git log

    listed = _run_gated(work, "--list")
    assert [line.split()[1] for line in listed.stdout.splitlines()] == ["#12"]


def test_pick_prefix_is_plain_fast_forward_without_gate(tmp_path):
    _, work, shas = _setup_prs(tmp_path)
    marker = tmp_path / "gate-ran"
    r = _run_gated(work, "--pick", "11", "12", "--yes", gate=f"touch {marker}")
    assert r.returncode == 0, r.stderr
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", "origin/deploy") == shas[12]
    assert not marker.exists()


def test_pick_gate_failure_pushes_nothing(tmp_path):
    _, work, _ = _setup_prs(tmp_path)
    before = _git(work, "rev-parse", "origin/deploy")
    r = _run_gated(work, "--pick", "12", "--yes", gate="exit 1")
    assert r.returncode == 1
    assert "gate failed" in r.stderr.lower()
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", "origin/deploy") == before
    assert "promote-pick-" not in _git(work, "worktree", "list")  # cleaned up


def test_pick_conflict_pushes_nothing(tmp_path):
    _, work, _ = _setup_prs(tmp_path)
    _merge_pr(work, 14, "eleven.py", "a = 14\n")  # edits #11's file
    before = _git(work, "rev-parse", "origin/deploy")
    r = _run_gated(work, "--pick", "14", "--yes")
    assert r.returncode == 1
    assert "conflict" in r.stderr.lower()
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", "origin/deploy") == before


def test_pick_unknown_pr_is_rejected(tmp_path):
    _, work, _ = _setup_prs(tmp_path)
    r = _run_gated(work, "--pick", "99", "--yes")
    assert r.returncode == 2
    assert "#99" in r.stderr


def test_pick_aborts_on_no(tmp_path):
    _, work, _ = _setup_prs(tmp_path)
    before = _git(work, "rev-parse", "origin/deploy")
    r = _run_gated(work, "--pick", "12", stdin="n\n")
    assert r.returncode == 1
    assert "Holding back" in r.stdout and "#11" in r.stdout
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", "origin/deploy") == before


def test_full_promote_after_picks_syncs_to_main_tree(tmp_path):
    _, work, _ = _setup_prs(tmp_path)
    assert _run_gated(work, "--pick", "12", "--yes").returncode == 0
    _git(work, "fetch", "-q", "origin")
    picked_deploy = _git(work, "rev-parse", "origin/deploy")

    r = _run_gated(work, "--yes")
    assert r.returncode == 0, r.stderr
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", "origin/deploy^{tree}") == _git(
        work, "rev-parse", "origin/main^{tree}"
    )
    assert _run_ok(work, "merge-base", "--is-ancestor", picked_deploy, "origin/deploy")
    assert _run_ok(work, "merge-base", "--is-ancestor", "origin/main", "origin/deploy")

    again = _run_gated(work, "--yes")
    assert again.returncode == 0
    assert "up to date" in again.stdout.lower()
    assert "Nothing pending" in _run_gated(work, "--list").stdout

    # new work on main still promotes cleanly (as a sync, deploy is diverged)
    _merge_pr(work, 15, "fifteen.py", "a = 15\n")
    assert _run_gated(work, "--pick", "15", "--yes").returncode == 0
    assert "fifteen.py" in _deploy_files(work)


def _run_ok(cwd, *args):
    return (
        subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=_scrubbed_env()).returncode
        == 0
    )
