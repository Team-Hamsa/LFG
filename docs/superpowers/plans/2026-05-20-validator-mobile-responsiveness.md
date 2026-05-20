# Validator Dashboard Mobile Responsiveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the validator dashboard ASCII table fit on mobile screens by reducing inner width from 78 to 65 chars and adding a responsive font-size CSS rule.

**Architecture:** Two files change: `styles.css` gets a single `font-size` addition to `.val-pre`; `validator.html` gets five JS edits (width constants, banner margins, banner subtitle, identity row removal, amendment columns). No new files. No dependencies.

**Tech Stack:** Vanilla HTML/CSS/JS, static files served via GitHub Pages.

---

### Task 1: CSS — responsive font-size for `.val-pre`

**Files:**
- Modify: `joshuahamsa.github.io/styles.css` (`.val-pre` rule, near bottom of file)

- [ ] **Step 1: Verify current state (pre-condition)**

```bash
grep -A6 '\.val-pre {' joshuahamsa.github.io/styles.css
```

Expected output includes `font-size: 14px;` but NO `min(` or `calc(` — confirms the responsive rule is absent.

- [ ] **Step 2: Add responsive font-size to `.val-pre`**

In `joshuahamsa.github.io/styles.css`, find the `.val-pre` rule (near the bottom, under `/* ── Validator dashboard ──`). Change:

```css
.val-pre {
  background-color: var(--bg-color);
  color: var(--text-color);
  font-family: 'Courier New', monospace;
  font-size: 14px;
  line-height: 1.4;
  overflow-x: auto;
  padding: 0;
  margin: 0;
  white-space: pre;
  border: none;
}
```

to:

```css
.val-pre {
  background-color: var(--bg-color);
  color: var(--text-color);
  font-family: 'Courier New', monospace;
  font-size: min(14px, calc((100vw - 2rem) / 40));
  line-height: 1.4;
  overflow-x: auto;
  padding: 0;
  margin: 0;
  white-space: pre;
  border: none;
}
```

The divisor `40` = 67 total chars × 0.6 (Courier New character width ratio). On a 390px phone with 2rem (32px) of container padding: `(390-32)/40 ≈ 9px`. On desktop (>800px) it stays 14px.

- [ ] **Step 3: Verify the rule was applied**

```bash
grep 'font-size' joshuahamsa.github.io/styles.css
```

Expected:
```
  font-size: min(14px, calc((100vw - 2rem) / 40));
```

- [ ] **Step 4: Commit**

```bash
git add joshuahamsa.github.io/styles.css
git commit -m "feat: add responsive font-size to validator dashboard pre blocks"
```

---

### Task 2: JS — update width constants, twoCol, and column headers

**Files:**
- Modify: `joshuahamsa.github.io/validator.html` lines 78–164

- [ ] **Step 1: Verify current state (pre-condition)**

```bash
grep -n "H78\|H38\|H39\|rpad(left\|rpad(right\|─.*VALIDATOR\|─.*SYSTEM" joshuahamsa.github.io/validator.html
```

Expected to see `H78`, `H38`, `H39`, `rpad(left, 38)`, `rpad(right, 39)`, dashes with counts 13/14 (VALIDATOR) and 15/16 (SYSTEM).

- [ ] **Step 2: Update twoCol comment and function (lines 78–81)**

Change:
```js
    // Build a two-column row: left col = 38 visible chars, right col = 39.
    function twoCol(left, right) {
      return `║${rpad(left, 38)}║${rpad(right, 39)}║`;
    }
```

to:
```js
    // Build a two-column row: left col = 30 visible chars, right col = 34.
    function twoCol(left, right) {
      return `║${rpad(left, 30)}║${rpad(right, 34)}║`;
    }
```

- [ ] **Step 3: Update width constants in buildLines (lines 137–139)**

Change:
```js
      const H78 = '═'.repeat(78);
      const H38 = '═'.repeat(38);
      const H39 = '═'.repeat(39);
```

