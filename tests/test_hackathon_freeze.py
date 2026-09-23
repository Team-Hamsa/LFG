"""The Make Waves hackathon stats and graphics are frozen as submitted.

Make Waves closed 2026-09-21. Every hackathon stat the repo shows is pinned
to what it showed at submission: commit 48656477 (2026-09-21 08:17 UTC), the
last refresh after the pitch deck landed. That covers the LoC bar, the
repo-vitals dashboard, the SourceTag badge and its metrics snapshot, the
tests / tagged-txs badges, and the merged-PR changelog in the build log.

Nothing regenerates them any more: the LoC / dashboard / SourceTag steps left
the README sync workflow, build-log-sync.yml was deleted, and the nightly
lfg-sourcetag push was unregistered along with sourcetag_metrics' `--push`.
These tests pin the exact bytes, so anything that regenerates one by accident
(a generator run from the repo root, a revived workflow or pm2 entry) fails
the gate instead of quietly rewriting what the judges saw.

To change a frozen artifact on purpose, update its pin here in the same
commit and say why in the message.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
GUARD = "tests/test_hackathon_freeze.py"

FROZEN_FILES = {
    "assets/hackathon_loc.svg": "7fa02b402f652b4129aabdfabf3b7a0dd89cb6022ac5b763275c5c76e63d1a3a",
    "assets/dashboard.svg": "6dd6b896f8a3abfa35513b3bfb4f01a14f960ee161441748d1dedf56347e41f7",
    "assets/sourcetag.svg": "6bb28606c1453959660ca760ddd01b0f8687171a088533778510d8794ebf3e50",
    "metrics/sourcetag.json": "0b121a1dfda4cacc50dfe08cddb3e9057b4621c20a80a5eceb29eaa77c994559",
}

# (file, start marker, end marker) -> sha256 of the block, markers included.
FROZEN_BLOCKS = {
    (
        "README.md",
        "<!-- hackathon-loc:start -->",
        "<!-- hackathon-loc:end -->",
    ): "f9d3f20c5605a550812e2dd0dd6b1072617465400616e59301b494c5d0c6c2aa",
    (
        "docs/HACKATHON.md",
        "<!-- changelog:start -->",
        "<!-- changelog:end -->",
    ): "e3eb27f13ec946ee854b9c88f33e315abebfd50c8c02ef9b5dd81ab9b7d085f8",
}

FROZEN_BADGES = [
    '<img src="https://img.shields.io/badge/tests-5%2C397-2ea043?style=flat-square"'
    ' alt="5,397 tests">',
    '<img src="https://img.shields.io/badge/tagged_txs-12%2C141-3E8DE3?style=flat-square"'
    ' alt="12,141 SourceTag-tagged XRPL transactions">',
]

# Generators whose output is frozen. None may run from CI or pm2 again.
RETIRED_GENERATORS = [
    "hackathon_loc",
    "readme_dashboard",
    "render_sourcetag_svg",
    "build_log_sync",
    "sourcetag_metrics",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _block(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"expected exactly one {start}"
    i = text.index(start)
    j = text.index(end, i) + len(end)
    return text[i:j]


@pytest.mark.parametrize("path", sorted(FROZEN_FILES))
def test_frozen_file_is_byte_identical(path: str) -> None:
    assert _sha256((ROOT / path).read_bytes()) == FROZEN_FILES[path], (
        f"{path} changed. Hackathon stats are frozen as submitted; restore it with "
        f"`git restore --source=48656477 -- {path}`."
    )


@pytest.mark.parametrize("key", sorted(FROZEN_BLOCKS), ids=lambda k: f"{k[0]}:{k[1]}")
def test_frozen_block_is_unchanged(key: tuple[str, str, str]) -> None:
    path, start, end = key
    block = _block((ROOT / path).read_text(), start, end)
    assert _sha256(block.encode()) == FROZEN_BLOCKS[key], (
        f"the {start} block in {path} changed; it is frozen as submitted"
    )


@pytest.mark.parametrize("line", FROZEN_BADGES)
def test_frozen_badge_is_in_readme(line: str) -> None:
    lines = (ROOT / "README.md").read_text().splitlines()
    assert lines.count(line) == 1


def _workflow_files(directory: Path) -> list[Path]:
    # GitHub runs both extensions.
    return sorted(p for p in directory.iterdir() if p.suffix in (".yml", ".yaml"))


def test_no_workflow_or_pm2_app_runs_a_retired_generator() -> None:
    automation = _workflow_files(WORKFLOWS)
    automation += sorted(ROOT.glob("ecosystem*.config.js"))
    assert automation, "found no workflows or ecosystem files to check"
    offenders = [
        f"{path.relative_to(ROOT)}: {name}"
        for path in automation
        for name in RETIRED_GENERATORS
        if name in path.read_text()
    ]
    assert offenders == []


def _workflow(path: Path) -> dict[Any, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict), f"{path.name} is not a workflow mapping"
    return loaded


def _effective(unit: dict[str, Any]) -> bool:
    """False for a job or step that is switched off or whose failure is ignored."""
    return str(unit.get("if", True)).strip().lower() != "false" and unit.get(
        "continue-on-error"
    ) not in (True, "true")


def _commands(run: str) -> list[list[str]]:
    """Each shell command in a `run` block, as tokens (comments dropped)."""
    commands = []
    for line in run.splitlines():
        for part in re.split(r"&&|\|\||;", line.split("#", 1)[0]):
            try:
                tokens = shlex.split(part)
            except ValueError:
                tokens = part.split()
            if tokens:
                commands.append(tokens)
    return commands


def _runs_guard(step: dict[str, Any]) -> bool:
    """True only for an effective step that actually invokes pytest on the guard."""
    if not _effective(step):
        return False
    for tokens in _commands(str(step.get("run") or "")):
        invokes_pytest = tokens[0] == "pytest" or (
            tokens[0] in ("python", "python3") and tokens[1:3] == ["-m", "pytest"]
        )
        if invokes_pytest and GUARD in tokens:
            return True
    return False


def _writes(step: dict[str, Any]) -> bool:
    run = str(step.get("run") or "")
    return "git commit" in run or "git push" in run


def unguarded_commits(workflow: dict[Any, Any]) -> list[str]:
    """Jobs that commit or push before an earlier step has run the freeze guard.

    Reads the parsed steps, so a comment or an echo that merely names the guard
    does not count. The guard must be its own earlier step: a step that pushes
    first and tests afterwards has already published by the time it tests.
    """
    bad = []
    for name, job in (workflow.get("jobs") or {}).items():
        guarded = False
        for step in job.get("steps") or []:
            if _writes(step) and not guarded:
                bad.append(str(name))
                break
            guarded = guarded or _runs_guard(step)
    return bad


def guards_every_push_to_main(workflow: dict[Any, Any]) -> bool:
    """True if the workflow runs the guard on every push to main, unfiltered."""
    # YAML 1.1 reads a bare `on:` key as boolean True.
    triggers = workflow.get("on", workflow.get(True))
    push = triggers.get("push") if isinstance(triggers, dict) else None
    if not isinstance(push, dict) or push.get("branches") != ["main"]:
        return False
    if "paths" in push or "paths-ignore" in push:
        return False
    return any(
        _runs_guard(step)
        for job in (workflow.get("jobs") or {}).values()
        if _effective(job)
        for step in job.get("steps") or []
    )


def test_every_workflow_that_commits_runs_the_guard_first() -> None:
    # A commit pushed with GITHUB_TOKEN triggers no other workflow, so
    # hackathon-freeze.yml never sees a bot commit: each writer must run the
    # guard itself before it commits.
    workflows = {p.name: _workflow(p) for p in _workflow_files(WORKFLOWS)}
    writers = [
        name
        for name, wf in workflows.items()
        if any(
            _writes(step)
            for job in (wf.get("jobs") or {}).values()
            for step in job.get("steps") or []
        )
    ]
    assert writers, "found no workflow that commits"
    offenders = {name: unguarded_commits(workflows[name]) for name in writers}
    assert {name: jobs for name, jobs in offenders.items() if jobs} == {}


def test_a_guard_workflow_runs_on_every_push_to_main() -> None:
    # ci.yml skips README.md and assets/** on push, so something unfiltered
    # has to catch a web edit to a frozen file.
    assert guards_every_push_to_main(_workflow(WORKFLOWS / "hackathon-freeze.yml"))


_GUARD_STEP = {"run": f"python -m pytest -q {GUARD}"}
_COMMIT_STEP = {"run": "git commit -m x && git push"}


@pytest.mark.parametrize(
    ("steps", "flagged"),
    [
        ([_GUARD_STEP, _COMMIT_STEP], False),
        ([_COMMIT_STEP, _GUARD_STEP], True),
        ([{"name": f"see {GUARD}", "run": "echo hi"}, _COMMIT_STEP], True),
        ([_COMMIT_STEP], True),
        ([{"run": f"echo python -m pytest {GUARD}"}, _COMMIT_STEP], True),
        ([{"run": f"git commit -m x && git push && python -m pytest {GUARD}"}], True),
        ([{**_GUARD_STEP, "if": False}, _COMMIT_STEP], True),
        ([{**_GUARD_STEP, "if": "false"}, _COMMIT_STEP], True),
        ([{**_GUARD_STEP, "continue-on-error": True}, _COMMIT_STEP], True),
        ([_GUARD_STEP, {"run": "git push"}], False),
        ([{"run": "git push"}], True),
    ],
    ids=[
        "guard-first",
        "guard-after-commit",
        "name-only-mention",
        "no-guard",
        "echoed-command",
        "same-step-guard-last",
        "guard-if-false",
        "guard-if-false-string",
        "guard-continue-on-error",
        "push-after-guard",
        "push-without-guard",
    ],
)
def test_unguarded_commits_reads_steps_not_text(steps: list[Any], flagged: bool) -> None:
    workflow = {"jobs": {"sync": {"steps": steps}}}
    assert unguarded_commits(workflow) == (["sync"] if flagged else [])


@pytest.mark.parametrize(
    ("on", "job", "ok"),
    [
        ({"push": {"branches": ["main"]}}, {}, True),
        ({"pull_request": {"branches": ["main"]}}, {}, False),
        ({"push": {"branches": ["main"], "paths-ignore": ["README.md"]}}, {}, False),
        ({"push": {"branches": ["main"], "paths": ["tests/**"]}}, {}, False),
        ({"push": {"branches": ["deploy"]}}, {}, False),
        ({"push": {"branches": ["main"]}}, {"if": False}, False),
        ({"push": {"branches": ["main"]}}, {"continue-on-error": True}, False),
    ],
    ids=[
        "push-main",
        "pr-only",
        "paths-ignore",
        "paths",
        "other-branch",
        "job-if-false",
        "job-continue-on-error",
    ],
)
def test_guards_every_push_to_main_requires_an_unfiltered_push(
    on: Any, job: dict[str, Any], ok: bool
) -> None:
    workflow = {True: on, "jobs": {"guard": {**job, "steps": [_GUARD_STEP]}}}
    assert guards_every_push_to_main(workflow) is ok


def test_workflow_scan_covers_both_extensions(tmp_path: Path) -> None:
    (tmp_path / "a.yml").write_text("x: 1\n")
    (tmp_path / "b.yaml").write_text("x: 1\n")
    (tmp_path / "notes.md").write_text("")
    assert [p.name for p in _workflow_files(tmp_path)] == ["a.yml", "b.yaml"]
