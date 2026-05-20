# Validator Amendments Voting Display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a live amendments voting box below the validator vitals dashboard, showing the validator's effective vote on every unenabled amendment, and fix the VALIDATOR/SYSTEM section header alignment.

**Architecture:** Parse `features.macro` once at server startup into a name→vote map. `get_amendments()` calls `rippled feature`, filters to unenabled, merges with the vote map and any config overrides, and returns a sorted list. The frontend renders a second terminal box using the same helpers as the existing vitals box. Section headers in the existing box are updated to center labels with dashes on both sides.

**Tech Stack:** Python 3.10, `http.server` (stdlib), `unittest` (stdlib), vanilla HTML/JS/CSS.

---

## File Map

**Server:**
- Modify: `/home/hamsa/motd/metrics_server.py` — add `FEATURES_MACRO`, `RIPPLED_CFG` constants; `_parse_vote_defaults()` called at module load; `_VOTE_DEFAULTS` dict; `_parse_cfg_overrides()` helper; `get_amendments()` function; wire into `collect_metrics()`
- Modify: `/home/hamsa/motd/test_metrics_server.py` — add `TestVoteDefaults` and `TestGetAmendments` classes; update `MOCK_METRICS` and `test_metrics_valid_json_top_level_keys`

**Deploy:**
- Create: `/home/hamsa/motd/install-amendments.sh` — adds sudoers entry for `rippled feature`, restarts service

**Frontend:**
- Modify: `~/joshuahamsa.github.io/validator.html` — fix VALIDATOR/SYSTEM headers; add `buildAmendmentLines()`; add `<pre id="amd-pre">`; update `renderData()`

---

## Task 1: Write failing tests for vote defaults and get_amendments

**Files:**
- Modify: `/home/hamsa/motd/test_metrics_server.py`

- [ ] **Step 1: Add MOCK_AMENDMENTS_FEATURE_JSON and MOCK_MACRO fixtures, and two new test classes after TestGetAlerts**

In `test_metrics_server.py`, add the following block immediately before `if __name__ == "__main__":`:

