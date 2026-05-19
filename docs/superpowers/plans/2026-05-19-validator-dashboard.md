# XRPL Validator Login Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a cyberpunk ASCII MOTD dashboard showing live XRPL validator and system stats on every SSH login, rendered from cache for instant display.

**Architecture:** A bash render script (`/usr/local/bin/motd-validator-render`) is called by a cron job every 60 seconds as root. It fetches live data from `rippled` and `/proc`, builds an ANSI-colored 80-column dashboard, and writes it atomically to `/var/cache/motd-validator`. At login, `/etc/update-motd.d/10-validator-dashboard` simply cats that file. All default Ubuntu MOTD scripts are disabled.

**Tech Stack:** Bash 5.1, jq 1.6, `/usr/local/bin/rippled`, /proc filesystem, journalctl, ss, df

---

## File Map

| Path | Action | Purpose |
|---|---|---|
| `/home/hamsa/motd/motd-validator-render` | Create | Render script source (git-tracked) |
| `/home/hamsa/motd/motd-validator-test` | Create | Test harness (git-tracked) |
| `/usr/local/bin/motd-validator-render` | Symlink | System-installed render script |
| `/usr/local/bin/motd-validator-test` | Symlink | System-installed test script |
| `/var/cache/motd-validator` | Auto-created | Pre-rendered ANSI dashboard |
| `/etc/cron.d/motd-validator` | Create | Runs render every 60s as root |
| `/etc/update-motd.d/10-validator-dashboard` | Create | MOTD entry: cats cache |
| `/etc/update-motd.d/00-header` … `98-reboot-required` | chmod -x | Disable all default scripts |

---

### Task 1: Script skeleton + constants + helper functions

**Files:**
- Create: `/home/hamsa/motd/motd-validator-render`
- Create: `/home/hamsa/motd/motd-validator-test`

- [ ] **Step 1: Write the failing helper tests**

Create `/home/hamsa/motd/motd-validator-test`:

```bash
#!/usr/bin/env bash
# Test harness — sources the render script to test helper functions in isolation.
export LC_ALL=en_US.UTF-8
set -euo pipefail

# Prevent main() from running when we source the render script
_SOURCED=1
# shellcheck source=/home/hamsa/motd/motd-validator-render
source "$(dirname "$0")/motd-validator-render"

PASS=0; FAIL=0
ok()   { echo "  PASS: $1"; (( PASS++ )) || true; }
fail() { echo "  FAIL: $1 — got: $(printf '%q' "$2")  want: $(printf '%q' "$3")"; (( FAIL++ )) || true; }
strip_ansi() { printf '%s' "$1" | sed 's/\x1b\[[0-9;]*m//g'; }

echo "=== motd-validator-render helpers ==="

# rpad
r=$(rpad "hi" 5)
[[ "$r" == "hi   " ]]  && ok "rpad: pads to width"     || fail "rpad: pads to width"     "$r" "hi   "
[[ ${#r} -eq 5 ]]      && ok "rpad: length correct"    || fail "rpad: length correct"    "${#r}" "5"

r=$(rpad "toolong" 4)
[[ "$r" == "tool" ]]   && ok "rpad: truncates"         || fail "rpad: truncates"         "$r" "tool"

r=$(rpad "" 3)
[[ ${#r} -eq 3 ]]      && ok "rpad: empty string"      || fail "rpad: empty string"      "${#r}" "3"

# center_str
c=$(center_str "hi" 10)
[[ ${#c} -eq 10 ]]     && ok "center_str: length"      || fail "center_str: length"      "${#c}" "10"
[[ "$c" == "    hi    " ]] && ok "center_str: content"     || fail "center_str: content"     "$c" "    hi    "

# progress_bar (strip ANSI to check visible chars)
b=$(strip_ansi "$(progress_bar 0)")
[[ "$b" == "░░░░░░░░░░" ]]  && ok "progress_bar: 0%"   || fail "progress_bar: 0%"   "$b" "░░░░░░░░░░"

b=$(strip_ansi "$(progress_bar 70)")
[[ "$b" == "███████░░░" ]]  && ok "progress_bar: 70%"  || fail "progress_bar: 70%"  "$b" "███████░░░"

b=$(strip_ansi "$(progress_bar 100)")
[[ "$b" == "██████████" ]]  && ok "progress_bar: 100%" || fail "progress_bar: 100%" "$b" "██████████"

[[ ${#b} -eq 10 ]]          && ok "progress_bar: length" || fail "progress_bar: length" "${#b}" "10"

# format_uptime
u=$(format_uptime 90061)
[[ "$u" == "1d 01h 01m" ]]  && ok "format_uptime: 1d1h1m"  || fail "format_uptime: 1d1h1m"  "$u" "1d 01h 01m"

u=$(format_uptime 3600)
[[ "$u" == "0d 01h 00m" ]]  && ok "format_uptime: 1h"       || fail "format_uptime: 1h"       "$u" "0d 01h 00m"

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
[[ $FAIL -eq 0 ]]
```

- [ ] **Step 2: Run the test — expect it to fail (script not yet created)**

```bash
bash /home/hamsa/motd/motd-validator-test 2>&1 | head -5
```

Expected: error like `No such file or directory` — render script does not exist yet.

- [ ] **Step 3: Create the render script skeleton**

Create `/home/hamsa/motd/motd-validator-render`:

```bash
#!/usr/bin/env bash
# XRPL Validator MOTD Renderer
# Writes a pre-rendered ANSI dashboard to /var/cache/motd-validator.
# Designed to be called by cron every 60s as root.
set -euo pipefail
export LC_ALL=en_US.UTF-8
export PATH="/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin"

VALIDATOR_JSON=""
CACHE_FILE="/var/cache/motd-validator"
CACHE_TMP="${CACHE_FILE}.tmp"

# ── ANSI color codes ──────────────────────────────────────────────────────────
RESET=$'\e[0m'
CYAN=$'\e[96m'
MAGENTA=$'\e[35m'
YELLOW=$'\e[93m'
GREEN=$'\e[92m'
RED=$'\e[91m'
AMBER=$'\e[33m'
WHITE=$'\e[37m'
DIM=$'\e[2m'

# ── Layout ────────────────────────────────────────────────────────────────────
LEFT_COL=38    # visible inner width of left column
RIGHT_COL=39   # visible inner width of right column
# Box total: ║(1) + LEFT_COL(38) + ╬(1) + RIGHT_COL(39) + ║(1) = 80

# ── Helper functions ──────────────────────────────────────────────────────────

# Right-pad a plain-text string to exactly N visible chars.
# Do NOT pass strings containing ANSI codes — measure before coloring.
rpad() {
    local str="$1" width="$2"
    local len=${#str}
    local pad=$(( width - len ))
    if (( pad <= 0 )); then
        printf '%s' "${str:0:$width}"
    else
        printf '%s%*s' "$str" "$pad" ""
    fi
}

# Center a plain-text string in N visible chars.
center_str() {
    local str="$1" width="$2"
    local len=${#str}
    local lp=$(( (width - len) / 2 ))
    local rp=$(( width - len - lp ))
    (( lp < 0 )) && lp=0
    (( rp < 0 )) && rp=0
    printf '%*s%s%*s' "$lp" "" "$str" "$rp" ""
}

# 10-char progress bar with color based on percentage.
progress_bar() {
    local pct="$1"
    local filled=$(( pct * 10 / 100 ))
    (( filled > 10 )) && filled=10
    local empty=$(( 10 - filled ))
    local bar="" i
    local color
    if   (( pct >= 81 )); then color="$RED"
    elif (( pct >= 61 )); then color="$AMBER"
    else                       color="$GREEN"
    fi
    for (( i=0; i<filled; i++ )); do bar+="█"; done
    for (( i=0; i<empty;  i++ )); do bar+="░"; done
    printf '%s%s%s' "$color" "$bar" "$RESET"
}

# Format /proc/uptime seconds as "Xd XXh XXm".
format_uptime() {
    local secs="${1%.*}"
    local d=$(( secs / 86400 ))
    local h=$(( (secs % 86400) / 3600 ))
    local m=$(( (secs % 3600) / 60 ))
    printf '%dd %02dh %02dm' "$d" "$h" "$m"
}

main() {
    echo "skeleton ok"
}

[[ -n "${_SOURCED:-}" ]] || main "$@"
```

- [ ] **Step 4: Run the test — all helpers should pass**

```bash
chmod +x /home/hamsa/motd/motd-validator-render /home/hamsa/motd/motd-validator-test
bash /home/hamsa/motd/motd-validator-test
```

Expected output:
```
=== motd-validator-render helpers ===
  PASS: rpad: pads to width
  PASS: rpad: length correct
  PASS: rpad: truncates
  PASS: rpad: empty string
  PASS: center_str: length
  PASS: center_str: content
  PASS: progress_bar: 0%
  PASS: progress_bar: 70%
  PASS: progress_bar: 100%
  PASS: progress_bar: length
  PASS: format_uptime: 1d1h1m
  PASS: format_uptime: 1h

Results: 12 passed, 0 failed
```

- [ ] **Step 5: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render motd/motd-validator-test
git commit -m "feat: add render script skeleton and test harness"
```

---

### Task 2: Box drawing primitives

**Files:**
- Modify: `/home/hamsa/motd/motd-validator-render` (add box functions after helpers)

- [ ] **Step 1: Add box width test to the test harness**

Add to `/home/hamsa/motd/motd-validator-test` (before the `echo "Results"` line):

```bash
# box_row: full-width single-column row must be exactly 80 visible chars
strip_ansi() { printf '%s' "$1" | sed 's/\x1b\[[0-9;]*m//g'; }