to:
```js
      const H65 = '═'.repeat(65);
      const H30 = '═'.repeat(30);
      const H34 = '═'.repeat(34);
```

- [ ] **Step 4: Update box top border (line 151)**

Change:
```js
      lines.push(`╔${H78}╗`);
```

to:
```js
      lines.push(`╔${H65}╗`);
```

- [ ] **Step 5: Update VALIDATOR|SYSTEM divider and column headers (lines 160–164)**

Change:
```js
      // Validator | System
      lines.push(`╠${H38}╦${H39}╣`);
      lines.push(twoCol(
        span('cyan', '─'.repeat(13) + ' VALIDATOR ' + '─'.repeat(14)),
        span('cyan', '─'.repeat(15) + ' SYSTEM ' + '─'.repeat(16))
      ));
```

to:
```js
      // Validator | System
      lines.push(`╠${H30}╦${H34}╣`);
      lines.push(twoCol(
        span('cyan', '─'.repeat(9) + ' VALIDATOR ' + '─'.repeat(10)),
        span('cyan', '─'.repeat(13) + ' SYSTEM ' + '─'.repeat(13))
      ));
```

Dash counts: left col 30 = 9+11+10 ✓, right col 34 = 13+8+13 ✓.

- [ ] **Step 6: Verify width constants and twoCol are correct**

```bash
grep -n "H65\|H30\|H34\|rpad(left\|rpad(right\|─.*VALIDATOR\|─.*SYSTEM" joshuahamsa.github.io/validator.html
```

Expected: `H65`, `H30`, `H34`, `rpad(left, 30)`, `rpad(right, 34)`, dash counts 9/10 (VALIDATOR) and 13/13 (SYSTEM).

- [ ] **Step 7: Commit**

```bash
git add joshuahamsa.github.io/validator.html
git commit -m "refactor: reduce validator dashboard table inner width 78→65"
```

---

### Task 3: JS — trim banner margins and remove manifest from subtitle

**Files:**
- Modify: `joshuahamsa.github.io/validator.html` lines 152–157

- [ ] **Step 1: Verify current state (pre-condition)**

```bash
sed -n '152,157p' joshuahamsa.github.io/validator.html
```

Expected: 9 leading spaces before `${span('magenta'`, and `Manifest #${id.manifest_seq}` in the `sub` line.

- [ ] **Step 2: Trim banner row margins (lines 152–154)**

Change:
```js
      for (const row of BANNER) {
        lines.push(`║         ${span('magenta', row)}          ║`);
      }
```

to:
```js
      for (const row of BANNER) {
        lines.push(`║   ${span('magenta', row)}   ║`);
      }
```

3 spaces each side. Verify: 3 + 59 (banner width) + 3 = 65 = inner width ✓.

- [ ] **Step 3: Update subtitle — remove manifest, update centering calculation (lines 155–157)**

Change:
```js
      const sub = `▸▸  ${id.domain}  ·  ${id.public_key_short}  ·  Manifest #${id.manifest_seq}  ◂◂`;
      const sp = Math.floor((78 - sub.length) / 2);
      lines.push(`║${' '.repeat(sp)}${span('white', sub)}${' '.repeat(78 - sub.length - sp)}║`);
```

to:
```js
      const sub = `▸▸  ${id.domain}  ·  ${id.public_key_short}  ◂◂`;
      const sp = Math.floor((65 - sub.length) / 2);
      lines.push(`║${' '.repeat(sp)}${span('white', sub)}${' '.repeat(65 - sub.length - sp)}║`);
