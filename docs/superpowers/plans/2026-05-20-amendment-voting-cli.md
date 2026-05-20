# Amendment Voting CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an interactive CLI tool (`amend`) that walks the validator operator through pending XRPL amendments whose current config vote differs from the network default, with a rich+prompt_toolkit TUI for reviewing, voting, writing config, and restarting rippled.

**Architecture:** A shared library (`amend_lib.py`) handles all data logic (parsing, fetching, config writing, session save/load) and is imported by both the `amend` TUI script and `metrics_server.py`. The TUI uses `rich` to render amendment panels and `prompt_toolkit`'s `PromptSession` for single-keypress input with a persistent bottom toolbar.

**Tech Stack:** Python 3.10, `rich`, `prompt_toolkit` (pip), `urllib`/`html.parser` (stdlib), `sudo rippled feature`, `/etc/opt/ripple/rippled.cfg`

**Spec:** `docs/superpowers/specs/2026-05-20-amendment-voting-cli-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `motd/amend_lib.py` | Create | All data logic: parsing, fetching, config write, session I/O |
| `motd/amend` | Create | TUI harness: render + keybindings + main loop |
| `motd/test_amend_lib.py` | Create | Unit tests for `amend_lib` |
| `motd/metrics_server.py` | Modify | Replace duplicated parsing functions with imports from `amend_lib` |
| `motd/install-amend.sh` | Create | Pip install, symlink, sudoers entry |

---

## Task 1: Install script + dependencies + sudoers

**Files:**
- Create: `motd/install-amend.sh`

- [ ] **Step 1: Write the install script**

```bash
#!/usr/bin/env bash
# Install the `amend` amendment voting CLI.
# Run as: sudo bash install-amend.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run with sudo: sudo bash $0"
  exit 1
fi

echo "=== Installing Python dependencies into .venv ==="
/home/hamsa/.venv/bin/pip install rich prompt_toolkit

echo "=== Installing amend script ==="
cp "$(dirname "$0")/amend" /usr/local/bin/amend
chmod 755 /usr/local/bin/amend

echo "=== Writing sudoers entry ==="
tee /etc/sudoers.d/amend > /dev/null << 'EOF'
hamsa ALL=(ALL) NOPASSWD: /usr/bin/cat /etc/opt/ripple/rippled.cfg
hamsa ALL=(ALL) NOPASSWD: /usr/bin/cp /etc/opt/ripple/rippled.cfg /etc/opt/ripple/rippled.cfg.bak
hamsa ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/opt/ripple/rippled.cfg
hamsa ALL=(ALL) NOPASSWD: /bin/systemctl restart rippled
hamsa ALL=(ALL) NOPASSWD: /bin/journalctl -u rippled -n 20 --no-pager
EOF
chmod 0440 /etc/sudoers.d/amend
visudo -c
echo "=== Done. Run: amend ==="
```

- [ ] **Step 2: Commit**

```bash
git add motd/install-amend.sh
git commit -m "feat: add amend install script and sudoers entry"
```

---

## Task 2: Write failing tests for `amend_lib` parsing functions

**Files:**
- Create: `motd/test_amend_lib.py`

- [ ] **Step 1: Create the test file**

```python
#!/usr/bin/env python3
"""Tests for amend_lib.py"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import amend_lib

SAMPLE_MACRO = """
XRPL_FEATURE(MultiSignReserve, Supported::yes, VoteBehavior::DefaultYes)
XRPL_FIX(fix1781, Supported::yes, VoteBehavior::DefaultNo)
XRPL_FEATURE(Clawback, Supported::yes, VoteBehavior::Obsolete)
XRPL_FIX(fixNFTokenRemint, Supported::yes, VoteBehavior::DefaultNo)
"""

SAMPLE_CFG = """
[server]
port_rpc_admin_local

[amendments]
AABBCC001122
DDEEFF334455

