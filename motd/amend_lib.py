#!/usr/bin/env python3
"""Shared data layer for the amend CLI and metrics_server."""
import html.parser
import json
import re
import subprocess
import urllib.request
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