```

- [ ] **Step 4: Verify**

```bash
sed -n '152,157p' joshuahamsa.github.io/validator.html
```

Expected: 3 leading spaces before span, no `Manifest`, centering uses `65`.

- [ ] **Step 5: Commit**

```bash
git add joshuahamsa.github.io/validator.html
git commit -m "refactor: reduce banner margins and remove manifest from subtitle"
```

---

### Task 4: JS — remove identity row, close box with `╚H65╝`

**Files:**
- Modify: `joshuahamsa.github.io/validator.html` lines 184–192

- [ ] **Step 1: Verify current state (pre-condition)**

```bash
sed -n '184,193p' joshuahamsa.github.io/validator.html
```

Expected: `╠${H78}╣` separator, identity line with `◈ IDENTITY`, `╚${H78}╝`.

- [ ] **Step 2: Replace identity block with closing border (lines 185–192)**

Remove the entire identity block and replace with a simple closing border. Change:

```js

      // Identity row
      lines.push(`╠${H78}╣`);
      const revCls = id.revoked ? 'red' : 'green';
      const revLabel = id.revoked ? 'YES ⚠' : 'NO';
      const identVisible = `  ◈ IDENTITY  Key: ${id.public_key_short}  ·  Domain: ${id.domain}  ·  Revoked: ${revLabel}`;
      const identPad = ' '.repeat(Math.max(0, 78 - identVisible.length));
      lines.push(`║  ${span('yellow', '◈ IDENTITY')}  Key: ${span('white', id.public_key_short)}  ·  Domain: ${span('white', id.domain)}  ·  Revoked: ${span(revCls, revLabel)}${identPad}║`);
      lines.push(`╚${H78}╝`);
```

to:

```js
      lines.push(`╚${H65}╝`);
```

- [ ] **Step 3: Verify identity block is gone and box closes correctly**

```bash
grep -n "IDENTITY\|revCls\|revLabel\|identPad\|identVisible" joshuahamsa.github.io/validator.html
```

Expected: no output (all identity references removed).

```bash
grep -n "╚.*H65" joshuahamsa.github.io/validator.html
```

Expected: one line in `buildLines` with `╚${H65}╝`.

- [ ] **Step 4: Commit**

```bash
git add joshuahamsa.github.io/validator.html
git commit -m "refactor: remove identity row from validator dashboard"
```

---

### Task 5: JS — compact amendment columns

**Files:**
- Modify: `joshuahamsa.github.io/validator.html` lines 83–128 (`buildAmendmentLines`)

- [ ] **Step 1: Verify current state (pre-condition)**

```bash
sed -n '83,128p' joshuahamsa.github.io/validator.html
```

Expected: `H78`, `dashes = 78 - label.length - 2`, `rpad(..., 78)`, `rpad(a.name, 35)`, `'─'.repeat(74)`, triple space before status.

- [ ] **Step 2: Update H constant and all width references in buildAmendmentLines (line 84)**

Change:
```js
      const H78 = '═'.repeat(78);
```

to:
```js
      const H65 = '═'.repeat(65);
```

- [ ] **Step 3: Update null/retrieving case (lines 88–90)**

Change:
```js
        lines.push(`╔${H78}╗`);
        lines.push(`║${rpad(span('dim', '  retrieving...'), 78)}║`);
        lines.push(`╚${H78}╝`);
```

to:
```js
        lines.push(`╔${H65}╗`);
        lines.push(`║${rpad(span('dim', '  retrieving...'), 65)}║`);
        lines.push(`╚${H65}╝`);
```

- [ ] **Step 4: Update dashes calculation and box top (lines 96, 99)**

Change:
```js
      const dashes = 78 - label.length - 2;
```

to:
```js
      const dashes = 65 - label.length - 2;
```

Change:
```js
      lines.push(`╔${H78}╗`);
```

to:
```js
      lines.push(`╔${H65}╗`);
```

- [ ] **Step 5: Update zero-amendments case (lines 103–104)**

Change:
```js
        lines.push(`║${rpad(span('dim', '  no pending amendments'), 78)}║`);
        lines.push(`╚${H78}╝`);
```

to:
```js
        lines.push(`║${rpad(span('dim', '  no pending amendments'), 65)}║`);
        lines.push(`╚${H65}╝`);
```

- [ ] **Step 6: Update column headers (lines 109–110)**

Change:
```js
      lines.push(`║${rpad('  Name' + ' '.repeat(31) + '  Vote   Status', 78)}║`);
      lines.push(`║${rpad('  ' + '─'.repeat(74), 78)}║`);