[veto_amendments]
FFEE99887766
"""


class TestParseVoteDefaults(unittest.TestCase):
    def test_parses_defaultyes(self):
        result = amend_lib.parse_vote_defaults(SAMPLE_MACRO)
        self.assertEqual(result["MultiSignReserve"], "yes")

    def test_parses_defaultno(self):
        result = amend_lib.parse_vote_defaults(SAMPLE_MACRO)
        self.assertEqual(result["fix1781"], "no")

    def test_excludes_obsolete(self):
        result = amend_lib.parse_vote_defaults(SAMPLE_MACRO)
        self.assertNotIn("Clawback", result)


class TestParseObsoleteFeatures(unittest.TestCase):
    def test_finds_obsolete(self):
        result = amend_lib.parse_obsolete_features(SAMPLE_MACRO)
        self.assertIn("Clawback", result)

    def test_excludes_active(self):
        result = amend_lib.parse_obsolete_features(SAMPLE_MACRO)
        self.assertNotIn("MultiSignReserve", result)


class TestParseCfgOverrides(unittest.TestCase):
    def test_parses_amendments_yes(self):
        result = amend_lib.parse_cfg_overrides(SAMPLE_CFG)
        self.assertEqual(result["AABBCC001122"], "yes")
        self.assertEqual(result["DDEEFF334455"], "yes")

    def test_parses_veto_no(self):
        result = amend_lib.parse_cfg_overrides(SAMPLE_CFG)
        self.assertEqual(result["FFEE99887766"], "no")

    def test_empty_cfg(self):
        result = amend_lib.parse_cfg_overrides("")
        self.assertEqual(result, {})


class TestComputeWorkingSet(unittest.TestCase):
    def _features(self):
        return {
            "HASH_YES_OVERRIDE": {"name": "fix1781", "supported": True},
            "HASH_MATCHES_DEFAULT": {"name": "MultiSignReserve", "supported": True},
            "HASH_ENABLED": {"name": "fixNFTokenRemint", "enabled": True, "supported": True},
        }

    def test_includes_differing_vote(self):
        # fix1781 default is "no"; cfg override says "yes" → should appear
        features = self._features()
        vote_defaults = {"fix1781": "no", "MultiSignReserve": "yes", "fixNFTokenRemint": "no"}
        obsolete = set()
        overrides = {"HASH_YES_OVERRIDE": "yes"}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        names = [a["name"] for a in result]
        self.assertIn("fix1781", names)

    def test_excludes_matching_default(self):
        # MultiSignReserve default "yes", no override → matches default → excluded
        features = self._features()
        vote_defaults = {"fix1781": "no", "MultiSignReserve": "yes"}
        obsolete = set()
        overrides = {}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        names = [a["name"] for a in result]
        self.assertNotIn("MultiSignReserve", names)

    def test_excludes_enabled(self):
        features = self._features()
        vote_defaults = {"fixNFTokenRemint": "no"}
        obsolete = set()
        overrides = {"HASH_ENABLED": "yes"}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        names = [a["name"] for a in result]
        self.assertNotIn("fixNFTokenRemint", names)

    def test_excludes_obsolete(self):
        features = {"HASH_OBS": {"name": "Clawback", "supported": True}}
        vote_defaults = {"Clawback": "no"}
        obsolete = {"Clawback"}
        overrides = {"HASH_OBS": "yes"}
        result = amend_lib.compute_working_set(features, vote_defaults, obsolete, overrides)
        self.assertEqual(result, [])


class TestUpdateCfgText(unittest.TestCase):
    def test_adds_to_amendments_section(self):
        cfg = "[amendments]\nexistinghash\n\n[server]\nport 6006\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "yes", "no")
        lines = result.splitlines()
        amend_idx = lines.index("[amendments]")
        self.assertIn("newhash", lines[amend_idx + 1 : amend_idx + 3])

    def test_adds_to_veto_section(self):
        cfg = "[veto_amendments]\nexistinghash\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "no", "yes")
        self.assertIn("newhash", result)
        self.assertIn("[veto_amendments]", result)

    def test_removes_from_opposite_section(self):
        cfg = "[amendments]\nnewhash\n\n[veto_amendments]\nother\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "no", "yes")
        sections = result.split("[amendments]")
        if len(sections) > 1:
            self.assertNotIn("newhash", sections[1].split("[")[0])

    def test_removes_when_vote_matches_default(self):
        cfg = "[amendments]\nnewhash\nextra\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "yes", "yes")
        lines = [l.strip() for l in result.splitlines()]
        self.assertNotIn("newhash", lines)

    def test_creates_section_when_absent(self):
        cfg = "[server]\nport 6006\n"
        result = amend_lib.update_cfg_text(cfg, "newhash", "yes", "no")
        self.assertIn("[amendments]", result)
        self.assertIn("newhash", result)


class TestSessionSaveLoad(unittest.TestCase):
    def test_roundtrip(self):
        amendments = [
            {"hash": "ABC", "name": "TestAmend", "vote": "no",
             "default_vote": "yes", "majority": False, "supported": True, "description": ""},
        ]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            amend_lib.save_session(amendments, path=path)
            loaded = amend_lib.load_session(path=path)
            self.assertEqual(loaded[0]["hash"], "ABC")
            self.assertEqual(loaded[0]["vote"], "no")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — confirm they all fail (module not found)**

```bash
cd /home/hamsa/motd && python3 -m pytest test_amend_lib.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'amend_lib'`

- [ ] **Step 3: Commit**

```bash
git add motd/test_amend_lib.py
git commit -m "test: add failing tests for amend_lib"
```

---

## Task 3: Implement `amend_lib.py` — parsing, working set, session

**Files:**
- Create: `motd/amend_lib.py`

- [ ] **Step 1: Create `amend_lib.py`**

```python
#!/usr/bin/env python3
"""Shared data layer for the amend CLI and metrics_server."""
import json
import re
import subprocess
from pathlib import Path

RIPPLED = "/usr/local/bin/rippled"
RIPPLED_CFG = "/etc/opt/ripple/rippled.cfg"
FEATURES_MACRO = "/home/hamsa/rippled/include/xrpl/protocol/detail/features.macro"
SESSION_FILE = "/tmp/amend-session.json"
XRPL_AMENDMENTS_URL = "https://xrpl.org/known-amendments.html"


def parse_vote_defaults(macro_text: str) -> dict:
    """Parse VoteBehavior from features.macro → {name: 'yes'|'no'}. Excludes Obsolete."""
    matches = re.findall(
        r'XRPL_(?:FEATURE|FIX)\s*\(\s*(\w+)\s*,\s*Supported::\w+\s*,'
        r'\s*VoteBehavior::(\w+)\s*\)',
        macro_text,
    )
    return {name: ("yes" if vote == "DefaultYes" else "no")
            for name, vote in matches if vote != "Obsolete"}


def parse_obsolete_features(macro_text: str) -> set:
    """Return set of amendment names marked VoteBehavior::Obsolete."""
    return set(re.findall(
        r'XRPL_(?:FEATURE|FIX)\s*\(\s*(\w+)\s*,\s*Supported::\w+\s*,'
        r'\s*VoteBehavior::Obsolete\s*\)',
        macro_text,
    ))


def parse_cfg_overrides(cfg_text: str) -> dict:
    """Parse [amendments] and [veto_amendments] sections → {hash: 'yes'|'no'}."""
    overrides: dict = {}
    section = None
    for line in cfg_text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.strip("[]")
        elif section == "veto_amendments" and line and not line.startswith("#"):
            overrides[line] = "no"
        elif section == "amendments" and line and not line.startswith("#"):
            overrides[line] = "yes"
    return overrides


def get_live_features() -> dict:
    """Call `sudo rippled feature` and return features dict keyed by hash."""
    raw = subprocess.check_output(
        ["sudo", RIPPLED, "feature"],
        timeout=10, text=True, stderr=subprocess.DEVNULL,
    )
    return json.loads(raw)["result"]["features"]


def compute_working_set(
    features: dict,
    vote_defaults: dict,
    obsolete: set,
    cfg_overrides: dict,
) -> list:
    """Return amendments where current vote differs from network default.

    Excludes: enabled amendments, obsolete amendments, votes matching default.
    Sorted: majority amendments first, then alphabetical by name.
    """
    result = []
    for hash_, data in features.items():
        if data.get("enabled"):
            continue
        name = data.get("name", "")
        if name in obsolete:
            continue
        default_vote = vote_defaults.get(name, "no")
        your_vote = cfg_overrides.get(hash_) or default_vote
        if your_vote == default_vote:
            continue
        result.append({
            "hash": hash_,
            "name": name,
            "default_vote": default_vote,
            "your_vote": your_vote,
            "majority": "majority" in data,
            "supported": data.get("supported", False),
            "description": "",
        })
    result.sort(key=lambda x: (not x["majority"], x["name"]))
    return result


def _remove_hash_from_sections(cfg_text: str, hash_: str) -> str:
    """Remove hash from [amendments] and [veto_amendments] sections."""
    current_section = None
    result = []
    for line in cfg_text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            current_section = stripped.strip("[]")
        if current_section in ("amendments", "veto_amendments") and stripped == hash_:
            continue
        result.append(line)
    return "".join(result)


def _add_hash_to_section(cfg_text: str, hash_: str, section: str) -> str:
    """Add hash as first entry under [section], creating section if absent."""
    header = f"[{section}]"
    lines = cfg_text.splitlines(keepends=True)
    result = []
    added = False
    for line in lines:
        result.append(line)
        if line.strip() == header and not added:
            result.append(hash_ + "\n")
            added = True
    if not added:
        if result and not result[-1].endswith("\n"):
            result.append("\n")
        result.append(f"\n{header}\n{hash_}\n")
    return "".join(result)


def update_cfg_text(cfg_text: str, hash_: str, vote: str, default_vote: str) -> str:
    """Return updated cfg text with hash voted correctly. Pure — no side effects."""
    cleaned = _remove_hash_from_sections(cfg_text, hash_)
    if vote != default_vote:
        target = "amendments" if vote == "yes" else "veto_amendments"
        cleaned = _add_hash_to_section(cleaned, hash_, target)
    return cleaned


def write_cfg_vote(hash_: str, vote: str, default_vote: str) -> None:
    """Backup cfg, then write a single amendment vote. Requires sudo."""
    cfg_text = subprocess.check_output(
        ["sudo", "cat", RIPPLED_CFG], text=True, stderr=subprocess.DEVNULL,
    )
    new_cfg = update_cfg_text(cfg_text, hash_, vote, default_vote)
    subprocess.run(
        ["sudo", "cp", RIPPLED_CFG, RIPPLED_CFG + ".bak"], check=True,
    )
    proc = subprocess.run(
        ["sudo", "tee", RIPPLED_CFG],
        input=new_cfg, text=True, capture_output=True,
    )
    proc.check_returncode()


def save_session(amendments: list, path: str = SESSION_FILE) -> None:
    """Save current in-memory votes to a JSON temp file for later resumption."""
    data = [{"hash": a["hash"], "name": a["name"], "vote": a["your_vote"]}
            for a in amendments]
    Path(path).write_text(json.dumps(data, indent=2))


def load_session(path: str = SESSION_FILE) -> list:
    """Load saved session votes. Returns [] if file absent or invalid."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return []