```python
MOCK_AMENDMENTS_FEATURE_JSON = json.dumps({
    "result": {
        "features": {
            "AAAA0001": {"enabled": True,  "name": "AlreadyEnabled", "supported": True},
            "BBBB0002": {"enabled": True,  "name": "AlsoEnabled",    "supported": True},
            "CCCC0003": {"enabled": False, "name": "HasMajority",    "supported": True,
                         "majority": {"since": 99999}},
            "DDDD0004": {"enabled": False, "name": "NoPriority",     "supported": True},
            "EEEE0005": {"enabled": False, "name": "NotSupported",   "supported": False},
        }
    }
})

MOCK_MACRO_SOURCE = """
XRPL_FEATURE(HasMajority, Supported::yes, VoteBehavior::DefaultYes)
XRPL_FEATURE(NoPriority,  Supported::yes, VoteBehavior::DefaultNo)
XRPL_FEATURE(NotSupported, Supported::no, VoteBehavior::DefaultNo)
"""


class TestVoteDefaults(unittest.TestCase):
    def test_parses_default_yes(self):
        with patch("builtins.open", mock_open(read_data=MOCK_MACRO_SOURCE)):
            result = metrics_server._parse_vote_defaults()
        self.assertEqual(result["HasMajority"], "yes")

    def test_parses_default_no(self):
        with patch("builtins.open", mock_open(read_data=MOCK_MACRO_SOURCE)):
            result = metrics_server._parse_vote_defaults()
        self.assertEqual(result["NoPriority"], "no")

    def test_returns_empty_dict_on_missing_file(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = metrics_server._parse_vote_defaults()
        self.assertEqual(result, {})


class TestGetAmendments(unittest.TestCase):
    def setUp(self):
        self._orig = metrics_server._VOTE_DEFAULTS.copy()
        metrics_server._VOTE_DEFAULTS.clear()
        metrics_server._VOTE_DEFAULTS.update({
            "HasMajority": "yes",
            "NoPriority": "no",
            "NotSupported": "no",
        })

    def tearDown(self):
        metrics_server._VOTE_DEFAULTS.clear()
        metrics_server._VOTE_DEFAULTS.update(self._orig)

    @patch("metrics_server._parse_cfg_overrides", return_value={})
    @patch("metrics_server.subprocess.check_output",
           return_value=MOCK_AMENDMENTS_FEATURE_JSON)
    def test_returns_only_unenabled(self, _sub, _cfg):
        result = metrics_server.get_amendments()
        names = [a["name"] for a in result]
        self.assertNotIn("AlreadyEnabled", names)
        self.assertNotIn("AlsoEnabled", names)
        self.assertEqual(len(result), 3)

    @patch("metrics_server._parse_cfg_overrides", return_value={})
    @patch("metrics_server.subprocess.check_output",
           return_value=MOCK_AMENDMENTS_FEATURE_JSON)
    def test_vote_from_defaults(self, _sub, _cfg):
        result = metrics_server.get_amendments()
        by_name = {a["name"]: a for a in result}
        self.assertEqual(by_name["HasMajority"]["vote"], "yes")
        self.assertEqual(by_name["NoPriority"]["vote"], "no")

    @patch("metrics_server._parse_cfg_overrides", return_value={})
    @patch("metrics_server.subprocess.check_output",
           return_value=MOCK_AMENDMENTS_FEATURE_JSON)
    def test_majority_flag_set(self, _sub, _cfg):
        result = metrics_server.get_amendments()
        by_name = {a["name"]: a for a in result}
        self.assertTrue(by_name["HasMajority"]["majority"])
        self.assertFalse(by_name["NoPriority"]["majority"])

    @patch("metrics_server._parse_cfg_overrides", return_value={})
    @patch("metrics_server.subprocess.check_output",
           return_value=MOCK_AMENDMENTS_FEATURE_JSON)
    def test_majority_sorted_first(self, _sub, _cfg):
        result = metrics_server.get_amendments()
        self.assertTrue(result[0]["majority"])

    @patch("metrics_server._parse_cfg_overrides",
           return_value={"CCCC0003": "no"})
    @patch("metrics_server.subprocess.check_output",
           return_value=MOCK_AMENDMENTS_FEATURE_JSON)
    def test_config_veto_overrides_default_yes(self, _sub, _cfg):
        result = metrics_server.get_amendments()
        by_name = {a["name"]: a for a in result}
        self.assertEqual(by_name["HasMajority"]["vote"], "no")

    @patch("metrics_server._parse_cfg_overrides",
           return_value={"DDDD0004": "yes"})
    @patch("metrics_server.subprocess.check_output",
           return_value=MOCK_AMENDMENTS_FEATURE_JSON)
    def test_config_vote_overrides_default_no(self, _sub, _cfg):
        result = metrics_server.get_amendments()
        by_name = {a["name"]: a for a in result}
        self.assertEqual(by_name["NoPriority"]["vote"], "yes")

    @patch("metrics_server._parse_cfg_overrides", return_value={})
    @patch("metrics_server.subprocess.check_output",
           side_effect=Exception("rippled unavailable"))
    def test_returns_empty_on_exception(self, _sub, _cfg):
        result = metrics_server.get_amendments()
        self.assertEqual(result, [])
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
cd /home/hamsa/motd
python3 -m unittest test_metrics_server.TestVoteDefaults test_metrics_server.TestGetAmendments -v 2>&1 | head -30
```

