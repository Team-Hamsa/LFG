"""Tests for scripts/check_repo_layout.py (README layout drift guard)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_repo_layout as crl  # noqa: E402

SYNTHETIC_README = """\
<details>
<summary><b>Repository layout</b></summary>

```
LFG/
├── main.py                 # launch shim
├── lfg_core/               # domain library
│   ├── config.py           # env config
│   └── mint_flow.py        # mint state machine
├── surfaces/
│   ├── discord_bot/        # Discord bot
│   └── _client/, _shared/  # plumbing
└── docs/                   # docs
```

</details>
"""


def test_parse_synthetic_tree() -> None:
    block = crl.extract_tree_block(SYNTHETIC_README)
    paths = crl.parse_paths(block)
    assert paths == [
        "main.py",
        "lfg_core/",
        "lfg_core/config.py",
        "lfg_core/mint_flow.py",
        "surfaces/",
        "surfaces/discord_bot/",
        "surfaces/_client/",
        "surfaces/_shared/",
        "docs/",
    ]


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "lfg_core").mkdir()
    (tmp_path / "lfg_core" / "config.py").touch()
    (tmp_path / "lfg_core" / "mint_flow.py").touch()
    (tmp_path / "surfaces" / "discord_bot").mkdir(parents=True)
    (tmp_path / "surfaces" / "_client").mkdir()
    (tmp_path / "surfaces" / "_shared").mkdir()
    (tmp_path / "surfaces" / "__pycache__").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "main.py").touch()
    (tmp_path / "README.md").write_text(SYNTHETIC_README, encoding="utf-8")
    return tmp_path


def test_clean_synthetic_repo_passes(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    assert crl.check(root / "README.md", root) == 0


def test_missing_path_detected(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _make_repo(tmp_path)
    (root / "main.py").unlink()
    assert crl.check(root / "README.md", root) == 1
    assert "main.py" in capsys.readouterr().err


def test_missing_flow_module_detected(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _make_repo(tmp_path)
    (root / "lfg_core" / "swap_flow.py").touch()
    assert crl.check(root / "README.md", root) == 1
    assert "lfg_core/swap_flow.py" in capsys.readouterr().err


def test_missing_surface_package_detected(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _make_repo(tmp_path)
    (root / "surfaces" / "x_bot").mkdir()
    assert crl.check(root / "README.md", root) == 1
    assert "surfaces/x_bot/" in capsys.readouterr().err


def test_underscore_and_pycache_ignored(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    extras = crl.expected_extras(root)
    assert "surfaces/__pycache__/" not in extras
    assert not any("/_" in e for e in extras)


def test_real_readme_passes() -> None:
    """The checked-in README layout block must match the repo today."""
    assert crl.check(REPO_ROOT / "README.md", REPO_ROOT) == 0
