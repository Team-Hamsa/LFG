"""Sync the merged-PR changelog block in docs/HACKATHON.md from GitHub.

The hackathon build log has two parts: the hand-written feature narrative
(never touched here) and a generated **merged changelog** between the
changelog:start/end markers, listing every merged pull request grouped by
month, newest first. This script regenerates that block from the live PR
state (fetched with the `gh` CLI, so a token must be available — GH_TOKEN in
Actions) so the build log can never silently fall behind the repo again.

Run by .github/workflows/build-log-sync.yml whenever a PR merges into main,
on a daily schedule, and on manual dispatch; safe to run locally from the
repo root and idempotent (the file is only rewritten when the generated
block changes). Mirrors scripts/readme_roadmap.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

BUILD_LOG_PATH = Path("docs/HACKATHON.md")

START_MARK = "<!-- changelog:start -->"
END_MARK = "<!-- changelog:end -->"

REPO = "Team-Hamsa/LFG"
PR_URL = f"https://github.com/{REPO}/pull"
# Hackathon sprint start — anything merged earlier is pre-sprint and left out.
SINCE = "2026-06-21"


def fetch_merged_prs() -> list[dict[str, Any]]:
    """Every merged PR (number, title, mergedAt) via gh."""
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--state",
            "merged",
            "--limit",
            "1000",
            "--json",
            "number,title,mergedAt",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    prs = json.loads(proc.stdout)
    if not isinstance(prs, list):
        raise SystemExit(f"unexpected gh output: {proc.stdout[:200]}")
    return prs


def escape_title(title: str) -> str:
    """Make an externally authored PR title safe inside generated Markdown.

    Collapses whitespace and escapes characters that could close a link
    label or open Markdown/HTML constructs.
    """
    flat = " ".join(title.split())
    return re.sub(r"([\\\[\]<>`])", r"\\\1", flat)


def month_label(iso_date: str) -> str:
    """'2026-08-23' -> 'August 2026'."""
    year, month = iso_date[:4], int(iso_date[5:7])
    names = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    return f"{names[month - 1]} {year}"


def bullet(pr: dict[str, Any]) -> str:
    number = int(pr["number"])
    day = str(pr["mergedAt"])[:10]
    title = escape_title(str(pr["title"]).strip())
    return f"- {day} · [#{number}]({PR_URL}/{number}) {title}"


def render(prs: list[dict[str, Any]], since: str = SINCE) -> list[str]:
    """The generated block body: per-month headings, newest month and PR first."""
    merged = [p for p in prs if p.get("mergedAt") and str(p["mergedAt"])[:10] >= since]
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pr in merged:
        by_month[str(pr["mergedAt"])[:7]].append(pr)

    total = len(merged)
    lines = [
        f"_{total} pull requests merged since {since}. "
        "Regenerated automatically on every merge — see `scripts/build_log_sync.py`._"
    ]
    for month in sorted(by_month, reverse=True):
        items = sorted(
            by_month[month],
            key=lambda p: (str(p["mergedAt"]), int(p["number"])),
            reverse=True,
        )
        lines += ["", f"### {month_label(month)} — {len(items)} merged", ""]
        lines += [bullet(p) for p in items]
    return lines


def replace_block(doc: str, block_lines: list[str]) -> str:
    """Swap the marker-delimited region for the freshly generated body."""
    pattern = re.compile(re.escape(START_MARK) + r".*?" + re.escape(END_MARK), flags=re.DOTALL)
    if not pattern.search(doc):
        raise SystemExit(f"changelog markers not found in {BUILD_LOG_PATH}")
    replacement = "\n".join([START_MARK, *block_lines, END_MARK])
    return pattern.sub(lambda _match: replacement, doc, count=1)


def main() -> int:
    doc = BUILD_LOG_PATH.read_text()
    updated = replace_block(doc, render(fetch_merged_prs()))
    if updated != doc:
        BUILD_LOG_PATH.write_text(updated)
        print("updated")
    else:
        print("already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