```

- [ ] **Step 2: Run tests — confirm they pass**

```bash
cd /home/hamsa/motd && python3 -m pytest test_amend_lib.py -v
```

Expected: All `TestParseVoteDefaults`, `TestParseObsoleteFeatures`, `TestParseCfgOverrides`, `TestComputeWorkingSet`, `TestUpdateCfgText`, `TestSessionSaveLoad` tests PASS.

- [ ] **Step 3: Commit**

```bash
git add motd/amend_lib.py
git commit -m "feat: implement amend_lib data layer"
```

---

## Task 4: Implement description scraper + test

**Files:**
- Modify: `motd/amend_lib.py` (add scraper)
- Modify: `motd/test_amend_lib.py` (add parser test)

- [ ] **Step 1: Add scraper test to `test_amend_lib.py`**

Add this class before `if __name__ == "__main__":`:

```python
class TestAmendmentDescriptionParser(unittest.TestCase):
    SAMPLE_HTML = """
    <html><body>
    <h2 id="multisignreserve">MultiSignReserve</h2>
    <p>Reduces the reserve for multi-signing from 5 XRP to 1 XRP per signer.</p>
    <p>Second paragraph, should be ignored.</p>
    <h2 id="fix1781">fix1781</h2>
    <p>Fixes an edge case in payment path finding.</p>
    </body></html>
    """

    def test_parses_first_paragraph(self):
        parser = amend_lib._AmendmentDescriptionParser()
        parser.feed(self.SAMPLE_HTML)
        desc = parser.get_descriptions()
        self.assertEqual(desc["MultiSignReserve"],
                         "Reduces the reserve for multi-signing from 5 XRP to 1 XRP per signer.")

    def test_parses_multiple_amendments(self):
        parser = amend_lib._AmendmentDescriptionParser()
        parser.feed(self.SAMPLE_HTML)
        desc = parser.get_descriptions()
        self.assertIn("fix1781", desc)

    def test_only_first_paragraph_per_amendment(self):
        parser = amend_lib._AmendmentDescriptionParser()
        parser.feed(self.SAMPLE_HTML)
        desc = parser.get_descriptions()
        self.assertNotIn("Second paragraph", desc.get("MultiSignReserve", ""))
