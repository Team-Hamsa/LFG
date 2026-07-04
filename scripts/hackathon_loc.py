"""Update the hackathon lines-of-code stats block in README.md.

Compares the last pre-hackathon commit (before 2026-06-21) against HEAD,
counting only hand-written code — Python, JS, CSS, HTML — and excluding
docs, data files (CSV/JSON manifests), dependency/config files, and the
legacy/backup trees. Run by .github/workflows/hackathon-loc.yml on every
push to main; safe to run locally from the repo root.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BASELINE_BEFORE = "2026-06-21T00:00:00"
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


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def numstat(baseline: str) -> list[tuple[int, int, str]]:
    out = git("diff", "--numstat", f"{baseline}..HEAD", "--", *CODE_PATHSPECS)
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


def build_block(baseline: str) -> str:
    rows = numstat(baseline)
    cats = {"Application code": [0, 0], "Tests": [0, 0]}
    for added, deleted, path in rows:
        key = "Tests" if is_test(path) else "Application code"
        cats[key][0] += added
        cats[key][1] += deleted
    total_a = sum(a for a, _ in cats.values())
    total_d = sum(d for _, d in cats.values())
    date = git("log", "-1", "--format=%ad", "--date=format:%Y-%m-%d", baseline)
    lines = [
        START_MARK,
        f"*Hand-written code merged since the hackathon baseline "
        f"(`{baseline[:7]}`, {date} — last commit before June 21), measured by "
        f"`git diff --numstat`. Counts `.py`/`.js`/`.css`/`.html` only; docs, "
        f"markdown, data files (CSV/JSON manifests), dependency lists, and the "
        f"legacy/backup trees are excluded. Updated automatically on every push "
        f"to `main`.*",
        "",
        "| Category | Lines added | Lines removed | Net |",
        "|---|---:|---:|---:|",
    ]
    for name, (a, d) in cats.items():
        lines.append(f"| {name} | +{fmt(a)} | −{fmt(d)} | {fmt(a - d)} |")
    lines.append(
        f"| **Total** | **+{fmt(total_a)}** | **−{fmt(total_d)}** "
        f"| **{fmt(total_a - total_d)}** |"
    )
    lines.append(END_MARK)
    return "\n".join(lines)


def main() -> int:
    baseline = git("rev-list", "-1", f"--before={BASELINE_BEFORE}", "HEAD")
    if not baseline:
        print("no baseline commit found", file=sys.stderr)
        return 1
    readme = Path("README.md")
    text = readme.read_text()
    if START_MARK not in text or END_MARK not in text:
        print("README markers missing", file=sys.stderr)
        return 1
    block = build_block(baseline)
    new_text = re.sub(
        re.escape(START_MARK) + r".*?" + re.escape(END_MARK),
        lambda _: block,
        text,
        count=1,
        flags=re.DOTALL,
    )
    if new_text != text:
        readme.write_text(new_text)
        print("README.md updated")
    else:
        print("README.md already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
