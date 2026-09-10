"""Regression guard for the #323 test-env-isolation rule.

A bare ``dotenv.load_dotenv()`` at import time loads the deployed ``.env``
into ``os.environ`` mid-suite despite ``LFG_SKIP_DOTENV=1`` — the gate only
lives in ``lfg_core.envload.load_dotenv_unless_skipped``. Any module a test
can import must go through that helper. This bit issue #477's PR, where four
``scripts/`` modules called ``load_dotenv()`` at import and leaked values
like ``SHARE_CARD_RENDER_ENABLED=1`` into later config reloads.

Allowed exceptions:
  - ``lfg_core/envload.py`` — the gate itself.
  - ``lfg_core/db_path.py`` — inlines the gate (must stay lfg_core-import-free).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SCANNED_DIRS = ("scripts", "lfg_core", "lfg_service", "surfaces", "webapp")

ALLOWED = {
    Path("lfg_core/envload.py"),
    Path("lfg_core/db_path.py"),
}

# Matches importing or referencing dotenv's loader directly.
_BARE_DOTENV = re.compile(
    r"(from\s+dotenv\s+import\b[^\n]*\bload_dotenv\b|\bdotenv\.load_dotenv\b)"
)


def test_no_module_calls_dotenv_load_dotenv_directly():
    offenders = []
    for dirname in SCANNED_DIRS:
        for path in (REPO_ROOT / dirname).rglob("*.py"):
            rel = path.relative_to(REPO_ROOT)
            if rel in ALLOWED:
                continue
            # Test modules may legitimately reference/monkeypatch the symbol.
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            if _BARE_DOTENV.search(path.read_text(encoding="utf-8")):
                offenders.append(str(rel))
    assert not offenders, (
        "These modules use dotenv.load_dotenv directly; call "
        "lfg_core.envload.load_dotenv_unless_skipped() instead (#323): "
        f"{sorted(offenders)}"
    )