```

- [ ] **Step 2: Run — confirm new tests fail**

```bash
cd /home/hamsa/motd && python3 -m pytest test_amend_lib.py::TestAmendmentDescriptionParser -v
```

Expected: `AttributeError: module 'amend_lib' has no attribute '_AmendmentDescriptionParser'`

- [ ] **Step 3: Add scraper to `amend_lib.py`**

Add these imports at the top of `amend_lib.py`:
```python
import html.parser
import urllib.request
```

Then add after the `load_session` function:

```python
class _AmendmentDescriptionParser(html.parser.HTMLParser):
    """Scrape amendment name → first paragraph description from xrpl.org."""

    def __init__(self):
        super().__init__()
        self._descriptions: dict = {}
        self._current_name: str | None = None
        self._in_h2 = False
        self._in_p = False
        self._h2_buf = ""
        self._p_buf = ""

    def handle_starttag(self, tag, attrs):
        if tag == "h2":
            self._in_h2 = True
            self._h2_buf = ""
            self._current_name = None
        elif tag == "p" and self._current_name and self._current_name not in self._descriptions:
            self._in_p = True
            self._p_buf = ""

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_h2 = False
            name = self._h2_buf.strip()
            if name:
                self._current_name = name
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = self._p_buf.strip()
            if self._current_name and text:
                self._descriptions[self._current_name] = text

    def handle_data(self, data):
        if self._in_h2:
            self._h2_buf += data
        elif self._in_p:
            self._p_buf += data

    def get_descriptions(self) -> dict:
        return self._descriptions


