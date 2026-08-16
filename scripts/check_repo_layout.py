#!/usr/bin/env python3
"""Verify the README "Repository layout" tree against the working tree.

VERIFY mode only — the tree in README.md is hand-curated (the comments are the
point) and is never regenerated. This checker fails when:

  (a) a path named in the tree no longer exists on disk, or
  (b) a top-level ``lfg_core/*_flow.py`` module or ``surfaces/<pkg>/`` package
      directory exists on disk but is missing from the tree
      (``_``-prefixed and ``__pycache__`` entries are ignored).

Exit codes: 0 clean, 1 drift found, 2 README block not parseable.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SUMMARY = "<summary><b>Repository layout</b></summary>"
# Box-drawing / tree prefix characters preceding an entry name.
_TREE_PREFIX = re.compile(r"^[\s│├└─]*")


def extract_tree_block(readme_text: str) -> str:
    """Return the fenced tree block under the Repository layout summary."""
    idx = readme_text.find(_SUMMARY)
    if idx == -1:
        raise ValueError(f"README marker not found: {_SUMMARY}")
    rest = readme_text[idx:]
    m = re.search(r"```\n(.*?)```", rest, flags=re.DOTALL)
    if not m:
        raise ValueError("no fenced code block after the Repository layout summary")
    return m.group(1)


def parse_paths(tree_block: str) -> list[str]:
    """Extract every repo-relative path named in the tree block."""
    paths: list[str] = []
    # Stack of (indent_column, dirname) for nesting resolution.
    stack: list[tuple[int, str]] = []
    for raw_line in tree_block.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = _TREE_PREFIX.sub("", line)
        if not stripped:
            continue
        indent = len(line) - len(stripped)
        if not paths and not stack and stripped.rstrip("/").endswith("LFG"):
            # Root label line ("LFG/") — anchors the tree, names no path.
            stack = [(indent, "")]
            continue
        while stack and indent <= stack[-1][0] and stack[-1][1]:
            stack.pop()
        parent = stack[-1][1] if stack else ""
        # "_client/, _shared/" comma form: several siblings on one line.
        for name in (part.strip() for part in stripped.split(",")):
            if not name:
                continue
            rel = f"{parent}{name}"
            paths.append(rel)
        # Only a single (non-comma) directory entry can nest children.
        if "," not in stripped and stripped.endswith("/"):
            stack.append((indent, f"{parent}{stripped}"))
    return paths


def missing_on_disk(paths: list[str], root: Path) -> list[str]:
    missing = []
    for rel in paths:
        p = root / rel.rstrip("/")
        if rel.endswith("/"):
            if not p.is_dir():
                missing.append(rel)
        elif not p.exists():
            missing.append(rel)
    return missing


def expected_extras(root: Path) -> list[str]:
    """Paths that must be listed: lfg_core/*_flow.py + surfaces/<pkg>/ dirs."""
    extras: list[str] = []
    for f in sorted((root / "lfg_core").glob("*_flow.py")):
        extras.append(f"lfg_core/{f.name}")
    surfaces = root / "surfaces"
    if surfaces.is_dir():
        for d in sorted(surfaces.iterdir()):
            if d.is_dir() and not d.name.startswith("_") and d.name != "__pycache__":
                extras.append(f"surfaces/{d.name}/")
    return extras


def check(readme_path: Path, root: Path) -> int:
    try:
        block = extract_tree_block(readme_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"check_repo_layout: cannot parse README tree block: {exc}", file=sys.stderr)
        return 2
    listed = parse_paths(block)
    listed_set = {p.rstrip("/") for p in listed}

    problems = False
    gone = missing_on_disk(listed, root)
    if gone:
        problems = True
        print("README 'Repository layout' names paths that no longer exist:", file=sys.stderr)
        for rel in gone:
            print(f"  - {rel}", file=sys.stderr)
        print(
            "  → remove or rename these entries in README.md (keep the comments).", file=sys.stderr
        )

    unlisted = [rel for rel in expected_extras(root) if rel.rstrip("/") not in listed_set]
    if unlisted:
        problems = True
        print("On-disk modules missing from the README 'Repository layout' tree:", file=sys.stderr)
        for rel in unlisted:
            print(f"  - {rel}", file=sys.stderr)
        print(
            "  → add these to the tree in README.md with a short hand-written comment.",
            file=sys.stderr,
        )

    if problems:
        return 1
    print(f"check_repo_layout: OK ({len(listed)} listed paths verified)")
    return 0


def main() -> int:
    return check(REPO_ROOT / "README.md", REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
