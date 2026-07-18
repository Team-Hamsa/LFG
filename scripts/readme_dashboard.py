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

# Last commit before the hackathon started (2026-06-21), pinned so the
# commit-count and velocity series can't drift if history is ever touched.
BASELINE_SHA = "e296308a57296a8d2f04497f7fc8872112e8ed49"
# Discord bot, Telegram bot, the Discord Activity webapp, and the standalone
# web app at build.letseffinggo.com.
SURFACES = 4
TEST_DEF_RE = r"^\s*def test_"
SVG_PATH = Path("assets/dashboard.svg")

# LFG brand kit (webapp/client/style.css)
INK = "#0A0A0A"
SURFACE = "#181818"
SURFACE_LIGHT = "#202020"  # subtle tile fill, one step up from the card
LINE = "#2C2C2C"
PAPER = "#FFFFFF"
TEXT = "#F5F4F1"
MUTED = "#9C9A94"
ORANGE = "#D89030"
RED = "#D84830"
BLUE = "#4890C0"
YELLOW = "#F0D848"
GREEN = "#3DA35D"
PURPLE = "#601878"
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


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


def fmt(n: int) -> str:
    return f"{n:,}"


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

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" '
        f'aria-label="Live repo vitals: {fmt(tests)} tests, {fmt(modules)} modules, '
        f"{fmt(commits)} commits across {surfaces} surfaces, with a commits-per-day "
        f'sprint velocity chart since {start_label}">',
        # sticker: hard offset shadow, then card with paper ring (renders on
        # both GitHub light and dark themes thanks to its own dark fill)
        f'<rect x="8" y="8" width="{card_w}" height="{card_h}" rx="18" fill="{INK}"/>',
        f'<rect x="2" y="2" width="{card_w}" height="{card_h}" rx="18" '
        f'fill="{SURFACE}" stroke="{PAPER}" stroke-width="3"/>',
        # title + subtitle
        f'<text x="{pad}" y="34" font-family="{FONT}" font-size="19" '
        f'font-weight="700" fill="{TEXT}">Built in a hackathon sprint</text>',
        f'<text x="{pad}" y="56" font-family="{FONT}" font-size="13" fill="{MUTED}">'
        f"live repo vitals · auto-updated on every push to main</text>",
    ]

    # Four stat tiles: big brand-colored number over a muted label.
    tiles = [
        (fmt(tests), "tests", BLUE),
        (fmt(modules), "modules", ORANGE),
        (fmt(commits), "commits", RED),
        (str(surfaces), "surfaces", GREEN),
    ]
    tile_y, tile_h, gap = 72, 60, 16
    tile_w = (area_w - gap * (len(tiles) - 1)) / len(tiles)
    for i, (value, label, color) in enumerate(tiles):
        tx = area_x + i * (tile_w + gap)
        parts.append(
            f'<rect x="{tx:.1f}" y="{tile_y}" width="{tile_w:.1f}" height="{tile_h}" '
            f'rx="12" fill="{SURFACE_LIGHT}" stroke="{LINE}" stroke-width="1"/>'
        )
        parts.append(
            f'<rect x="{tx + 16:.1f}" y="{tile_y + 12}" width="30" height="5" '
            f'rx="2.5" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{tx + 16:.1f}" y="{tile_y + 42}" font-family="{FONT}" '
            f'font-size="26" font-weight="800" fill="{color}">{value}</text>'
        )
        parts.append(
            f'<text x="{tx + 16:.1f}" y="{tile_y + 55}" font-family="{FONT}" '
            f'font-size="11" fill="{MUTED}">{label}</text>'
        )

    # Velocity chart: caption over a baseline with one thin bar per day.
    parts.append(
        f'<text x="{pad}" y="156" font-family="{FONT}" font-size="12" '
        f'fill="{MUTED}">commits / day since {start_label}</text>'
    )
    base_y, max_bar_h = 192, 26
    parts.append(
        f'<line x1="{area_x:.1f}" y1="{base_y}" x2="{area_x + area_w:.1f}" '
        f'y2="{base_y}" stroke="{LINE}" stroke-width="1"/>'
    )
    if series:
        peak = max(max(series), 1)
        slot = area_w / len(series)
        bar_w = slot * 0.55
        for i, count in enumerate(series):
            if count <= 0:
                continue
            bar_h = max(max_bar_h * count / peak, 2.0)
            bx = area_x + i * slot + (slot - bar_w) / 2
            parts.append(
                f'<rect x="{bx:.1f}" y="{base_y - bar_h:.1f}" width="{bar_w:.1f}" '
                f'height="{bar_h:.1f}" rx="2" fill="{ORANGE}"/>'
            )

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