def fetch_amendment_descriptions() -> dict:
    """Fetch xrpl.org Known Amendments page and return {name: description}. Returns {} on error."""
    try:
        req = urllib.request.Request(
            XRPL_AMENDMENTS_URL,
            headers={"User-Agent": "amend-cli/1.0 (XRPL validator tool)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html_text = resp.read().decode("utf-8")
        parser = _AmendmentDescriptionParser()
        parser.feed(html_text)
        return parser.get_descriptions()
    except Exception:
        return {}
```

- [ ] **Step 4: Run all tests — confirm they pass**

```bash
cd /home/hamsa/motd && python3 -m pytest test_amend_lib.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add motd/amend_lib.py motd/test_amend_lib.py
git commit -m "feat: add xrpl.org amendment description scraper"
```

---

## Task 5: Refactor `metrics_server.py` to import from `amend_lib`

**Files:**
- Modify: `motd/metrics_server.py`

- [ ] **Step 1: Replace duplicated parsing functions**

In `metrics_server.py`, remove the bodies of `_parse_vote_defaults`, `_parse_obsolete_features`, and `_parse_cfg_overrides`, and replace the entire block (lines ~22–70) with imports:

Find this block:
```python
def _parse_vote_defaults() -> dict:
    try:
        source = open(FEATURES_MACRO).read()
        matches = re.findall(
            r'XRPL_(?:FEATURE|FIX)\s*\(\s*(\w+)\s*,\s*Supported::\w+\s*,'
            r'\s*VoteBehavior::(\w+)\s*\)',
            source,
        )
        return {name: ("yes" if vote == "DefaultYes" else "no") for name, vote in matches}
    except Exception:
        return {}


def _parse_obsolete_features() -> set:
    try:
        source = open(FEATURES_MACRO).read()
        return set(re.findall(
            r'XRPL_(?:FEATURE|FIX)\s*\(\s*(\w+)\s*,\s*Supported::\w+\s*,'
            r'\s*VoteBehavior::Obsolete\s*\)',
            source,
        ))
    except Exception:
        return set()


_VOTE_DEFAULTS: dict = _parse_vote_defaults()
_OBSOLETE_FEATURES: set = _parse_obsolete_features()


def _parse_cfg_overrides() -> dict:
    try:
        text = open(RIPPLED_CFG).read()
    except Exception:
        return {}
    overrides: dict = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.strip("[]")
        elif section == "veto_amendments" and line and not line.startswith("#"):
            overrides[line] = "no"
        elif section == "amendments" and line and not line.startswith("#"):
            overrides[line] = "yes"
    return overrides
```

Replace with:
```python
import amend_lib as _alib


def _parse_vote_defaults() -> dict:
    try:
        return _alib.parse_vote_defaults(open(FEATURES_MACRO).read())
    except Exception:
        return {}


def _parse_obsolete_features() -> set:
    try:
        return _alib.parse_obsolete_features(open(FEATURES_MACRO).read())
    except Exception:
        return set()


_VOTE_DEFAULTS: dict = _parse_vote_defaults()
_OBSOLETE_FEATURES: set = _parse_obsolete_features()


def _parse_cfg_overrides() -> dict:
    try:
        return _alib.parse_cfg_overrides(open(RIPPLED_CFG).read())
    except Exception:
        return {}
```

- [ ] **Step 2: Run existing metrics_server tests to confirm nothing broke**

```bash
cd /home/hamsa/motd && python3 -m pytest test_metrics_server.py -v
```

Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add motd/metrics_server.py
git commit -m "refactor: metrics_server imports parsing logic from amend_lib"
```

---

## Task 6: Build the `amend` TUI harness

**Files:**
- Create: `motd/amend`

- [ ] **Step 1: Create the executable script**

```python
#!/home/hamsa/.venv/bin/python3
"""amend — interactive XRPL amendment voting CLI."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import amend_lib

from rich.console import Console
from rich.panel import Panel
from rich import box
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import HTML

console = Console()

TOOLBAR = (
    " <b>[↑/k]</b> prev  <b>[↓/j]</b> next  "
    "<b>[y]</b> YES  <b>[n]</b> NO  <b>[s]</b> skip  "
    "<b>[w]</b> write cfg  <b>[t]</b> save temp  "
    "<b>[R]</b> restart rippled  <b>[q]</b> quit "
)


def _wait_for_key() -> str:
    kb = KeyBindings()
    captured = []

    @kb.add("<any>", eager=True)
    def _(event):
        captured.append(event.key_sequence[0].key)
        event.app.exit()

    PromptSession(key_bindings=kb, bottom_toolbar=HTML(TOOLBAR)).prompt("")
    return captured[0] if captured else ""


def _render(amendment: dict, idx: int, total: int) -> None:
    console.clear()
    a = amendment
    vote_str = "[bold green]YES[/]" if a["your_vote"] == "yes" else "[bold red]NO[/]"
    default_str = "YES" if a["default_vote"] == "yes" else "NO"
    supported_str = "[green]✓[/]" if a["supported"] else "[bold red]✗ UNSUPPORTED — upgrade rippled before this activates[/]"
    majority_tag = "  [yellow]\[majority][/yellow]" if a["majority"] else ""
    changed = a["your_vote"] != a.get("_written_vote", a["your_vote"])

    body = (
        f"Default: {default_str}   Your vote: {vote_str}"
        + ("  [dim](unsaved)[/dim]" if changed else "") + "\n"
        f"Supported: {supported_str}\n\n"
        f"{a['description'] or '[dim]No description available.[/dim]'}"
    )
    console.print(Panel(
        body,
        title=f"[bold]{a['name']}[/bold]{majority_tag}",
        subtitle=f"[dim]{idx + 1} / {total}[/dim]",
        box=box.ROUNDED,
        expand=False,
        width=min(console.width, 80),
    ))


def _write_vote(amendment: dict) -> None:
    try:
        amend_lib.write_cfg_vote(amendment["hash"], amendment["your_vote"], amendment["default_vote"])
        amendment["_written_vote"] = amendment["your_vote"]
        console.print(f"[green]✓ Written:[/] {amendment['name']} → {amendment['your_vote'].upper()}")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error writing config:[/] {e}")
    input("Press Enter to continue...")


def _restart_rippled() -> None:
    console.print("\n[yellow]Restart rippled now?[/] [y/N] ", end="")
    ans = input().strip().lower()
    if ans != "y":
        return
    console.print("[cyan]Restarting rippled...[/]")
    subprocess.run(["sudo", "systemctl", "restart", "rippled"], check=False)
    console.print("[cyan]Recent logs:[/]")
    subprocess.run(["sudo", "journalctl", "-u", "rippled", "-n", "20", "--no-pager"])
    input("Press Enter to continue...")


def _handle_quit(amendments: list) -> bool:
    unsaved = [a for a in amendments if a["your_vote"] != a.get("_written_vote", a["default_vote"])]
    if not unsaved:
        return True
    console.print(f"\n[yellow]{len(unsaved)} vote(s) not written to config.[/]")
    console.print("Save to temp file before quitting? [y/N/cancel] ", end="")
    ans = input().strip().lower()
    if ans == "cancel":
        return False
    if ans == "y":
        amend_lib.save_session(amendments)
        console.print(f"[green]Saved to {amend_lib.SESSION_FILE}[/]")
    return True


def main() -> None:
    console.print("[bold cyan]amend[/] — XRPL Amendment Voting CLI\n")

    console.print("[dim]Loading features.macro...[/]")
    try:
        macro_text = Path(amend_lib.FEATURES_MACRO).read_text()
    except FileNotFoundError:
        console.print(f"[red]features.macro not found at {amend_lib.FEATURES_MACRO}[/]")
        sys.exit(1)

    vote_defaults = amend_lib.parse_vote_defaults(macro_text)
    obsolete = amend_lib.parse_obsolete_features(macro_text)

    console.print("[dim]Fetching live amendment state from rippled...[/]")
    try:
        features = amend_lib.get_live_features()
    except Exception as e:
        console.print(f"[red]Failed to query rippled:[/] {e}")
        sys.exit(1)

    console.print("[dim]Reading config overrides...[/]")
    try:
        cfg_text = subprocess.check_output(
            ["sudo", "cat", amend_lib.RIPPLED_CFG], text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        cfg_text = ""
    cfg_overrides = amend_lib.parse_cfg_overrides(cfg_text)

    console.print("[dim]Fetching amendment descriptions from xrpl.org...[/]")
    descriptions = amend_lib.fetch_amendment_descriptions()
    if not descriptions:
        console.print("[yellow]Warning: could not fetch descriptions (offline?)[/]")

    amendments = amend_lib.compute_working_set(features, vote_defaults, obsolete, cfg_overrides)

    session_path = Path(amend_lib.SESSION_FILE)
    if session_path.exists():
        console.print(f"\n[yellow]Saved session found at {amend_lib.SESSION_FILE}[/]")
        console.print("Resume from saved votes? [y/N] ", end="")
        if input().strip().lower() == "y":
            saved = amend_lib.load_session()
            saved_map = {s["hash"]: s["vote"] for s in saved}
            for a in amendments:
                if a["hash"] in saved_map:
                    a["your_vote"] = saved_map[a["hash"]]
            console.print(f"[green]Resumed {len(saved_map)} saved votes.[/]")

    for a in amendments:
        a["description"] = descriptions.get(a["name"], "")
        a["_written_vote"] = cfg_overrides.get(a["hash"], a["default_vote"])

    if not amendments:
        console.print("\n[green]✓ Nothing to review — all your votes match network defaults.[/]")
        return

    console.print(f"\n[bold]{len(amendments)} amendment(s) to review.[/] Use arrow keys or j/k to navigate.\n")
    input("Press Enter to start...")

    idx = 0
    while True:
        _render(amendments[idx], idx, len(amendments))
        try:
            key = _wait_for_key()
        except (KeyboardInterrupt, EOFError):
            if _handle_quit(amendments):
                break
            continue

        if key in ("up", "k"):
            idx = max(0, idx - 1)
        elif key in ("down", "j", "s"):
            idx = min(len(amendments) - 1, idx + 1)
        elif key == "y":
            amendments[idx]["your_vote"] = "yes"
        elif key == "n":
            amendments[idx]["your_vote"] = "no"
        elif key == "w":
            _write_vote(amendments[idx])
        elif key == "t":
            amend_lib.save_session(amendments)
            console.print(f"[green]Session saved to {amend_lib.SESSION_FILE}[/]")
            input("Press Enter to continue...")
        elif key == "R":
            _restart_rippled()
        elif key == "q":
            if _handle_quit(amendments):
                break


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable**

```bash
chmod +x /home/hamsa/motd/amend
```

- [ ] **Step 3: Install pip dependencies**

```bash
/home/hamsa/.venv/bin/pip install rich prompt_toolkit
```

Expected: Both packages install successfully.

- [ ] **Step 4: Commit**

```bash
git add motd/amend
git commit -m "feat: add amend TUI harness (rich + prompt_toolkit)"
```

---

## Task 7: Install and smoke test

**Files:**
- No new files — wire up install script and validate end-to-end

- [ ] **Step 1: Run the install script**

```bash
sudo bash /home/hamsa/motd/install-amend.sh
```

Expected: deps installed, `/usr/local/bin/amend` created, sudoers entry validated with `visudo -c`.

- [ ] **Step 2: Verify symlink and sudoers**

```bash
which amend && amend --help 2>/dev/null || echo "(no --help, that's fine)"
sudo visudo -c
```

- [ ] **Step 3: Dry-run the data loading (non-interactive)**

```bash
python3 - << 'EOF'
import sys
sys.path.insert(0, "/home/hamsa/motd")
import amend_lib
from pathlib import Path

macro = Path(amend_lib.FEATURES_MACRO).read_text()
defaults = amend_lib.parse_vote_defaults(macro)
obsolete = amend_lib.parse_obsolete_features(macro)
print(f"vote_defaults: {len(defaults)} amendments")
print(f"obsolete: {len(obsolete)} amendments")

import subprocess
cfg = subprocess.check_output(["sudo", "cat", amend_lib.RIPPLED_CFG], text=True)
overrides = amend_lib.parse_cfg_overrides(cfg)
print(f"cfg overrides: {overrides}")

features = amend_lib.get_live_features()
print(f"live features: {len(features)} total")

working_set = amend_lib.compute_working_set(features, defaults, obsolete, overrides)
print(f"working set (needs review): {len(working_set)}")
for a in working_set:
    print(f"  {a['name']}: default={a['default_vote']} yours={a['your_vote']} majority={a['majority']}")

descriptions = amend_lib.fetch_amendment_descriptions()
print(f"descriptions fetched: {len(descriptions)}")
EOF
```

Expected: Numbers print cleanly. Working set shows any amendments where your config vote differs from default.

- [ ] **Step 4: Run full test suite**

```bash
cd /home/hamsa/motd && python3 -m pytest test_amend_lib.py test_metrics_server.py -v
```

Expected: All tests PASS.

- [ ] **Step 5: Final commit**

```bash
git add motd/install-amend.sh
git commit -m "feat: complete amend CLI — install script + smoke test verified"
```
