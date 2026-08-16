#!/usr/bin/env python3
"""Demo-GIF staleness guard (advisory).

The README embeds hand-captured UI walkthrough GIFs under ``assets/demo/``.
The UI they depict lives in ``webapp/client/`` and changes often; nothing
else flags when the GIFs no longer match it. This script compares each
README-referenced GIF's last git commit time against the most recent commit
touching ``webapp/client/`` and warns when the client has moved on by more
than a grace threshold (default 21 days).

Deliberately compares GIF age to the CLIENT's last-change time — not to
wall-clock "now" — so an untouched UI never makes a matching GIF look stale.

Exit codes: 0 always in the default advisory mode; non-zero only with
``--strict`` (stale GIFs) or when a README-referenced GIF is missing from
disk (always an error). Full automated recapture is out of scope.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SECONDS_PER_DAY = 86400
DEFAULT_MAX_LAG_DAYS = 21.0

GIF_REF_RE = re.compile(r"assets/demo/[A-Za-z0-9_.-]+\.gif")


@dataclass(frozen=True)
class GifStatus:
    """Staleness verdict for one README-referenced GIF."""

    path: str
    gif_commit_ts: int
    client_commit_ts: int
    lag_days: float
    stale: bool


def compute_lag_days(gif_commit_ts: int, client_commit_ts: int) -> float:
    """Days the client's last change post-dates the GIF's last capture.

    Negative or zero means the GIF is at least as fresh as the client.
    """
    return (client_commit_ts - gif_commit_ts) / SECONDS_PER_DAY


def assess_gif(
    path: str, gif_commit_ts: int, client_commit_ts: int, max_lag_days: float
) -> GifStatus:
    """Pure staleness assessment from injected timestamps (no git, no clock)."""
    lag = compute_lag_days(gif_commit_ts, client_commit_ts)
    return GifStatus(
        path=path,
        gif_commit_ts=gif_commit_ts,
        client_commit_ts=client_commit_ts,
        lag_days=lag,
        stale=lag > max_lag_days,
    )


def parse_readme_gifs(readme_text: str) -> list[str]:
    """Unique assets/demo/*.gif paths referenced by the README, in order."""
    seen: dict[str, None] = {}
    for match in GIF_REF_RE.findall(readme_text):
        seen.setdefault(match)
    return list(seen)


def git_last_commit_ts(repo_root: Path, rel_path: str) -> int | None:
    """UNIX time of the last commit touching ``rel_path``, or None if never."""
    # Scrub GIT_* from the environment: a git hook (e.g. the pre-push gate)
    # exports GIT_DIR etc., which would point this git invocation at the
    # hook's repo instead of ``repo_root``.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ct", "--", rel_path],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout.strip()
    return int(out) if out else None


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def run(repo_root: Path, max_lag_days: float, strict: bool) -> int:
    readme = repo_root / "README.md"
    if not readme.exists():
        print(f"ERROR: {readme} not found", file=sys.stderr)
        return 2

    gif_paths = parse_readme_gifs(readme.read_text(encoding="utf-8"))
    if not gif_paths:
        print("No assets/demo/*.gif references found in README.md — nothing to check.")
        return 0

    missing = [p for p in gif_paths if not (repo_root / p).exists()]
    for p in missing:
        print(f"ERROR: README references {p} but it is missing from disk")

    client_ts = git_last_commit_ts(repo_root, "webapp/client/")
    if client_ts is None:
        print("No commits found for webapp/client/ — cannot assess staleness.")
        return 1 if missing else 0

    statuses: list[GifStatus] = []
    for p in gif_paths:
        if p in missing:
            continue
        gif_ts = git_last_commit_ts(repo_root, p)
        if gif_ts is None:
            # On disk but never committed — treat as unassessable, note it.
            print(f"NOTE: {p} exists on disk but has no git history; skipping")
            continue
        statuses.append(assess_gif(p, gif_ts, client_ts, max_lag_days))

    stale = [s for s in statuses if s.stale]
    if stale:
        print(
            f"\nWARNING: {len(stale)} demo GIF(s) predate the latest "
            f"webapp/client/ change by more than {max_lag_days:g} days:\n"
        )
        header = f"{'gif':<32} {'captured':<12} {'client changed':<15} {'lag (days)':>10}"
        print(header)
        print("-" * len(header))
        for s in stale:
            print(
                f"{s.path:<32} {fmt_date(s.gif_commit_ts):<12} "
                f"{fmt_date(s.client_commit_ts):<15} {s.lag_days:>10.1f}"
            )
        print(
            "\nThe UI these GIFs depict may have changed since capture. "
            "Consider re-recording them (manual capture; see README)."
        )
    else:
        print(
            f"All {len(statuses)} README demo GIFs are within {max_lag_days:g} "
            "days of the latest webapp/client/ change."
        )

    if missing:
        return 1
    if stale and strict:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-lag-days",
        type=float,
        default=DEFAULT_MAX_LAG_DAYS,
        help="grace window before a GIF older than the client counts as stale "
        f"(default {DEFAULT_MAX_LAG_DAYS:g})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when any GIF is stale (default: advisory, exit 0)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: this script's parent's parent)",
    )
    args = parser.parse_args(argv)
    return run(args.repo_root, args.max_lag_days, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
