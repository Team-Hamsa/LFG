# MOTD Amendments Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `print_amendments()` section to `motd/motd-validator-render` that displays pending XRPL amendments by curling the local metrics server.

**Architecture:** `collect_data()` fetches `http://localhost:8080/metrics` and stores the amendments array in globals. `print_amendments()` renders a full-width ANSI box section between the Alerts and Identity sections. Vote and status fields are ANSI-colored; all box rows are exactly 80 visible chars.

**Tech Stack:** bash, jq, curl

---

### Reference: ANSI code byte lengths (used in `rpad` compensation)

All ANSI codes in the render script are ASCII-only, so `${#VAR}` equals the byte count:

| Variable | Sequence  | `${#VAR}` |
|----------|-----------|-----------|
| `$RESET` | `\e[0m`   | 4         |
| `$GREEN` | `\e[92m`  | 5         |
| `$RED`   | `\e[91m`  | 5         |
| `$AMBER` | `\e[33m`  | 5         |
| `$DIM`   | `\e[2m`   | 4         |
| `$CYAN`  | `\e[96m`  | 5         |
| `$YELLOW`| `\e[93m`  | 5         |

The script runs with `LC_ALL=en_US.UTF-8`, so `${#str}` counts Unicode characters, not bytes. Multi-byte Unicode chars (─, ║, ★, etc.) each count as 1.

### Reference: Column layout (78 visible inner chars)

```
║  {name:35}  {vote:3}   {status:33}║
  ^^          ^^         ^^^
  prefix(2)   gap(2)     gap(3)
```

Total: 2 + 35 + 2 + 3 + 3 + 33 = 78 ✓

---

### Task 1: Write failing tests for `print_amendments()`

**Files:**
- Modify: `motd/motd-validator-test`

- [ ] **Step 1: Add the amendments test block to `motd-validator-test`**

  Open `motd/motd-validator-test`. Before the final `echo "Results: ..."` and `[[ $FAIL -eq 0 ]]` lines, insert:

```bash
# ── print_amendments ─────────────────────────────────────────────────────────
echo ""
echo "=== print_amendments ==="

# Test 1: all box rows are 80 visible chars with amendment data
AMD_JSON='[{"name":"AMMClawback","vote":"yes","supported":true,"majority":true},{"name":"Batch","vote":"no","supported":false,"majority":false}]'
AMD_ERROR=false
amd_w_pass=0; amd_w_fail=0
while IFS= read -r line; do
    stripped=$(strip_ansi "$line")
    if [[ "$stripped" =~ ^[╠║] ]]; then
        if [[ ${#stripped} -eq 80 ]]; then
            (( amd_w_pass++ )) || true
        else
            fail "print_amendments: row width 80" "${#stripped}" "80"
            (( amd_w_fail++ )) || true
        fi
    fi
done < <(print_amendments)
[[ $amd_w_pass -gt 0 && $amd_w_fail -eq 0 ]] \
    && ok "print_amendments: all rows width 80 (with data)" \
    || fail "print_amendments: all rows width 80 (with data)" "${amd_w_fail} bad rows" "0 bad rows"

# Test 2: empty state row is 80 visible chars
AMD_JSON='[]'
AMD_ERROR=false
amd_empty_ok=false
while IFS= read -r line; do
    stripped=$(strip_ansi "$line")
    if [[ "$stripped" =~ ^║ ]]; then
        [[ ${#stripped} -eq 80 ]] \
            && { ok "print_amendments: empty state row width 80"; amd_empty_ok=true; } \
            || fail "print_amendments: empty state row width 80" "${#stripped}" "80"
        break
    fi
done < <(print_amendments)
$amd_empty_ok || fail "print_amendments: empty state produced no rows" "" "at least one row"

# Test 3: error state row is 80 visible chars
AMD_JSON='[]'
AMD_ERROR=true
amd_err_ok=false
while IFS= read -r line; do
    stripped=$(strip_ansi "$line")
    if [[ "$stripped" =~ ^║ ]]; then
        [[ ${#stripped} -eq 80 ]] \
            && { ok "print_amendments: error state row width 80"; amd_err_ok=true; } \
            || fail "print_amendments: error state row width 80" "${#stripped}" "80"
        break
    fi
done < <(print_amendments)
$amd_err_ok || fail "print_amendments: error state produced no rows" "" "at least one row"
AMD_ERROR=false

# Test 4: YES vote text present in data row
AMD_JSON='[{"name":"AMMClawback","vote":"yes","supported":true,"majority":false}]'
AMD_ERROR=false
found_yes=false
while IFS= read -r line; do
    stripped=$(strip_ansi "$line")
    if [[ "$stripped" =~ "AMMClawback" && "$stripped" =~ "YES" ]]; then
        ok "print_amendments: YES vote rendered"; found_yes=true; break
    fi
done < <(print_amendments)
$found_yes || fail "print_amendments: YES vote rendered" "" "row with AMMClawback + YES"

# Test 5: MAJORITY status present in data row
AMD_JSON='[{"name":"fixSomething","vote":"yes","supported":true,"majority":true}]'
AMD_ERROR=false
found_maj=false
while IFS= read -r line; do
    stripped=$(strip_ansi "$line")
    if [[ "$stripped" =~ "fixSomething" && "$stripped" =~ "MAJORITY" ]]; then
        ok "print_amendments: MAJORITY status rendered"; found_maj=true; break
    fi
done < <(print_amendments)
$found_maj || fail "print_amendments: MAJORITY status rendered" "" "row with fixSomething + MAJORITY"
```

