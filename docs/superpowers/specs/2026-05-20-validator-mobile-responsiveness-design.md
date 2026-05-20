# Validator Dashboard Mobile Responsiveness

**Date:** 2026-05-20
**Status:** Approved
**Files:** `joshuahamsa.github.io/validator.html`, `joshuahamsa.github.io/styles.css`

## Goal

Make the cyberpunk ASCII MOTD dashboard readable on mobile phones without sacrificing the aesthetic. The page loads publicly via Tailscale Funnel; the data works fine on mobile — the layout breaks.

## Root Cause

The dashboard renders a fixed 80-char wide ASCII table (`white-space: pre`, `font-size: 14px`). At 14px Courier New, 80 chars ≈ 672px — overflows any phone screen. Additionally, the first-fetch error handler silently fails (shows "connecting..." forever) because `lastSeenAt` is null on first load.

## Approach

Two-part fix: reduce total table width by trimming whitespace, then add a responsive font-size rule that auto-scales to fit any viewport.

## CSS Change

Add to `.val-pre` in `styles.css`:

```css
font-size: min(14px, calc((100vw - 2rem) / 40));
```

- On desktop (>800px): 14px unchanged
- On 430px phone: ~10px
- On 375px phone: ~8.5px
- `40` = 67 total chars × 0.6 (Courier New character width ratio)

## JS Changes (`validator.html`)

### 1. Inner width: 78 → 65

Update all width constants:
- `H78 = '═'.repeat(78)` → `H65 = '═'.repeat(65)`
- `H38 = '═'.repeat(38)` → `H30 = '═'.repeat(30)`
- `H39 = '═'.repeat(39)` → `H34 = '═'.repeat(34)`

### 2. Banner subtitle — remove manifest

```js
// Before
const sub = `▸▸  ${id.domain}  ·  ${id.public_key_short}  ·  Manifest #${id.manifest_seq}  ◂◂`;

// After
const sub = `▸▸  ${id.domain}  ·  ${id.public_key_short}  ◂◂`;
```

### 3. Banner side margins: 9 spaces → 3 spaces

```js
// Before
lines.push(`║         ${span('magenta', row)}          ║`);

// After
lines.push(`║   ${span('magenta', row)}   ║`);
```

Note: the banner rows are 59 chars. With 3-char margins each side: 3+59+3 = 65 = inner width. ✓

### 4. Two-column split: 38+39 → 30+34

```js
// Before
function twoCol(left, right) {
  return `║${rpad(left, 38)}║${rpad(right, 39)}║`;
}
// divider: `╠${H38}╦${H39}╣`

// After
function twoCol(left, right) {
  return `║${rpad(left, 30)}║${rpad(right, 34)}║`;
}
// divider: `╠${H30}╦${H34}╣`
```

Right column max content is 27 chars (`  RAM  ██████████  25.5/63G`) — fits in 34. ✓
Left column max content is 20 chars (`  ledger   104367266`) — fits in 30. ✓

### 5. Identity row — remove entirely

Remove from `buildLines()`:
- The `╠${H78}╣` separator before the identity line
- The identity line itself
- The `╚${H78}╝` closing border (replaced with `╚${H65}╝` closing the validator/system box)

The validator/system two-column section now closes the box directly.

### 6. Amendment columns — compact

```js
// Before
const row = `  ${rpad(a.name, 35)}  ${voteStr}   ${span(sCls, status)}`;
// header: `  Name` + ' '.repeat(31) + `  Vote   Status`
// divider: `  ` + '─'.repeat(74)

// After
const row = `  ${rpad(a.name, 24)}  ${voteStr}  ${span(sCls, status)}`;
// header: `  Name` + ' '.repeat(22) + `Vote  Status`
// divider: `  ` + '─'.repeat(61)
```

Longest amendment name (`fixXChainRewardRounding`) = 23 chars — fits in 24. ✓
Longest status (`pending · unsupported`) = 21 chars — fits in remaining space. ✓

## Out of Scope

- Error UX bug (silent "connecting..." on first-load failure) — separate issue, not part of this change
- Any layout changes to the rest of `joshuahamsa.github.io` (index, blog, etc.)