r=$(strip_ansi "$(box_row "$(rpad "hello" 78)")")
[[ ${#r} -eq 80 ]] && ok "box_row: total width 80"   || fail "box_row: total width 80"   "${#r}" "80"
[[ "${r:0:1}" == "║" ]] && ok "box_row: starts with ║" || fail "box_row: starts with ║" "${r:0:1}" "║"
[[ "${r: -1}" == "║" ]] && ok "box_row: ends with ║"   || fail "box_row: ends with ║"   "${r: -1}" "║"

r=$(strip_ansi "$(box_row2 "$(rpad "left" 38)" "$(rpad "right" 39)")")
[[ ${#r} -eq 80 ]] && ok "box_row2: total width 80"  || fail "box_row2: total width 80"  "${#r}" "80"

r=$(strip_ansi "$(box_top)")
[[ ${#r} -eq 80 ]] && ok "box_top: total width 80"   || fail "box_top: total width 80"   "${#r}" "80"
```

- [ ] **Step 2: Run the test — expect failures for missing box functions**

```bash
bash /home/hamsa/motd/motd-validator-test 2>&1 | grep -E 'FAIL|Results'
```

Expected: FAILs for box_row, box_row2, box_top.

- [ ] **Step 3: Add box primitives to the render script**

Add after the `format_uptime` function in `/home/hamsa/motd/motd-validator-render`:

```bash
# ── Pre-built border strings (computed once at source time) ───────────────────
_hline() { local n="$1" s=""; for (( i=0; i<n; i++ )); do s+="═"; done; printf '%s' "$s"; }

_H78=$(_hline 78)
_H38=$(_hline 38)
_H39=$(_hline 39)

_BOX_TOP="╔${_H78}╗"
_BOX_BOT="╚${_H78}╝"
_BOX_MID="╠${_H78}╣"
_BOX_SPL="╠${_H38}╦${_H39}╣"   # open two-column split
_BOX_JOI="╠${_H38}╬${_H39}╣"   # mid two-column join
_BOX_CLS="╠${_H38}╩${_H39}╣"   # close two-column (╩ rejoins)

box_top()     { printf '%s%s%s\n' "$CYAN" "$_BOX_TOP" "$RESET"; }
box_bottom()  { printf '%s%s%s\n' "$CYAN" "$_BOX_BOT" "$RESET"; }
box_mid()     { printf '%s%s%s\n' "$CYAN" "$_BOX_MID" "$RESET"; }
box_split()   { printf '%s%s%s\n' "$CYAN" "$_BOX_SPL" "$RESET"; }
box_join()    { printf '%s%s%s\n' "$CYAN" "$_BOX_JOI" "$RESET"; }
box_close()   { printf '%s%s%s\n' "$CYAN" "$_BOX_CLS" "$RESET"; }

# Single-column content row. $1 must be exactly 78 visible chars (no ANSI).
# Wrap $1 in color codes BEFORE calling if needed; padding must be plain.
box_row() {
    printf '%s║%s%s%s║%s\n' "$CYAN" "$RESET" "$1" "$CYAN" "$RESET"
}

# Two-column content row. $1=38 visible chars, $2=39 visible chars (plain or pre-colored).
box_row2() {
    printf '%s║%s%s%s║%s%s%s║%s\n' \
        "$CYAN" "$RESET" "$1" "$CYAN" "$RESET" "$2" "$CYAN" "$RESET"
}
```

- [ ] **Step 4: Run tests — all should pass**

```bash
bash /home/hamsa/motd/motd-validator-test
```

Expected: 17 passed, 0 failed (12 helpers + 5 box tests).

- [ ] **Step 5: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render motd/motd-validator-test
git commit -m "feat: add box drawing primitives and tests"
```

---

### Task 3: LFG XRPL ASCII banner + header section

**Files:**
- Modify: `/home/hamsa/motd/motd-validator-render` (add `print_header` function)

- [ ] **Step 1: Add banner test to the test harness**

Add to `/home/hamsa/motd/motd-validator-test` (before `echo "Results"`):

```bash
# Each banner row must be exactly 59 visible chars
for i in "${!_BANNER[@]}"; do
    row="${_BANNER[$i]}"
    len=${#row}
    [[ $len -eq 59 ]] && ok "banner row $i: 59 chars" || fail "banner row $i: 59 chars" "$len" "59"
done
```

- [ ] **Step 2: Run the test — expect FAILs for missing _BANNER**

```bash
bash /home/hamsa/motd/motd-validator-test 2>&1 | grep -E 'FAIL|Results'
```

Expected: FAILs for all 6 banner row tests.

- [ ] **Step 3: Add banner data and print_header to the render script**

Add after the box primitives in `/home/hamsa/motd/motd-validator-render`:

```bash
# ── LFG XRPL banner (figlet block font, 59 chars wide, 6 rows) ───────────────
# Each string is exactly 59 visible chars: LFG(25) + sep(2) + XRPL(32)
_BANNER=(
    "██╗     ███████╗ ██████╗   ██╗  ██╗██████╗ ██████╗ ██╗     "
    "██║     ██╔════╝██╔════╝   ╚██╗██╔╝██╔══██╗██╔══██╗██║     "
    "██║     █████╗  ██║  ███╗   ╚███╔╝ ██████╔╝██████╔╝██║     "
    "██║     ██╔══╝  ██║   ██║   ██╔██╗ ██╔══██╗██╔═══╝ ██║     "
    "███████╗██║     ╚██████╔╝  ██╔╝ ██╗██║  ██║██║     ███████╗"
    "╚══════╝╚═╝      ╚═════╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚══════╝"
)
# Centering: (78 - 59) / 2 = 9 left pad, 10 right pad
_BANNER_LPAD="         "   # 9 spaces
_BANNER_RPAD="          "  # 10 spaces

print_header() {
    local pubkey="unknown" manifest_seq="?" domain="joshuahamsa.com"

    if [[ -n "$VALIDATOR_JSON" && -f "$VALIDATOR_JSON" ]]; then
        pubkey=$(jq -r '.public_key // "unknown"' "$VALIDATOR_JSON" 2>/dev/null)
        manifest_seq=$(jq -r '.token_sequence // "?"' "$VALIDATOR_JSON" 2>/dev/null)
        domain=$(jq -r '.domain // "joshuahamsa.com"' "$VALIDATOR_JSON" 2>/dev/null)
    fi

    local key_short="${pubkey:0:6}···${pubkey: -4}"

    box_top
    local row
    for row in "${_BANNER[@]}"; do
        # 9 spaces (plain) + colored banner + 10 spaces (plain) = 78 visible chars
        box_row "${_BANNER_LPAD}${MAGENTA}${row}${RESET}${_BANNER_RPAD}"
    done

    # Subtitle: "▸▸  domain  ·  key  ·  Manifest #N  ◂◂" centered in 78
    local plain_sub="▸▸  ${domain}  ·  ${key_short}  ·  Manifest #${manifest_seq}  ◂◂"
    local sub_len=${#plain_sub}
    local sub_lp=$(( (78 - sub_len) / 2 ))
    local sub_rp=$(( 78 - sub_len - sub_lp ))
    local lpad rpad
    printf -v lpad '%*s' "$sub_lp" ""
    printf -v rpad '%*s' "$sub_rp" ""
    box_row "${lpad}${WHITE}${plain_sub}${RESET}${rpad}"
}
```

- [ ] **Step 4: Run tests — all should pass**

```bash
bash /home/hamsa/motd/motd-validator-test
```

Expected: 23 passed, 0 failed.

- [ ] **Step 5: Smoke-test the header visually**

Temporarily add `print_header` to `main()`, run it, and eyeball the output:

```bash
# In main(), replace "echo skeleton ok" with: print_header
# Then:
VALIDATOR_JSON=$(ls /home/hamsa/.ripple/*.json 2>/dev/null | head -1)
bash /home/hamsa/motd/motd-validator-render | cat
```

Expected: 8-line colored box with "LFG XRPL" banner and subtitle. Check borders align.

- [ ] **Step 6: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render motd/motd-validator-test
git commit -m "feat: add LFG XRPL banner and header section"
```

---

### Task 4: Data collection functions

**Files:**
- Modify: `/home/hamsa/motd/motd-validator-render` (add `collect_data` function)

All data is fetched once in `collect_data()` and stored in global variables used by the section renderers. This keeps rippled calls and /proc reads centralized.

- [ ] **Step 1: Add data collection to the render script**

Add after `print_header` in `/home/hamsa/motd/motd-validator-render`:

```bash
# ── Data collection (called once in main, sets globals) ───────────────────────

# Validator / rippled globals
RPC_STATE="OFFLINE"
RPC_LEDGER_SEQ="—"
RPC_LEDGER_AGE="—"
RPC_LOAD="—"
RPC_PEERS="—"
RPC_PEER_DISC="—"
RPC_UPTIME_S="0"
RPC_AMENDMENT_BLOCKED="false"

# System globals
SYS_CPU_PCT=0
SYS_RAM_USED_GB="—"
SYS_RAM_TOTAL_GB="—"
SYS_RAM_PCT=0
SYS_DISK_USED="—"
SYS_DISK_TOTAL="—"
SYS_DISK_PCT=0
SYS_UPTIME="—"

# Network globals
NET_LAN="—"
NET_TAILSCALE="—"
NET_SSH_SESSIONS=0
NET_P2P_STATUS="CLOSED"

# Alert globals (arrays)
ALERT_LINES=()

collect_data() {
    # ── rippled ──
    local rpc_json
    if rpc_json=$(timeout 5 /usr/local/bin/rippled server_info 2>/dev/null); then
        RPC_STATE=$(printf '%s' "$rpc_json"   | jq -r '.result.info.server_state    // "unknown"')
        RPC_LEDGER_SEQ=$(printf '%s' "$rpc_json" | jq -r '.result.info.validated_ledger.seq // "—"')
        RPC_LEDGER_AGE=$(printf '%s' "$rpc_json" | jq -r '.result.info.validated_ledger.age // "—"')
        RPC_LOAD=$(printf '%s' "$rpc_json"    | jq -r '.result.info.load_factor      // "—"')
        RPC_PEERS=$(printf '%s' "$rpc_json"   | jq -r '.result.info.peers            // "—"')
        RPC_PEER_DISC=$(printf '%s' "$rpc_json" | jq -r '.result.info.peer_disconnects // "—"')
        RPC_UPTIME_S=$(printf '%s' "$rpc_json" | jq -r '.result.info.uptime          // "0"')
        local ab
        ab=$(printf '%s' "$rpc_json" | jq -r '.result.info.amendment_blocked // false')
        RPC_AMENDMENT_BLOCKED="$ab"
    fi

    # ── CPU: 1-min load / core count as percent ──
    local load_avg cores
    load_avg=$(awk '{print $1}' /proc/loadavg)
    cores=$(grep -c '^processor' /proc/cpuinfo)
    SYS_CPU_PCT=$(awk "BEGIN { v=int(($load_avg/$cores)*100); print (v>100?100:v) }")

    # ── RAM ──
    local total_kb avail_kb used_kb
    total_kb=$(awk '/^MemTotal:/{print $2}'     /proc/meminfo)
    avail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
    used_kb=$(( total_kb - avail_kb ))
    SYS_RAM_PCT=$(( used_kb * 100 / total_kb ))
    SYS_RAM_USED_GB=$(awk "BEGIN { printf \"%.1f\", $used_kb/1048576 }")
    SYS_RAM_TOTAL_GB=$(awk "BEGIN { printf \"%.0f\",  $total_kb/1048576 }")

    # ── Disk ──
    read -r _ total used _ pct _ < <(df -h / | tail -1)
    SYS_DISK_TOTAL="$total"
    SYS_DISK_USED="$used"
    SYS_DISK_PCT="${pct%%%}"   # strip trailing %

    # ── Uptime ──
    local up_secs
    up_secs=$(awk '{print $1}' /proc/uptime)
    SYS_UPTIME=$(format_uptime "$up_secs")

    # ── Network ──
    NET_LAN=$(ip route get 8.8.8.8 2>/dev/null | grep -oP 'src \K[^ ]+' || echo "—")
    # Tailscale uses the 100.x.x.x range
    NET_TAILSCALE=$(ip addr show 2>/dev/null \
        | grep -oP '(?<=inet )100\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "—")
    NET_SSH_SESSIONS=$(ss -tnp 2>/dev/null | grep -c 'ESTAB.*:22' || echo 0)
    if ss -tlnp 2>/dev/null | grep -q ':51235'; then
        NET_P2P_STATUS="OPEN"
    else
        NET_P2P_STATUS="CLOSED"
    fi

    # ── Alerts: last 3 notable rippled log lines ──
    mapfile -t ALERT_LINES < <(
        journalctl -u rippled -n 60 --no-pager -o short-iso 2>/dev/null \
        | grep -iE 'warn|error|amendment|consensus lost|slow ledger' \
        | tail -3 \
        | awk '{
            # Extract HH:MM from ISO timestamp (field 1), rest is message
            ts = substr($1, 12, 5)
            $1=""; sub(/^ /, "")
            # strip hostname and process field
            sub(/[^ ]+ rippled\[[0-9]+\]: /, "")
            printf "[%s] %s\n", ts, substr($0, 1, 60)
          }'
    ) || true
}
```

- [ ] **Step 2: Wire collect_data into main() and smoke-test**

Replace `main()` body with:

```bash
main() {
    VALIDATOR_JSON=$(ls /home/hamsa/.ripple/*.json 2>/dev/null | head -1 || true)
    collect_data
    echo "STATE=$RPC_STATE PEERS=$RPC_PEERS CPU=${SYS_CPU_PCT}% RAM=${SYS_RAM_PCT}%"
    echo "LAN=$NET_LAN TAILSCALE=$NET_TAILSCALE SSH=$NET_SSH_SESSIONS P2P=$NET_P2P_STATUS"
    echo "UPTIME=$SYS_UPTIME DISK=${SYS_DISK_USED}/${SYS_DISK_TOTAL}"
    for line in "${ALERT_LINES[@]:-}"; do echo "ALERT: $line"; done
}
```

Run it:

```bash
sudo bash /home/hamsa/motd/motd-validator-render
```

Expected output (values will vary):
```
STATE=full PEERS=8 CPU=3% RAM=52%
LAN=10.30.0.176 TAILSCALE=100.86.141.2 SSH=2 P2P=OPEN
UPTIME=13d 01h 07m DISK=130G/455G
ALERT: [14:23] 2026-May-19 14:23:01.000 WRNr Slow ledger close...
```

- [ ] **Step 3: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render
git commit -m "feat: add data collection function"
```

---

### Task 5: Validator + System section (two-column)

**Files:**
- Modify: `/home/hamsa/motd/motd-validator-render` (add `print_validator_system`)

- [ ] **Step 1: Add section test to the test harness**

Add to `/home/hamsa/motd/motd-validator-test` (before `echo "Results"`):

```bash
# Section rows must produce exactly 80 visible chars
strip_ansi() { printf '%s' "$1" | sed 's/\x1b\[[0-9;]*m//g'; }

# Simulate data so we can call the section function
RPC_STATE="full"; RPC_LEDGER_SEQ="104325241"; RPC_LEDGER_AGE="3"
RPC_LOAD="1"; RPC_PEERS="8"; RPC_PEER_DISC="5"; RPC_AMENDMENT_BLOCKED="false"
SYS_CPU_PCT=45; SYS_RAM_PCT=52; SYS_RAM_USED_GB="33.2"; SYS_RAM_TOTAL_GB="62"
SYS_DISK_USED="130G"; SYS_DISK_TOTAL="455G"; SYS_DISK_PCT=30; SYS_UPTIME="13d 01h 07m"

# Capture output and check each line is 80 chars
while IFS= read -r line; do
    stripped=$(strip_ansi "$line")
    len=${#stripped}
    if [[ "$stripped" =~ ^[╔╠╚║] ]]; then
        [[ $len -eq 80 ]] \
            && ok "validator+system: row width 80" \
            || fail "validator+system: row width 80" "$len" "80"
    fi
done < <(print_validator_system)
```

- [ ] **Step 2: Add print_validator_system to the render script**

Add after `collect_data` in `/home/hamsa/motd/motd-validator-render`:

```bash
print_validator_system() {
    # ── Left column: Validator ────────────────────────────────────────────────
    local state_color="$GREEN"
    [[ "$RPC_STATE" != "full" ]] && state_color="$RED"
    local amend_warn=""
    [[ "$RPC_AMENDMENT_BLOCKED" == "true" ]] && amend_warn=" ${RED}[AMENDMENT BLOCKED]${RESET}"

    # State progress bar — all filled when FULL, else empty
    local state_bar
    if [[ "$RPC_STATE" == "full" ]]; then
        state_bar=$(printf '%s%s%s' "$GREEN" "▓▓▓▓▓▓▓▓▓▓" "$RESET")
    else
        state_bar=$(printf '%s%s%s' "$RED"   "░░░░░░░░░░" "$RESET")
    fi

    local state_plain="  ◈ VALIDATOR"
    local load_color="$GREEN"
    (( $(awk "BEGIN{print ($RPC_LOAD > 5)}") )) && load_color="$AMBER"
    (( $(awk "BEGIN{print ($RPC_LOAD > 20)}") )) && load_color="$RED"

    local age_color="$GREEN"
    (( RPC_LEDGER_AGE > 10 )) && age_color="$AMBER"
    (( RPC_LEDGER_AGE > 30 )) && age_color="$RED"

    # Format ledger seq with commas
    local seq_fmt
    seq_fmt=$(printf '%d' "$RPC_LEDGER_SEQ" 2>/dev/null \
        | sed ':a;s/\B[0-9]\{3\}\>/,&/;ta' || echo "$RPC_LEDGER_SEQ")

    # Label widths for left column (each label is 12 visible chars)
    local p1="    State   " p2="    Ledger  " p3="    Age     " p4="    Load    "
    # value fills rest of LEFT_COL=38; state row: 12(label) + 10(bar) + 1(space) + val = 38

    # ── Right column: System ──────────────────────────────────────────────────
    local cpu_bar ram_bar disk_bar
    cpu_bar=$(progress_bar "$SYS_CPU_PCT")
    ram_bar=$(progress_bar "$SYS_RAM_PCT")
    disk_bar=$(progress_bar "$SYS_DISK_PCT")

    local rp1="    CPU   " rp2="    RAM   " rp3="    Disk  " rp4="    Up    "

    local cpu_val="${SYS_CPU_PCT}%"
    local ram_val="${SYS_RAM_USED_GB} / ${SYS_RAM_TOTAL_GB} GB"
    local dsk_val="${SYS_DISK_USED} / ${SYS_DISK_TOTAL}"

    # Right col: label(10) + bar(10) + "  " + value fills to RIGHT_COL=39
    local rv_width=$(( RIGHT_COL - 10 - 10 - 2 ))   # 17

    # ── Render ────────────────────────────────────────────────────────────────
    box_split
    box_row2 \
        "$(rpad "  ${YELLOW}◈ VALIDATOR${RESET}" $(( LEFT_COL + ${#YELLOW} + ${#RESET} )))" \
        "$(rpad "  ${YELLOW}◈ SYSTEM${RESET}"    $(( RIGHT_COL + ${#YELLOW} + ${#RESET} )))"
    box_row2 \
        "    State   ${state_bar} ${state_color}$(rpad "${RPC_STATE^^}" $(( LEFT_COL - 23 )))${RESET}" \
        "    CPU   ${cpu_bar}  ${GREEN}$(rpad "$cpu_val" $rv_width)${RESET}"
    box_row2 \
        "    Ledger  ${GREEN}$(rpad "$seq_fmt" $(( LEFT_COL - 12 )))${RESET}" \
        "    RAM   ${ram_bar}  ${GREEN}$(rpad "$ram_val" $rv_width)${RESET}"
    box_row2 \
        "    Age     ${age_color}$(rpad "${RPC_LEDGER_AGE}s" $(( LEFT_COL - 12 )))${RESET}" \
        "    Disk  ${disk_bar}  ${GREEN}$(rpad "$dsk_val" $rv_width)${RESET}"
    box_row2 \
        "    Load    ${load_color}$(rpad "${RPC_LOAD}x" $(( LEFT_COL - 12 )))${RESET}" \
        "    Up    ${GREEN}$(rpad "$SYS_UPTIME" $(( RIGHT_COL - 10 )))${RESET}"
}
```

**Note on width arithmetic:** `rpad` measures visible chars only. ANSI codes add no visible width. When building a cell that mixes plain labels with colored values, compute: `visible_chars_needed = column_width - visible_chars_already_used`, then `rpad value visible_chars_needed`.

- [ ] **Step 3: Update main() to render header + validator+system, then run**

```bash
main() {
    VALIDATOR_JSON=$(ls /home/hamsa/.ripple/*.json 2>/dev/null | head -1 || true)
    collect_data
    {
        print_header
        print_validator_system
    } | cat
}
```

```bash
sudo bash /home/hamsa/motd/motd-validator-render
```

Expected: colored box with LFG XRPL header + two-column validator/system section. Borders align on right side.

- [ ] **Step 4: Run the tests**

```bash
bash /home/hamsa/motd/motd-validator-test
```

Expected: all pass (width test verifies each box row is 80 chars).

- [ ] **Step 5: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render motd/motd-validator-test
git commit -m "feat: add validator + system two-column section"
```

---

### Task 6: Peers + Network section (two-column)

**Files:**
- Modify: `/home/hamsa/motd/motd-validator-render` (add `print_peers_network`)

Note: `rippled peers` fails on this node (internal error). Peer count and disconnect count come from `server_info` globals set in `collect_data`.

- [ ] **Step 1: Add print_peers_network to the render script**

Add after `print_validator_system` in `/home/hamsa/motd/motd-validator-render`:

```bash
print_peers_network() {
    local lw=$LEFT_COL rw=$RIGHT_COL

    # Peer color based on count
    local peer_color="$GREEN"
    (( RPC_PEERS < 4 ))  && peer_color="$AMBER"
    (( RPC_PEERS == 0 )) && peer_color="$RED"

    # P2P color
    local p2p_color="$GREEN" p2p_sym="✓"
    [[ "$NET_P2P_STATUS" != "OPEN" ]] && { p2p_color="$RED"; p2p_sym="✗"; }

    # Tailscale line — show dash if not available
    local ts_val="${NET_TAILSCALE:-—}"

    # Amendment blocked warning (if applicable, replaces one peer row)
    local amend_line="    Disconnects  ${AMBER}${RPC_PEER_DISC}${RESET}"

    box_join
    box_row2 \
        "$(rpad "  ${YELLOW}◈ PEERS${RESET}" $(( lw + ${#YELLOW} + ${#RESET} )))" \
        "$(rpad "  ${YELLOW}◈ NETWORK${RESET}" $(( rw + ${#YELLOW} + ${#RESET} )))"
    box_row2 \
        "    Connected  ${peer_color}$(rpad "${RPC_PEERS} peers" $(( lw - 15 )))${RESET}" \
        "    LAN        ${GREEN}$(rpad "$NET_LAN" $(( rw - 15 )))${RESET}"
    box_row2 \
        "$(rpad "${amend_line}" $(( lw + ${#AMBER} + ${#RESET} )))" \
        "    Tailscale  ${GREEN}$(rpad "$ts_val" $(( rw - 15 )))${RESET}"
    box_row2 \
        "    Uptime     ${GREEN}$(rpad "$(format_uptime "$RPC_UPTIME_S")" $(( lw - 15 )))${RESET}" \
        "    SSH        ${GREEN}$(rpad "${NET_SSH_SESSIONS} active" $(( rw - 15 )))${RESET}"
    box_row2 \
        "$(rpad "" $lw)" \
        "    P2P :51235 ${p2p_color}${p2p_sym} ${NET_P2P_STATUS}$(rpad "" $(( rw - 23 )))${RESET}"
}
```

- [ ] **Step 2: Add to main() and run visually**

```bash
main() {
    VALIDATOR_JSON=$(ls /home/hamsa/.ripple/*.json 2>/dev/null | head -1 || true)
    collect_data
    {
        print_header
        print_validator_system
        print_peers_network
    } | cat
}
```

```bash
sudo bash /home/hamsa/motd/motd-validator-render
```

Expected: three sections visible, borders aligned. P2P shows green ✓ OPEN.

- [ ] **Step 3: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render
git commit -m "feat: add peers + network two-column section"
```

---

### Task 7: Alerts + Identity + footer sections

**Files:**
- Modify: `/home/hamsa/motd/motd-validator-render` (add `print_alerts`, `print_identity`, `print_footer`)

- [ ] **Step 1: Add the three remaining sections**

Add after `print_peers_network` in `/home/hamsa/motd/motd-validator-render`:

```bash
print_alerts() {
    box_close   # closes the two-column split back to full width
    box_row "$(rpad "  ${YELLOW}◈ ALERTS${RESET}" $(( 78 + ${#YELLOW} + ${#RESET} )))"

    if [[ ${#ALERT_LINES[@]} -eq 0 ]]; then
        box_row "$(rpad "    ${DIM}No recent warnings or errors${RESET}" $(( 78 + ${#DIM} + ${#RESET} )))"
    else
        local line
        for line in "${ALERT_LINES[@]}"; do
            local color="$DIM"
            [[ "$line" =~ [Ww][Aa][Rr][Nn] ]] && color="$AMBER"
            [[ "$line" =~ [Ee][Rr][Rr][Oo][Rr] ]] && color="$RED"
            local plain="    ${line}"
            box_row "$(rpad "    ${color}${line}${RESET}" $(( 78 + ${#color} + ${#RESET} )))"
        done
    fi
}

print_identity() {
    local pubkey="unknown" revoked="—" domain="joshuahamsa.com" seq="?"
    if [[ -n "$VALIDATOR_JSON" && -f "$VALIDATOR_JSON" ]]; then
        pubkey=$(jq -r '.public_key // "unknown"' "$VALIDATOR_JSON" 2>/dev/null)
        revoked=$(jq -r '.revoked // false'       "$VALIDATOR_JSON" 2>/dev/null)
        domain=$(jq -r '.domain // "joshuahamsa.com"' "$VALIDATOR_JSON" 2>/dev/null)
        seq=$(jq -r '.token_sequence // "?"'      "$VALIDATOR_JSON" 2>/dev/null)
    fi

    local key_short="${pubkey:0:6}···${pubkey: -6}"
    local rev_color="$GREEN" rev_label="NO"
    [[ "$revoked" == "true" ]] && { rev_color="$RED"; rev_label="YES ⚠"; }

    local plain="  ◈ IDENTITY  Key: ${key_short}  ·  Domain: ${domain}  ·  Revoked: ${rev_label}"
    local plain_len=${#plain}
    local plain_no_ansi="  ◈ IDENTITY  Key: ${key_short}  ·  Domain: ${domain}  ·  Revoked: ${rev_label}"

    box_mid
    box_row "  ${YELLOW}◈ IDENTITY${RESET}  Key: ${WHITE}${key_short}${RESET}  ·  Domain: ${WHITE}${domain}${RESET}  ·  Revoked: ${rev_color}${rev_label}$(rpad "" $(( 78 - ${#plain_no_ansi} )))${RESET}"
}

print_footer() {
    box_bottom
    local now
    now=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    local cache_line="⟨ cached ${now} ⟩"
    printf '%s%s%s\n' "$DIM" "$(center_str "$cache_line" 80)" "$RESET"
}
```

- [ ] **Step 2: Update main() to render full dashboard**

```bash
main() {
    VALIDATOR_JSON=$(ls /home/hamsa/.ripple/*.json 2>/dev/null | head -1 || true)
    collect_data
    {
        print_header
        print_validator_system
        print_peers_network
        print_alerts
        print_identity
        print_footer
    } | cat
}
```

```bash
sudo bash /home/hamsa/motd/motd-validator-render
```

Expected: complete dashboard visible end-to-end with all sections.

- [ ] **Step 3: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render
git commit -m "feat: add alerts, identity, and footer sections"
```

---

### Task 8: Atomic write + graceful degradation

**Files:**
- Modify: `/home/hamsa/motd/motd-validator-render` (finalize `main()`, add offline fallback)

- [ ] **Step 1: Add offline fallback and atomic write to main()**

Replace the entire `main()` function:

```bash
main() {
    VALIDATOR_JSON=$(ls /home/hamsa/.ripple/*.json 2>/dev/null | head -1 || true)

    # Render to temp file, then atomically move to cache path
    {
        collect_data
        print_header
        print_validator_system
        print_peers_network
        print_alerts
        print_identity
        print_footer
    } > "$CACHE_TMP" 2>/dev/null || {
        # Fallback: write a minimal offline notice
        printf '%s\n' \
            "╔══════════════════════════════════════════════════════════════════════════════╗" \
            "║          ${RED}LFG XRPL VALIDATOR — DASHBOARD RENDER FAILED${RESET}                          ║" \
            "║          Check: sudo journalctl -u rippled -n 20                           ║" \
            "╚══════════════════════════════════════════════════════════════════════════════╝" \
            > "$CACHE_TMP"
    }

    mv "$CACHE_TMP" "$CACHE_FILE"
}
```

- [ ] **Step 2: Run full render and verify the cache file is written**

```bash
sudo bash /home/hamsa/motd/motd-validator-render
ls -lh /var/cache/motd-validator
cat /var/cache/motd-validator
```

Expected: file exists, dashboard renders cleanly to terminal.

- [ ] **Step 3: Verify all box lines are exactly 80 visible chars**

```bash
while IFS= read -r line; do
    stripped=$(printf '%s' "$line" | sed 's/\x1b\[[0-9;]*m//g')
    if [[ "$stripped" =~ ^[╔╠╚║] ]]; then
        w=${#stripped}
        [[ $w -ne 80 ]] && echo "BAD WIDTH $w: $stripped"
    fi
done < /var/cache/motd-validator
echo "Width check done (no output = all good)"
```

Expected: no "BAD WIDTH" lines printed.

- [ ] **Step 4: Commit**

```bash
cd /home/hamsa
git add motd/motd-validator-render
git commit -m "feat: add atomic write and offline fallback to main()"
```

---

### Task 9: Cron job + MOTD script

**Files:**
- Create: `/etc/cron.d/motd-validator`
- Create: `/etc/update-motd.d/10-validator-dashboard`

- [ ] **Step 1: Install the render script via symlink**

```bash
sudo ln -sf /home/hamsa/motd/motd-validator-render /usr/local/bin/motd-validator-render
sudo ln -sf /home/hamsa/motd/motd-validator-test    /usr/local/bin/motd-validator-test
```

- [ ] **Step 2: Create the cron job**

```bash
sudo tee /etc/cron.d/motd-validator > /dev/null <<'EOF'
# Refresh the MOTD validator dashboard every 60 seconds
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
* * * * * root /usr/local/bin/motd-validator-render
* * * * * root sleep 30 && /usr/local/bin/motd-validator-render
EOF
sudo chmod 644 /etc/cron.d/motd-validator
```

(Two entries offset by 30 seconds gives ~30s maximum staleness within the 60s cron resolution.)

- [ ] **Step 3: Create the MOTD entry script**

```bash
sudo tee /etc/update-motd.d/10-validator-dashboard > /dev/null <<'EOF'
#!/bin/sh
if [ -f /var/cache/motd-validator ]; then
    cat /var/cache/motd-validator
else
    /usr/local/bin/motd-validator-render
    cat /var/cache/motd-validator
fi
EOF
sudo chmod +x /etc/update-motd.d/10-validator-dashboard
```

(Falls back to on-demand render on first login before cron has run.)

- [ ] **Step 4: Verify cron syntax is accepted**

```bash
sudo crontab -l 2>/dev/null || true
sudo systemctl status cron | head -5
```

Expected: cron service is active.

- [ ] **Step 5: Commit**

```bash
cd /home/hamsa
git add motd/
git commit -m "feat: install cron job and MOTD entry script"
```

---

### Task 10: Disable existing MOTD scripts + end-to-end test

**Files:**
- Modify: all existing `/etc/update-motd.d/` scripts (chmod -x)

- [ ] **Step 1: Disable all default Ubuntu MOTD scripts**

```bash
sudo chmod -x \
    /etc/update-motd.d/00-header \
    /etc/update-motd.d/10-help-text \
    /etc/update-motd.d/50-landscape-sysinfo \
    /etc/update-motd.d/50-motd-news \
    /etc/update-motd.d/85-fwupd \
    /etc/update-motd.d/90-updates-available \
    /etc/update-motd.d/91-contract-ua-esm-status \
    /etc/update-motd.d/91-contract-ua-esm-status.dpkg-new \
    /etc/update-motd.d/91-release-upgrade \
    /etc/update-motd.d/92-unattended-upgrades \
    /etc/update-motd.d/95-hwe-eol \
    /etc/update-motd.d/97-overlayroot \
    /etc/update-motd.d/98-fsck-at-reboot \
    /etc/update-motd.d/98-reboot-required \
    2>/dev/null || true
```

- [ ] **Step 2: Confirm only the validator dashboard is executable**

```bash
ls -la /etc/update-motd.d/
```

Expected: only `10-validator-dashboard` has the executable bit set.

- [ ] **Step 3: Run the MOTD pipeline as pam_motd would**

```bash
sudo run-parts --lsbsysinit /etc/update-motd.d/ 2>/dev/null | cat
```

Expected: the cyberpunk dashboard and nothing else.

- [ ] **Step 4: Do a real SSH login test**

Open a new SSH session to the server:

```bash
ssh <your-user>@10.30.0.176
```

Expected: dashboard appears immediately at login prompt (cached), with no Ubuntu boilerplate.

- [ ] **Step 5: Wait 2 minutes and verify cron is refreshing the cache**

```bash
# Note the current cache timestamp
stat /var/cache/motd-validator
sleep 70
stat /var/cache/motd-validator
```

Expected: `Modify` time updates after ~30–60 seconds.

- [ ] **Step 6: Final commit**

```bash
cd /home/hamsa
git add motd/
git commit -m "feat: disable default MOTD scripts — validator dashboard is live"
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Dashboard not showing on login | `ls -la /etc/update-motd.d/` — is 10-validator-dashboard executable? |
| Empty dashboard | `sudo bash /usr/local/bin/motd-validator-render` — does it error? |
| rippled shows OFFLINE | `sudo systemctl status rippled` |
| Borders misaligned | Run the width check from Task 8 Step 3 |
| Cron not refreshing | `sudo grep motd-validator /var/log/syslog` |