Expected: `AttributeError: module 'metrics_server' has no attribute '_parse_vote_defaults'` (or similar — implementation doesn't exist yet).

---

## Task 2: Implement vote defaults, config overrides, and get_amendments

**Files:**
- Modify: `/home/hamsa/motd/metrics_server.py`

- [ ] **Step 1: Add constants and `_parse_vote_defaults()` after the existing constants block (after line 18)**

```python
FEATURES_MACRO = "/home/hamsa/rippled/include/xrpl/protocol/detail/features.macro"
RIPPLED_CFG = "/etc/opt/ripple/rippled.cfg"


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


_VOTE_DEFAULTS: dict = _parse_vote_defaults()
```

Insert this block after line 18 (`PORT = 8080`) and before the blank line that precedes `_rapl_prev_uj`.

- [ ] **Step 2: Add `_parse_cfg_overrides()` immediately after `_VOTE_DEFAULTS`**

```python
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

- [ ] **Step 3: Add `get_amendments()` after `get_alerts()`**

```python
def get_amendments() -> list:
    try:
        raw = subprocess.check_output(
            ["sudo", RIPPLED, "feature"],
            timeout=10, text=True, stderr=subprocess.DEVNULL,
        )
        features = json.loads(raw)["result"]["features"]
        overrides = _parse_cfg_overrides()
        result = []
        for hash_, data in features.items():
            if data.get("enabled"):
                continue
            name = data.get("name", "")
            vote = overrides.get(hash_) or _VOTE_DEFAULTS.get(name, "no")
            result.append({
                "name": name,
                "vote": vote,
                "supported": data.get("supported", False),
                "majority": "majority" in data,
            })
        result.sort(key=lambda x: (not x["majority"], x["name"]))
        return result
    except Exception:
        return []
```

- [ ] **Step 4: Wire `get_amendments()` into `collect_metrics()`**

In `collect_metrics()`, add `"amendments": get_amendments()` as a new key:

```python
def collect_metrics():
    return {
        "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "validator": get_validator_info(),
        "identity": get_identity(),
        "system": get_system_info(),
        "network": get_network_info(),
        "alerts": get_alerts(),
        "amendments": get_amendments(),
    }
```

- [ ] **Step 5: Update MOCK_METRICS and test_metrics_valid_json_top_level_keys in test_metrics_server.py**

Add `"amendments": []` to `MOCK_METRICS` at the top of the file:

```python
MOCK_METRICS = {
    "timestamp": "2026-05-20T00:00:00Z",
    "validator": { ... },   # unchanged
    "identity":  { ... },   # unchanged
    "system":    { ... },   # unchanged
    "network":   { ... },   # unchanged
    "alerts": ["WRN slow_close 4.2s"],
    "amendments": [],
}
```

Update the key list in `test_metrics_valid_json_top_level_keys`:

```python
for key in ("timestamp", "validator", "identity", "system", "network", "alerts", "amendments"):
    self.assertIn(key, data, f"missing top-level key: {key}")
```

- [ ] **Step 6: Run the full test suite**

```bash
cd /home/hamsa/motd
python3 -m unittest test_metrics_server -v 2>&1 | tail -15
```

Expected: all tests pass. Final line: `Ran N tests in X.Xs` — `OK`

- [ ] **Step 7: Commit**

```bash
cd /home/hamsa/motd
git add metrics_server.py test_metrics_server.py
git commit -m "feat: add amendments voting data collection"
```

---

## Task 3: Write install-amendments.sh

**Files:**
- Create: `/home/hamsa/motd/install-amendments.sh`

- [ ] **Step 1: Create the script**

```bash
#!/usr/bin/env bash
# Add sudoers entry for rippled feature command and restart metrics-server.
# Run as: sudo bash install-amendments.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "ERROR: run this script with sudo: sudo bash $0"
  exit 1
fi

echo "=== Updating /etc/sudoers.d/metrics-server ==="
tee /etc/sudoers.d/metrics-server > /dev/null << 'EOF'
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled server_info
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rippled feature
hamsa ALL=(ALL) NOPASSWD: /usr/local/bin/rapl-energy-uj
EOF
chmod 0440 /etc/sudoers.d/metrics-server
visudo -c
echo "  done."

echo "=== Restarting metrics-server ==="
systemctl restart metrics-server
sleep 2
systemctl status metrics-server --no-pager

echo ""
echo "=== Verifying amendments in endpoint ==="
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | grep -A 5 '"amendments"'
echo ""
echo "=== Done ==="
```

- [ ] **Step 2: Commit**

```bash
cd /home/hamsa/motd
git add install-amendments.sh
git commit -m "feat: add install script for amendments sudoers entry"
```

---

## Task 4: Fix VALIDATOR/SYSTEM section headers in validator.html

**Files:**
- Modify: `~/joshuahamsa.github.io/validator.html`

- [ ] **Step 1: Replace the left-aligned headers with centered versions**

Find this block (around line 114):

```javascript
      lines.push(twoCol(` ${span('cyan', '── VALIDATOR' + '─'.repeat(25))}`,
                        ` ${span('cyan', '── SYSTEM' + '─'.repeat(29))}`));
```

Replace with:

```javascript
      lines.push(twoCol(
        span('cyan', '─'.repeat(13) + ' VALIDATOR ' + '─'.repeat(14)),
        span('cyan', '─'.repeat(15) + ' SYSTEM ' + '─'.repeat(16))
      ));
```

Verification: VALIDATOR column = 38 chars (13 + 11 + 14 = 38 ✓). SYSTEM column = 39 chars (15 + 8 + 16 = 39 ✓).

- [ ] **Step 2: Commit**

```bash
cd ~/joshuahamsa.github.io
git add validator.html
git commit -m "fix: center VALIDATOR and SYSTEM section headers"
```

---

## Task 5: Add amendments box to validator.html

**Files:**
- Modify: `~/joshuahamsa.github.io/validator.html`

- [ ] **Step 1: Add `<pre id="amd-pre">` to the HTML, immediately after `<pre id="val-pre">`**

Find:

```html
      <pre id="val-pre" class="val-pre">connecting...</pre>
      <div id="val-footer" class="val-footer val-dim">updated — · refreshing every 5s</div>
```

Replace with:

```html
      <pre id="val-pre" class="val-pre">connecting...</pre>
      <pre id="amd-pre" class="val-pre">connecting...</pre>
      <div id="val-footer" class="val-footer val-dim">updated — · refreshing every 5s</div>
```

- [ ] **Step 2: Add `buildAmendmentLines()` to the `<script>` block, immediately before `buildLines()`**

```javascript
    function buildAmendmentLines(amendments) {
      const H78 = '═'.repeat(78);
      const lines = [];

      if (amendments === null || amendments === undefined) {
        lines.push(`╔${H78}╗`);
        lines.push(`║${rpad(span('dim', '  retrieving...'), 78)}║`);
        lines.push(`╚${H78}╝`);
        return lines;
      }

      // Centered header
      const label = `PENDING AMENDMENTS (${amendments.length})`;
      const dashes = 78 - label.length - 2;
      const ldash = Math.floor(dashes / 2);
      const rdash = dashes - ldash;
      lines.push(`╔${H78}╗`);
      lines.push(`║${span('cyan', '─'.repeat(ldash) + ' ' + label + ' ' + '─'.repeat(rdash))}║`);

      if (amendments.length === 0) {
        lines.push(`║${rpad(span('dim', '  no pending amendments'), 78)}║`);
        lines.push(`╚${H78}╝`);
        return lines;
      }

      // Column headers
      lines.push(`║${rpad('  Name' + ' '.repeat(31) + '  Vote   Status', 78)}║`);
      lines.push(`║${rpad('  ' + '─'.repeat(74), 78)}║`);

      for (const a of amendments) {
        const voteStr = a.vote === 'yes' ? span('green', 'YES') : span('red', 'NO ');
        let status, sCls;
        if (a.majority) {
          status = '★ MAJORITY'; sCls = 'amber';
        } else if (!a.supported) {
          status = 'pending · unsupported'; sCls = 'dim';
        } else {
          status = 'pending'; sCls = 'dim';
        }
        const row = `  ${rpad(a.name, 35)}  ${voteStr}   ${span(sCls, status)}`;
        lines.push(`║${rpad(row, 78)}║`);
      }

      lines.push(`╚${H78}╝`);
      return lines;
    }
```

- [ ] **Step 3: Update `renderData()` to populate `amd-pre`**

Find:

```javascript
    function renderData(data) {
      document.getElementById('val-pre').innerHTML = buildLines(data).join('\n');
      document.getElementById('val-footer').textContent =
        `updated ${data.timestamp} · refreshing every 5s`;
    }
```

Replace with:

```javascript
    function renderData(data) {
      document.getElementById('val-pre').innerHTML = buildLines(data).join('\n');
      document.getElementById('amd-pre').innerHTML =
        buildAmendmentLines(data.amendments).join('\n');
      document.getElementById('val-footer').textContent =
        `updated ${data.timestamp} · refreshing every 5s`;
    }
```

- [ ] **Step 4: Commit and push**

```bash
cd ~/joshuahamsa.github.io
git add validator.html
git commit -m "feat: add amendments voting box and fix section headers"
git push origin main
```

---

## Task 6: Deploy and verify

- [ ] **Step 1: Run the install script on the server**

```bash
sudo bash /home/hamsa/motd/install-amendments.sh
```

Expected: `parsed OK`, `Active: active (running)`, JSON output with `"amendments"` array containing unenabled amendments.

- [ ] **Step 2: Smoke test the endpoint**

```bash
curl -s http://127.0.0.1:8080/metrics | python3 -m json.tool | python3 -c "
import json, sys
data = json.load(sys.stdin)
amds = data['amendments']
print(f'{len(amds)} pending amendments')
for a in amds:
    print(f'  {a[\"name\"]:<35} {a[\"vote\"]:<3}  majority={a[\"majority\"]}')
"
```

Expected: list of unenabled amendments with correct votes (majority entries first).

- [ ] **Step 3: Verify GitHub Pages after ~60s**

Open `https://joshuahamsa.github.io/validator.html` and confirm:
- VALIDATOR and SYSTEM headers have dashes on both sides of the label
- Amendments box appears below the identity row
- Amendments are listed with YES (green) / NO (red) vote labels
- Any amendment with majority shows `★ MAJORITY` in amber

---

## Self-Review

**Spec coverage:**
- ✅ Section header centering (Task 4)
- ✅ `_parse_vote_defaults()` at module load (Task 2 Step 1)
- ✅ `_VOTE_DEFAULTS` dict (Task 2 Step 1)
- ✅ `_parse_cfg_overrides()` reading `[veto_amendments]` / `[amendments]` (Task 2 Step 2)
- ✅ `get_amendments()` filtering, vote resolution, sort (Task 2 Step 3)
- ✅ `amendments` key in `collect_metrics()` (Task 2 Step 4)
- ✅ All 7 test cases from spec (Task 1)
- ✅ `buildAmendmentLines()` with centered header, column headers, data rows, empty/null states (Task 5)
- ✅ `renderData()` update (Task 5 Step 3)
- ✅ `install-amendments.sh` with all three sudoers entries (Task 3)

**Type consistency:**
- `get_amendments()` returns `list` of dicts with keys `name`, `vote`, `supported`, `majority` — used consistently in `buildAmendmentLines()` as `a.name`, `a.vote`, `a.supported`, `a.majority` ✓
- `_parse_vote_defaults()` returns `dict` — assigned to `_VOTE_DEFAULTS`, `.get(name, "no")` call consistent ✓
- `_parse_cfg_overrides()` returns `dict` — `.get(hash_)` call consistent ✓
