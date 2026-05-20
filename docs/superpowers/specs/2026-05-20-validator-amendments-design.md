# Validator Amendments Voting Display — Design Spec
**Date:** 2026-05-20
**Status:** Approved

## Overview

Add two things to the validator dashboard:

1. **Fix existing section headers** — center `VALIDATOR` and `SYSTEM` labels within their two-column cells so dashes appear on both sides of each label.
2. **New amendments box** — a second terminal box rendered immediately below the existing vitals box, showing the validator's current vote on every unenabled amendment.

---

## Section Header Centering (existing box)

The `── VALIDATOR──────────────────────────` and `── SYSTEM─────────────────────────────` headers in `buildLines()` are left-aligned. Replace them with centered labels.

**Formula** (column width `W`, label string `L`):
```
dashes = W - len(L) - 2        // 2 for the spaces flanking the label
left   = floor(dashes / 2)
right  = dashes - left
header = '─'.repeat(left) + ' ' + L + ' ' + '─'.repeat(right)
```

- VALIDATOR column: W = 38, label = `VALIDATOR` (9 chars) → 13 left + 14 right
- SYSTEM column: W = 39, label = `SYSTEM` (6 chars) → 15 left + 16 right

No change to the `twoCol()` function or column widths — only the header string content changes.

---

## Amendments Data Layer

### Vote map (module-level, parsed once at startup)

Parse `/home/hamsa/rippled/include/xrpl/protocol/detail/features.macro` at import time using a single regex:

```python
re.findall(
    r'XRPL_(?:FEATURE|FIX)\s*\(\s*(\w+)\s*,\s*Supported::\w+\s*,\s*VoteBehavior::(\w+)\s*\)',
    source
)
```

Build `_VOTE_DEFAULTS: dict[str, str]` mapping amendment name → `"yes"` (DefaultYes) or `"no"` (DefaultNo). If the file is missing or unreadable, `_VOTE_DEFAULTS` is an empty dict — callers fall back to `"no"`.

### Config override detection

Read `/etc/opt/ripple/rippled.cfg` once per request (cheap text read) and parse:
- `[veto_amendments]` — lines are amendment hashes → force vote `"no"`
- `[amendments]` — lines are amendment hashes → force vote `"yes"`

Build a `{hash: "yes"|"no"}` override dict. If the file is unreadable, use an empty dict.

### `get_amendments()` function

1. Run `["sudo", RIPPLED, "feature"]` (new sudoers entry required — see Deployment).
2. Filter `result.features` to entries where `enabled == false`.
3. For each unenabled amendment:
   - Look up hash in config override dict → use if present
   - Otherwise look up name in `_VOTE_DEFAULTS` → use if present
   - Otherwise default to `"no"`
4. Sort: majority entries first (sorted by name), then non-majority entries (sorted by name).
5. Return list of objects:

```json
[
  {
    "name": "fixCleanup3_1_3",
    "vote": "yes",
    "supported": true,
    "majority": true
  },
  {
    "name": "AMMClawback",
    "vote": "no",
    "supported": true,
    "majority": false
  },
  {
    "name": "Batch",
    "vote": "no",
    "supported": false,
    "majority": false
  }
]
```

On any exception, return `[]`.

### JSON payload

Add `amendments` as a new top-level key in `collect_metrics()`. Existing keys are unchanged — no breaking change.

---

## Amendments Display (frontend)

### Placement

A second `<pre id="amd-pre">` element immediately after the existing `<pre id="val-pre">`, with a small `<div id="amd-footer">` below it. Both elements update on every fetch cycle.

### Box layout

Full-width 80-char box (same `╔═══╗` / `║` / `╚═══╝` chrome as the vitals box). Column widths inside 78 visible chars:

```
║  {name:<35}  {vote:<3}   {status:<36}║
```

- Name: 35 chars, left-aligned
- Vote: 3 chars (`YES` or `NO ` — always padded), colored green/red
- Status: remaining 33 chars

### Section header

Centered label with dashes filling both sides:

```
║─────────── PENDING AMENDMENTS (N) ─────────────────────────────────────║
```

Label: `PENDING AMENDMENTS (N)` where N is the count. Dashes computed as:
```
dashes = 78 - len(label) - 2
left   = floor(dashes / 2)
right  = dashes - left
```

Header is rendered in cyan.

### Column header row

```
║  Name                               Vote  Status                        ║
║  ────────────────────────────────────────────────────────────────────   ║
```

### Data rows

```
║  fixCleanup3_1_3                    YES   ★ MAJORITY                    ║
║  AMMClawback                        NO    pending                       ║
║  Batch                              NO    pending · unsupported         ║
```

Status field content and color:
- `★ MAJORITY` in amber — `majority == true`
- `pending · unsupported` in dim — `majority == false` and `supported == false`
- `pending` in dim — `majority == false` and `supported == true`

Vote field color: `YES` in green (`.val-green`), `NO ` in red (`.val-red`).

### Empty and error states

- `amendments` array empty: single dim row `  no pending amendments`
- `amendments` key missing from payload: single dim row `  retrieving...`

### New JS function

`buildAmendmentLines(amendments)` — parallel to `buildLines()`, same `rpad()` / `span()` / `H78` helpers. Called from `renderData()` alongside the existing call to `buildLines()`.

---

## Deployment

### New sudoers entry

Add to `/etc/sudoers.d/metrics-server` alongside the existing `server_info` line:

```
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled feature
```

A new `install-amendments.sh` script handles this plus a service restart, following the same pattern as `install-service.sh` and `install-rapl.sh`.

### No new CSS

All color classes (`.val-green`, `.val-red`, `.val-amber`, `.val-cyan`, `.val-dim`, `.val-white`) are already defined in `styles.css`. No additions needed.

---

## Testing

### Server (`test_metrics_server.py`)

- `TestGetAmendments` class with a `MOCK_FEATURE_JSON` fixture containing 2 enabled and 3 unenabled amendments (one with majority, one without, one unsupported).
- `test_returns_only_unenabled` — enabled amendments absent from result
- `test_vote_from_defaults` — DefaultYes → `"yes"`, DefaultNo → `"no"`
- `test_config_veto_overrides_default` — hash in `[veto_amendments]` → `"no"` even for DefaultYes
- `test_config_vote_overrides_default` — hash in `[amendments]` → `"yes"` even for DefaultNo
- `test_majority_sorted_first` — majority entries precede non-majority
- `test_returns_empty_on_exception` — subprocess failure → `[]`

### `_VOTE_DEFAULTS` parsing

- `test_vote_map_parsed_at_startup` — patch `open` with sample macro content, verify map keys and values

---

## Out of Scope

- Displaying enabled (already-activated) amendments
- Historical voting record
- Ability to change votes from the dashboard
- Showing the amendment hash
- Mobile layout