- [ ] **Step 2: Run tests — verify new tests fail with "function not found"**

```bash
cd /home/hamsa/motd && bash motd-validator-test 2>&1 | tail -20
```

Expected: tests fail with something like `print_amendments: command not found` or similar. The old tests still pass.

---

### Task 2: Add globals and curl call to `collect_data()`

**Files:**
- Modify: `motd/motd-validator-render`

- [ ] **Step 1: Declare amendment globals near the top of the file**

  After the `# Alert globals (arrays)` block (around line 197), add:

```bash
# Amendment globals
METRICS_JSON='{}'
AMD_JSON='[]'
AMD_ERROR=false
```

- [ ] **Step 2: Add curl fetch at the end of `collect_data()`**

  At the end of `collect_data()`, before the closing `}`, add:

```bash
    # ── Amendments: fetch from metrics server ──
    local _metrics
    _metrics=$(curl -s --max-time 3 http://localhost:8080/metrics 2>/dev/null) || true
    if [[ -z "$_metrics" ]]; then
        AMD_ERROR=true
    else
        METRICS_JSON="$_metrics"
        AMD_JSON=$(printf '%s' "$METRICS_JSON" | jq -c '.amendments // []' 2>/dev/null) \
            || AMD_ERROR=true
    fi
```

- [ ] **Step 3: Run existing tests — verify they still pass**

```bash
cd /home/hamsa/motd && bash motd-validator-test 2>&1 | grep -E "^(Results|  FAIL)"
```

Expected: `Results: N passed, 0 failed` (same count as before this task).

---

### Task 3: Implement `print_amendments()`

**Files:**
- Modify: `motd/motd-validator-render`

- [ ] **Step 1: Add the function before `print_identity()`**

  In `motd/motd-validator-render`, insert this function immediately before `print_identity()`:

```bash
print_amendments() {
    box_mid

    if [[ "$AMD_ERROR" == "true" ]]; then
        box_row "$(rpad "  ${YELLOW}◈ AMENDMENTS${RESET}" $(( 78 + ${#YELLOW} + ${#RESET} )))"
        box_row "$(rpad "    ${DIM}amendments unavailable${RESET}" $(( 78 + ${#DIM} + ${#RESET} )))"
        return
    fi

    local count
    count=$(printf '%s' "$AMD_JSON" | jq 'length' 2>/dev/null || echo '0')

    # Centered section header with ─ dashes
    local label="PENDING AMENDMENTS (${count})"
    local label_len=${#label}
    local dashes=$(( 78 - label_len - 2 ))
    local left=$(( dashes / 2 ))
    local right=$(( dashes - left ))
    local hdash_l="" hdash_r="" _i
    for (( _i=0; _i<left;  _i++ )); do hdash_l+="─"; done
    for (( _i=0; _i<right; _i++ )); do hdash_r+="─"; done
    box_row "${CYAN}${hdash_l} ${label} ${hdash_r}${RESET}"

    if [[ "$count" == "0" ]]; then
        box_row "$(rpad "    ${DIM}no pending amendments${RESET}" $(( 78 + ${#DIM} + ${#RESET} )))"
        return
    fi

    # Column headers
    box_row "$(rpad "  Name                               Vote  Status" 78)"
    local div="" 
    for (( _i=0; _i<73; _i++ )); do div+="─"; done
    box_row "$(rpad "  ${DIM}${div}${RESET}" $(( 78 + ${#DIM} + ${#RESET} )))"

    # Data rows — AMD_JSON is already sorted (majority first, then alpha) by metrics server
    while IFS= read -r entry; do
        local name vote supported majority
        name=$(printf '%s' "$entry"     | jq -r '.name      // ""')
        vote=$(printf '%s' "$entry"     | jq -r '.vote      // "no"')
        supported=$(printf '%s' "$entry" | jq -r '.supported // false')
        majority=$(printf '%s' "$entry"  | jq -r '.majority  // false')

        # Name: truncate to 35 chars, pad to 35
        local name_padded
        name_padded=$(rpad "${name:0:35}" 35)

        # Vote: 3 visible chars, colored
        local vote_str vote_color
        if [[ "$vote" == "yes" ]]; then
            vote_str="YES"; vote_color="$GREEN"
        else
            vote_str="NO "; vote_color="$RED"
        fi
        local vote_padded
        vote_padded=$(rpad "${vote_color}${vote_str}${RESET}" \
            $(( 3 + ${#vote_color} + ${#RESET} )))

        # Status: 33 visible chars, colored
        local status_str status_color
        if [[ "$majority" == "true" ]]; then
            status_str="★ MAJORITY"
            status_color="$AMBER"
        elif [[ "$supported" == "false" ]]; then
            status_str="pending · unsupported"
            status_color="$DIM"
        else
            status_str="pending"
            status_color="$DIM"
        fi
        local status_padded
        status_padded=$(rpad "${status_color}${status_str}${RESET}" \
            $(( 33 + ${#status_color} + ${#RESET} )))

        box_row "  ${name_padded}  ${vote_padded}   ${status_padded}"
    done < <(printf '%s' "$AMD_JSON" | jq -c '.[]' 2>/dev/null)
}
```

- [ ] **Step 2: Run tests — verify amendments tests now pass**

```bash
cd /home/hamsa/motd && bash motd-validator-test
```

Expected: `Results: N passed, 0 failed` — amendments tests passing alongside all existing tests.

---

### Task 4: Wire `print_amendments()` into `main()`

**Files:**
- Modify: `motd/motd-validator-render`

- [ ] **Step 1: Add call between `print_alerts` and `print_identity` in `main()`**

  Find this block in `main()`:

```bash
        print_alerts
        print_identity
```

  Replace with:

```bash
        print_alerts
        print_amendments
        print_identity
```

- [ ] **Step 2: Run full test suite to confirm no regressions**

```bash
cd /home/hamsa/motd && bash motd-validator-test
```

Expected: `Results: N passed, 0 failed`.

---

### Task 5: Visual check and commit

**Files:** none new

- [ ] **Step 1: Do a dry-run render (metrics server must be running)**

```bash
cd /home/hamsa/motd && sudo bash motd-validator-render && cat /var/cache/motd-validator
```

  Verify visually:
  - Amendments section appears between Alerts and Identity
  - Section header is visibly centered (dashes on both sides)
  - `YES` is green, `NO` is red
  - `★ MAJORITY` rows appear before plain `pending` rows
  - Box borders on the right are flush (no ragged edges)
  - If metrics server is down: `amendments unavailable` shows, rest of MOTD renders

- [ ] **Step 2: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render motd/motd-validator-test
git commit -m "feat: add amendments voting section to MOTD

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
