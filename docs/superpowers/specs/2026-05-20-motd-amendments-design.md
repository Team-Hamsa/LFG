# MOTD Amendments Section — Design Spec
**Date:** 2026-05-20
**Status:** Approved

## Overview

Add an amendments voting status section to the ANSI MOTD rendered by `motd/motd-validator-render`. The section surfaces below the existing Alerts section and above the Identity section, showing every unenabled amendment with its vote and majority status.

---

## Data Collection

### Fetch from metrics server

In `collect_data()`, after the existing rippled/system/network calls, add:

```bash
METRICS_JSON=$(curl -s --max-time 3 http://localhost:8080/metrics 2>/dev/null || echo '{}')
AMD_JSON=$(printf '%s' "$METRICS_JSON" | jq -c '.amendments // []' 2>/dev/null || echo '[]')
```

- `METRICS_JSON` — full payload (reusable if other sections migrate to curl later)
- `AMD_JSON` — compact JSON array of amendment objects, already sorted by the metrics server (majority first, then alphabetical)
- On curl timeout or parse failure: `AMD_JSON` = `[]`

Global declarations at the top of the script:
```bash
METRICS_JSON='{}'
AMD_JSON='[]'
```

---

## `print_amendments()` Function

### Placement in `main()`

```bash
collect_data
print_header
print_validator_system
print_peers_network
print_alerts
print_amendments   # new
print_identity
print_footer
```

### Box structure

The function opens with `box_mid` (full-width horizontal rule, consistent with how `print_identity` opens). All rows are single-column `box_row` calls.

```
╠══════════════════════════════════════════════════════════════════════════════╣
║──────────── PENDING AMENDMENTS (N) ────────────────────────────────────────║
║  Name                               Vote  Status                            ║
║  ──────────────────────────────────────────────────────────────────────     ║
║  fixCleanup3_1_3                    YES   ★ MAJORITY                        ║
║  AMMClawback                        NO    pending                           ║
║  Batch                              NO    pending · unsupported             ║
```

### Column layout

Inner width = 78 visible chars (between the two `║`):

| Segment       | Width | Notes                        |
|---------------|-------|------------------------------|
| prefix `  `   | 2     | two spaces                   |
| name          | 35    | left-aligned, truncated      |
| gap `  `      | 2     | two spaces                   |
| vote          | 3     | `YES` or `NO ` (space-padded)|
| gap `   `     | 3     | three spaces                 |
| status        | 33    | left-aligned, truncated      |

Total: 2 + 35 + 2 + 3 + 3 + 33 = 78 ✓

### Section header

Centered label with `─` dashes filling both sides (cyan):

```
Label = "PENDING AMENDMENTS (N)"
dashes = 78 - len(label) - 2   # 2 for flanking spaces
left   = dashes / 2  (floor)
right  = dashes - left
header = '─' × left + ' ' + label + ' ' + '─' × right
```

Header row rendered in cyan via `box_row`.

### Column header row

```
║  Name                               Vote  Status                            ║
║  ──────────────────────────────────────────────────────────────────────     ║
```

The divider is 71 `─` chars followed by padding to fill 76 chars (after the 2-char prefix).

### Data rows

Parsed per entry from `AMD_JSON` using `jq -c '.[]'`:

```bash
name     = .name      (string, max 35 chars)
vote     = .vote      ("yes" | "no")
supported = .supported (bool)
majority = .majority  (bool)
```

**Vote field** (3 visible chars):
- `YES` in `$GREEN` when `vote == "yes"`
- `NO ` in `$RED` when `vote == "no"`

**Status field** (33 visible chars):
- `★ MAJORITY` in `$AMBER` when `majority == true`
- `pending · unsupported` in `$DIM` when `majority == false` and `supported == false`
- `pending` in `$DIM` when `majority == false` and `supported == true`

### Empty and error states

| `AMD_JSON` value | Displayed                                                        |
|------------------|------------------------------------------------------------------|
| `[]` (empty)     | Single row: `    no pending amendments` in `$DIM`                |
| `AMD_ERROR=true` | Two rows: yellow `  ◈ AMENDMENTS` header + dim `    amendments unavailable` |

---

## rpad() and ANSI compensation

The existing `rpad()` function measures string length with `${#str}`, which counts ANSI escape bytes. Pre-colored fields must be passed with the width adjusted:

```bash
rpad "${VOTE_COLOR}${vote_str}${RESET}" $(( 3 + ${#VOTE_COLOR} + ${#RESET} ))
```

Plain fields (name, status) are passed without color then wrapped after padding.

---

## Testing

Run `motd-validator-test` after implementation and verify:

1. Amendments section appears between Alerts and Identity
2. Majority amendments appear before non-majority
3. `YES` renders green, `NO` renders red
4. `★ MAJORITY` renders amber
5. `pending · unsupported` renders dim
6. Section header is visibly centered
7. Box borders align (no ragged right edges)
8. When metrics server is down: `amendments unavailable` row appears, rest of MOTD renders normally

---

## Out of Scope

- Showing enabled (already-activated) amendments
- Capping the number of rows shown
- Migrating other MOTD data collection to curl
- Changing the sort order (metrics server handles this)
