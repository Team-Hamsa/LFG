"""Agent users §1: a memo platform derived from a surface must go through
memos.platform_for, so an agent session is labelled platform=agent everywhere."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_code_outside_memos_calls_platform_for_surface():
    offenders = [
        f"{p.relative_to(ROOT)}:{i}"
        for base in ("lfg_core", "lfg_service", "webapp", "scripts")
        for p in (ROOT / base).rglob("*.py")
        if p.name != "memos.py"
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if re.search(r"\bplatform_for_surface\(", line)
    ]
    assert offenders == []
