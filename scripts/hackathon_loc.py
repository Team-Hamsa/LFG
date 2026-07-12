"""Update the hackathon lines-of-code stats in README.md.

Compares the pinned pre-hackathon baseline commit against HEAD, counting
only hand-written code — Python, JS, CSS, HTML — and excluding docs, data
files (CSV/JSON manifests), dependency/config files, and the legacy/backup
trees. Writes a brand-kit SVG bar (assets/hackathon_loc.svg) plus a stats
table between the README markers. Run by
.github/workflows/hackathon-loc.yml on every push to main; safe to run
locally from the repo root.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Last commit before the hackathon started (2026-06-21), pinned so the
# numbers can't drift if dates or history are ever touched.
BASELINE_SHA = "e296308a57296a8d2f04497f7fc8872112e8ed49"
CODE_PATHSPECS = [
    "*.py",
    "*.js",
    "*.css",
    "*.html",
    ":(exclude)legacy/*",
    ":(exclude)backup/*",
]
START_MARK = "<!-- hackathon-loc:start -->"
END_MARK = "<!-- hackathon-loc:end -->"
SVG_PATH = Path("assets/hackathon_loc.svg")

# LFG brand kit (webapp/client/style.css)
ORANGE = "#D89030"  # baseline code
RED = "#D84830"  # tests
BLUE = "#4890C0"  # application code
INK = "#0A0A0A"
PAPER = "#FFFFFF"
SURFACE = "#181818"
TEXT = "#F5F4F1"
MUTED = "#9C9A94"
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def baseline_loc() -> int:
    """Total code lines in the tree at the baseline commit."""
    out = git("grep", "-c", "", BASELINE_SHA, "--", *CODE_PATHSPECS)
    return sum(int(line.rsplit(":", 1)[1]) for line in out.splitlines())


def numstat() -> list[tuple[int, int, str]]:
    out = git("diff", "--numstat", f"{BASELINE_SHA}..HEAD", "--", *CODE_PATHSPECS)
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        added, deleted, path = line.split("\t", 2)
        if added == "-":  # binary
            continue
        rows.append((int(added), int(deleted), path))
    return rows


def is_test(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in path


def fmt(n: int) -> str:
    return f"{n:,}"


def build_svg(base: int, app: int, tests: int) -> str:
    """Sticker-style stacked bar: baseline vs hackathon app code vs tests."""
    total = base + app + tests
    w, h = 720, 150
    pad = 24
    bar_x, bar_y, bar_w, bar_h, gap = pad, 76, w - 2 * pad, 16, 2
    segs = [
        ("Baseline", base, ORANGE),
        ("App code", app, BLUE),
        ("Tests", tests, RED),
    ]
    # Segment widths, minus the two 2px surface gaps
    avail = bar_w - gap * (len(segs) - 1)
    widths = [avail * v / total for _, v, _ in segs]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w + 8}" height="{h + 8}" '
        f'viewBox="0 0 {w + 8} {h + 8}" role="img" '
        f'aria-label="Code growth: {fmt(base)} baseline lines, {fmt(app)} application '
        f'lines and {fmt(tests)} test lines added during the hackathon">',
        # sticker: hard offset shadow, then card with paper ring
        f'<rect x="8" y="8" width="{w - 3}" height="{h - 3}" rx="18" fill="{INK}"/>',
        f'<rect x="1.5" y="1.5" width="{w - 3}" height="{h - 3}" rx="18" '
        f'fill="{SURFACE}" stroke="{PAPER}" stroke-width="3"/>',
        # title + subtitle
        f'<text x="{pad}" y="38" font-family="{FONT}" font-size="19" '
        f'font-weight="700" fill="{TEXT}">Code written during the hackathon</text>',
        f'<text x="{pad}" y="58" font-family="{FONT}" font-size="13" fill="{MUTED}">'
        f"since June 21 · +{fmt(app + tests)} net lines · "
        f"codebase now {fmt(total)} lines</text>",
        # bar, clipped to rounded pill
        f'<clipPath id="pill"><rect x="{bar_x}" y="{bar_y}" width="{bar_w}" '
        f'height="{bar_h}" rx="{bar_h / 2}"/></clipPath>',
        '<g clip-path="url(#pill)">',
    ]
    x = float(bar_x)
    for (_, _, color), seg_w in zip(segs, widths, strict=True):
        parts.append(
            f'<rect x="{x:.1f}" y="{bar_y}" width="{seg_w:.1f}" height="{bar_h}" fill="{color}"/>'
        )
        x += seg_w + gap
    parts.append("</g>")
    # legend: dot + name + value per segment
    lx = float(pad)
    ly = bar_y + bar_h + 28
    for name, value, color in segs:
        pct = 100 * value / total
        label = f"{name}  {fmt(value)} ({pct:.0f}%)"
        parts.append(f'<circle cx="{lx + 6:.1f}" cy="{ly - 4}" r="6" fill="{color}"/>')
        parts.append(
            f'<text x="{lx + 20:.1f}" y="{ly}" font-family="{FONT}" '
            f'font-size="13" fill="{TEXT}">{label}</text>'
        )
        lx += 20 + 8.2 * len(label) + 28  # advance past dot + approx text width
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def build_block(base: int) -> str:
    date = git("log", "-1", "--format=%ad", "--date=format:%Y-%m-%d", BASELINE_SHA)
    return "\n".join(
        [
            START_MARK,
            '<div align="center">',
            '<img src="assets/hackathon_loc.svg" alt="Hackathon code growth bar" width="728">',
            "</div>",
            "",
            f"> **Every line hand-written since the June 21 hackathon baseline** "
            f"(`{BASELINE_SHA[:7]}`, {date}, {fmt(base)} lines) — measured by "
            f"`git diff --numstat` over `.py`/`.js`/`.css`/`.html`, excluding docs, "
            f"data files (CSV/JSON manifests), dependency lists, and the "
            f"legacy/backup trees. Regenerated on every push to `main`.",
            END_MARK,
        ]
    )


def main() -> int:
    readme = Path("README.md")
    text = readme.read_text()
    if START_MARK not in text or END_MARK not in text:
        print("README markers missing", file=sys.stderr)
        return 1

    app_a = app_d = test_a = test_d = 0
    for added, deleted, path in numstat():
        if is_test(path):
            test_a, test_d = test_a + added, test_d + deleted
        else:
            app_a, app_d = app_a + added, app_d + deleted
    base = baseline_loc()

    svg = build_svg(base, app_a - app_d, test_a - test_d)
    changed = not SVG_PATH.exists() or SVG_PATH.read_text() != svg
    if changed:
        SVG_PATH.parent.mkdir(exist_ok=True)
        SVG_PATH.write_text(svg)

    block = build_block(base)
    new_text = re.sub(
        re.escape(START_MARK) + r".*?" + re.escape(END_MARK),
        lambda _: block,
        text,
        count=1,
        flags=re.DOTALL,
    )
    if new_text != text:
        readme.write_text(new_text)
        changed = True
    print("updated" if changed else "already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