```

to:
```js
      lines.push(`║${rpad('  Name' + ' '.repeat(22) + 'Vote  Status', 65)}║`);
      lines.push(`║${rpad('  ' + '─'.repeat(61), 65)}║`);
```

Header alignment: row format is `  name(24)  vote(3)  status`, so Vote starts at column 28. Header "Name" ends at column 5, needs 22 spaces to reach column 28. Divider: 65 - 2 (leading spaces) - 2 (trailing) = 61 dashes.

- [ ] **Step 7: Update amendment rows and closing border (lines 122–126)**

Change:
```js
        const row = `  ${rpad(a.name, 35)}  ${voteStr}   ${span(sCls, status)}`;
        lines.push(`║${rpad(row, 78)}║`);
      }

      lines.push(`╚${H78}╝`);
```

to:
```js
        const row = `  ${rpad(a.name, 24)}  ${voteStr}  ${span(sCls, status)}`;
        lines.push(`║${rpad(row, 65)}║`);
      }

      lines.push(`╚${H65}╝`);
```

Name column reduced 35→24 (longest name `fixXChainRewardRounding` is 23 chars). Vote spacing: two spaces before and after instead of two before / three after. Max row visible: 2+24+2+3+2+21 = 54 chars, padded to 65 ✓.

- [ ] **Step 8: Run full width consistency check**

```bash
node -e "
const inner = 65;
const bannerLen = 59;
const left = 3, right = 3;
console.assert(left + bannerLen + right === inner, 'banner padding: ' + (left+bannerLen+right));

const leftCol = 30, rightCol = 34;
console.assert(leftCol + 1 + rightCol === inner, 'col widths: ' + (leftCol+1+rightCol));

const nameCol = 24;
const maxName = 'fixXChainRewardRounding'.length;
console.assert(maxName <= nameCol, 'name overflow: ' + maxName);

const maxAmdRow = 2 + nameCol + 2 + 3 + 2 + 'pending · unsupported'.length;
console.assert(maxAmdRow <= inner, 'amd row overflow: ' + maxAmdRow);

const maxLeftCol = '  ledger   104367266'.length;
console.assert(maxLeftCol <= leftCol, 'left col overflow: ' + maxLeftCol);

const maxRightCol = '  RAM  ██████████  25.5/63G'.length;
console.assert(maxRightCol <= rightCol, 'right col overflow: ' + maxRightCol);

console.log('All width checks pass. Inner:', inner, '/ Total:', inner+2, 'chars');
"
```

Expected:
```
All width checks pass. Inner: 65 / Total: 67 chars
```

- [ ] **Step 9: Verify no remaining references to old width (78) in buildAmendmentLines**

```bash
grep -n "78\|H78" joshuahamsa.github.io/validator.html
```

Expected: no output (all `78` and `H78` references gone from the file).

- [ ] **Step 10: Commit**

```bash
git add joshuahamsa.github.io/validator.html
git commit -m "refactor: compact amendment columns to fit 65-char table width"
```

---

### Task 6: Live verification

- [ ] **Step 1: Confirm the live endpoint returns data**

```bash
curl -s https://letseffinggo.tail82fcc6.ts.net/metrics | python3 -c "import sys,json; d=json.load(sys.stdin); print('validator state:', d['validator']['state'])"
```

Expected: `validator state: full` (or whatever current state is).

- [ ] **Step 2: Push to GitHub Pages**

```bash
git push
```

- [ ] **Step 3: Verify page loads (desktop)**

```bash
curl -s https://joshuahamsa.com/validator.html | grep -c "val-pre"
```

Expected: `2` (two `<pre>` blocks).

- [ ] **Step 4: Manual mobile check**

Open `https://joshuahamsa.com/validator.html` on your phone (Tailscale off). Confirm:
- ASCII table renders without horizontal scrolling
- Font scales smaller than desktop but all content is visible
- Both the validator/system block and the amendments block fit the screen width
