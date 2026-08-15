"""Regenerate the live repo-vitals dashboard badge for README.md.

Computes a handful of headline numbers straight from git and the tracked
file list — test count, module count, commits since the pinned hackathon
baseline, and a fixed surface count — plus a commits-per-day velocity series,
and bakes them into a sticker-style brand-kit SVG (assets/dashboard.svg). No
README markers are used: every number lives inside the SVG. Run by the same CI
that refreshes assets/hackathon_loc.svg on every push to main; safe to run
locally from the repo root and idempotent (the SVG is only rewritten when its
content changes).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from scripts._brand import (
    BLUE,
    FONT,
    GREEN,
    MUTED,
    ORANGE,
    RED,
    fmt,
    open_svg,
    sparkline,
    stat_tiles,
    sticker_card,
    title_block,
)

# Last commit before the hackathon started (2026-06-21), pinned so the
# commit-count and velocity series can't drift if history is ever touched.
BASELINE_SHA = "e296308a57296a8d2f04497f7fc8872112e8ed49"
# Discord bot, Telegram bot, the Discord Activity webapp, and the standalone
# web app at build.letseffinggo.com.
SURFACES = 4
TEST_DEF_RE = r"^\s*def test_"
SVG_PATH = Path("assets/dashboard.svg")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def count_tests() -> int:
    """Test functions across tracked Python files (one `def test_` per line)."""
    out = git("grep", "-h", "-E", TEST_DEF_RE, "--", "*.py")
    return len(out.splitlines())


def count_modules() -> int:
    """Tracked Python source files."""
    out = git("ls-files", "--", "*.py")
    return len(out.splitlines())


def count_commits() -> int:
    """Commits landed since the pinned hackathon baseline."""
    return int(git("rev-list", "--count", f"{BASELINE_SHA}..HEAD"))


def velocity() -> tuple[str, list[int]]:
    """Commits-per-day since the baseline, as a gap-filled contiguous series.

    Returns the human start-date label (e.g. "June 21") and one count per
    calendar day from the first commit day through the last, inclusive, so a
    quiet stretch mid-sprint reads as a run of zero-height bars.
    """
    out = git("log", "--date=format:%Y-%m-%d", "--pretty=%ad", f"{BASELINE_SHA}..HEAD")
    per_day: dict[str, int] = {}
    for day in out.splitlines():
        per_day[day] = per_day.get(day, 0) + 1
    if not per_day:
        return "", []
    start = date.fromisoformat(min(per_day))
    end = date.fromisoformat(max(per_day))
    series: list[int] = []
    cursor = start
    while cursor <= end:
        series.append(per_day.get(cursor.isoformat(), 0))
        cursor += timedelta(days=1)
    return f"{start:%B} {start.day}", series


def build_svg(
    tests: int,
    modules: int,
    commits: int,
    surfaces: int,
    start_label: str,
    series: list[int],
) -> str:
    """Sticker-style vitals card: a title, four stat tiles, and a velocity chart."""
    w, h = 728, 210
    card_w, card_h = 718, 200
    pad = 24
    area_x, area_w = float(pad), 672.0

    aria_label = (
        f"Live repo vitals: {fmt(tests)} tests, {fmt(modules)} modules, "
        f"{fmt(commits)} commits across {surfaces} surfaces, with a commits-per-day "
        f"sprint velocity chart since {start_label}"
    )

    parts = [open_svg(w, h, aria_label)]
    # sticker: hard offset shadow, then card with paper ring (renders on
    # both GitHub light and dark themes thanks to its own dark fill)
    parts += sticker_card(card_w, card_h)
    # title + subtitle
    parts += title_block(
        pad,
        "Built in a hackathon sprint",
        "live repo vitals · auto-updated on every push to main",
    )

    # Four stat tiles: big brand-colored number over a muted label.
    tiles = [
        (fmt(tests), "tests", BLUE),
        (fmt(modules), "modules", ORANGE),
        (fmt(commits), "commits", RED),
        (str(surfaces), "surfaces", GREEN),
    ]
    parts += stat_tiles(area_x, 72, area_w, tiles)

    # Velocity chart: caption over a baseline with one thin bar per day.
    parts.append(
        f'<text x="{pad}" y="156" font-family="{FONT}" font-size="12" '
        f'fill="{MUTED}">commits / day since {start_label}</text>'
    )
    parts += sparkline(area_x, 192, area_w, series, ORANGE)

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    tests = count_tests()
    modules = count_modules()
    commits = count_commits()
    start_label, series = velocity()

    svg = build_svg(tests, modules, commits, SURFACES, start_label, series)
    changed = not SVG_PATH.exists() or SVG_PATH.read_text() != svg
    if changed:
        SVG_PATH.parent.mkdir(exist_ok=True)
        SVG_PATH.write_text(svg)
    print("updated" if changed else "already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
