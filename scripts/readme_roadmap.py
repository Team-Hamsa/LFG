"""Sync the README roadmap block from `roadmap`-labelled GitHub issues.

Rewrites the region between the roadmap:start/end markers in README.md from
the live state of every issue carrying the `roadmap` label (fetched with the
`gh` CLI, so a token must be available — GH_TOKEN in Actions):

- open issues render as unchecked `- [ ]` bullets (ascending issue number)
- closed issues move themselves into a "Recently completed" list (most
  recently closed first, capped), so a bullet can never claim work is
  outstanding after its issue closes

Hand-written bullets that aren't issue-backed (e.g. pure ops steps) live
OUTSIDE the markers and are never touched. Run by
.github/workflows/roadmap-sync.yml on a daily schedule and on issue
close/reopen/label events; safe to run locally from the repo root and
idempotent (README is only rewritten when the generated block changes).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

README_PATH = Path("README.md")

START_MARK = "<!-- roadmap:start -->"
END_MARK = "<!-- roadmap:end -->"

REPO = "Team-Hamsa/LFG"
LABEL = "roadmap"
RECENT_COMPLETED_CAP = 10


def fetch_issues() -> list[dict[str, Any]]:
    """Every issue carrying the roadmap label, open and closed, via gh."""
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            REPO,
            "--label",
            LABEL,
            "--state",
            "all",
            "--limit",
            "200",
            "--json",
            "number,title,state,closedAt",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    issues = json.loads(proc.stdout)
    if not isinstance(issues, list):
        raise SystemExit(f"unexpected gh output: {proc.stdout[:200]}")
    return issues


def bullet(issue: dict[str, Any], checked: bool) -> str:
    number = issue["number"]
    title = str(issue["title"]).strip()
    box = "x" if checked else " "
    line = f"- [{box}] [#{number} — {title}](../../issues/{number})"
    if checked and issue.get("closedAt"):
        line += f" (closed {str(issue['closedAt'])[:10]})"
    return line


def render(issues: list[dict[str, Any]]) -> list[str]:
    """The generated block body: open bullets, then recently-closed ones."""
    open_issues = sorted(
        (i for i in issues if i["state"] == "OPEN"), key=lambda i: int(i["number"])
    )
    closed_issues = sorted(
        (i for i in issues if i["state"] != "OPEN"),
        key=lambda i: str(i.get("closedAt") or ""),
        reverse=True,
    )[:RECENT_COMPLETED_CAP]

    lines = [bullet(i, checked=False) for i in open_issues]
    if closed_issues:
        lines += [
            "",
            "**Recently completed** (moved here automatically when a roadmap issue closes)",
            "",
        ]
        lines += [bullet(i, checked=True) for i in closed_issues]
    return lines


def replace_block(readme: str, block_lines: list[str]) -> str:
    """Swap the marker-delimited region for the freshly generated bullets."""
    pattern = re.compile(re.escape(START_MARK) + r".*?" + re.escape(END_MARK), flags=re.DOTALL)
    if not pattern.search(readme):
        raise SystemExit("roadmap markers not found in README.md")
    replacement = "\n".join([START_MARK, *block_lines, END_MARK])
    return pattern.sub(lambda _match: replacement, readme, count=1)


def main() -> int:
    readme = README_PATH.read_text()
    updated = replace_block(readme, render(fetch_issues()))
    if updated != readme:
        README_PATH.write_text(updated)
        print("updated")
    else:
        print("already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
