# Amendment Voting CLI — Design Spec

**Date:** 2026-05-20
**Status:** Approved

## Overview

An interactive CLI tool (`amend`) for reviewing and voting on XRPL amendments without manually editing `rippled.cfg`. Built in Python using `rich` + `prompt_toolkit`, styled after the Claude Code CLI interaction model.

---

## Scope

The tool surfaces only amendments that are:
- Not yet enabled on the network
- Not obsolete (filtered via `features.macro` `VoteBehavior::Obsolete`)
- Have the user's current config vote **differing from the network default** (DefaultYes/DefaultNo from `features.macro`)

Amendments already aligned with defaults do not appear — the config is clean by default.

---

## Architecture & Data Flow

Single Python script. Runs as the logged-in user; uses `sudo` for config writes and rippled restarts.

**Startup sequence (parallel):**
1. `sudo rippled feature` → live amendment state (hash, name, majority status, supported flag)
2. Parse `features.macro` → vote defaults and obsolete set (logic shared with `metrics_server.py`)
3. Fetch xrpl.org Known Amendments page → scrape name → description lookup dict (stdlib `urllib` + `html.parser`)

**In-memory state:**
```python
{
  amendment_hash: {
    "name": str,
    "description": str,         # from xrpl.org scrape
    "default_vote": "yes"|"no",
    "your_vote": "yes"|"no",    # current effective vote (cfg override or default)
    "majority": bool,
    "supported": bool,
  }
}
```

Working set = amendments where `your_vote != default_vote`.

---

## UI & Interaction Model

Two-zone layout managed by `prompt_toolkit`:

### Top zone — rich amendment panel
```
┌─ MultiSignReserve [majority] ──────────────────────────────┐
│ Default: YES  │  Your vote: NO (config override)           │
│ Supported: ✓                                               │
│                                                            │
│ Reduces the reserve for multi-signing from 5 XRP to 1 XRP │
│ per signer. Currently holds supermajority.                 │
└────────────────────────────────────────────────────────────┘
```

### Bottom zone — persistent keybinding toolbar
```
 [↑↓/jk] navigate  [y] vote YES  [n] vote NO  [s] skip  [w] write cfg  [t] save temp  [R] restart rippled  [q] quit
```

### Keybinding semantics

| Key | Action |
|-----|--------|
| `↑`/`↓` or `j`/`k` | Navigate between amendments |
| `y` | Cast YES vote (in memory only) |
| `n` | Cast NO vote (in memory only) |
| `s` | Skip this amendment (leave unchanged) |
| `w` | Write current amendment's vote to `rippled.cfg` immediately |
| `t` | Save full in-memory working set to `/tmp/amend-session.json` |
| `R` | Prompt confirmation, then `sudo systemctl restart rippled`; tail first few log lines |
| `q` | Warn if unsaved votes exist; offer to save temp before exiting |

---

## Config Writing

**Target file:** `/etc/opt/ripple/rippled.cfg`

**Backup:** Written to `/etc/opt/ripple/rippled.cfg.bak` before any modification.

**Write logic per amendment:**
- Vote YES → add hash to `[amendments]`; remove from `[veto_amendments]` if present
- Vote NO → add hash to `[veto_amendments]`; remove from `[amendments]` if present
- Vote matches default → remove hash from both sections (config stays clean)

Sections are created if absent. Writes are surgical — only the relevant sections are touched.

**Mechanism:** Script writes to a temp file and applies with `sudo tee`.

---

## Temp File (Session Save)

**Path:** `/tmp/amend-session.json`

**Format:**
```json
[
  {"hash": "abc123...", "name": "MultiSignReserve", "vote": "no"},
  ...
]
```

On next launch, if `/tmp/amend-session.json` exists, the tool offers to resume from it rather than recomputing from scratch. The user can accept, discard, or ignore it.

---

## Installation

**Script location:** `/home/hamsa/motd/amend`
**Symlink:** `/usr/local/bin/amend`

**Dependencies (into existing `.venv`):**
- `rich`
- `prompt_toolkit`
- stdlib only for HTTP scraping (`urllib`, `html.parser`)

**Sudoers file:** `/etc/sudoers.d/amend`
```
hamsa ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/opt/ripple/rippled.cfg
hamsa ALL=(ALL) NOPASSWD: /usr/bin/cp /etc/opt/ripple/rippled.cfg /etc/opt/ripple/rippled.cfg.bak
hamsa ALL=(ALL) NOPASSWD: /bin/systemctl restart rippled
```

**Install script:** `motd/install-amend.sh` — installs pip deps, symlinks the script, drops the sudoers file. Follows pattern of `motd/install-amendments.sh`.

---

## Out of Scope (Future)

- Vote notes surfaced on the web dashboard
