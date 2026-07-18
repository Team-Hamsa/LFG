# X Share-Card Click-Through Forwarding + Share Attribution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Humans clicking a shared `/nft/<number>` link on X get JS-forwarded to the minting webapp, while X's crawler still renders the per-NFT card; every click is logged with an optional `?ref=<sharer wallet>` for future attribution.

**Architecture:** `handle_nft_card` (`lfg_service/app.py`) keeps its OG/Twitter meta tags untouched and, when the new `SHARE_FORWARD_URL` env var is set, swaps its human-visible body for a branded flash + `location.replace()` redirect (no HTTP redirect — X's crawler follows those and would card the destination). A new `lfg_core/share_clicks.py` store logs each hit best-effort. The Activity client appends `?ref=<wallet>` to share URLs and stashes an incoming `ref` in localStorage.

**Tech Stack:** Python 3 / aiohttp / sqlite3 (existing service), vanilla JS no-build client, pytest.

Spec: `docs/superpowers/specs/2026-07-17-x-share-forwarding-design.md`

## Global Constraints

- `SHARE_FORWARD_URL` unset (default `""`) ⇒ byte-for-byte today's behavior. Feature-flag convention.
- NEVER an HTTP 301/302 on `/nft/{number}`; NEVER meta-refresh; JS-only redirect via `location.replace`.
- `og:url` / `canonical` stay ref-less (they already are — built from `number` only; do not change).
- Meta tags (`twitter:card/image/title/description`, `og:*`) must be unchanged by this work.
- Click logging is best-effort: a `share_clicks` failure must never break the card response.
- `ref` is validated with `is_valid_classic_address` (already imported in `lfg_service/app.py`); invalid ⇒ treated as absent (logged NULL), never echoed into HTML/redirect.
- Pre-push gate (ruff, mypy, pytest) must stay green; run `.venv/bin/python -m pytest` from the worktree root.
- Commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `share_clicks` store (`lfg_core/share_clicks.py`)

**Files:**
- Create: `lfg_core/share_clicks.py`
- Create: `tests/test_share_clicks.py`

**Interfaces:**
- Consumes: `lfg_core.db_path.app_db_path(network)` (existing).
- Produces: `record_click(db_file: str, nft_number: int, ref_wallet: str | None, is_bot: bool, user_agent: str) -> bool` (True = row written; False = swallowed failure). `init_db(db_file: str) -> None` (idempotent CREATE TABLE). Task 2 calls `record_click` with `db_file=app_db_path()`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_share_clicks.py
import sqlite3

from lfg_core import share_clicks


def test_record_click_inserts_row(tmp_path):
    db = str(tmp_path / "app.db")
    ok = share_clicks.record_click(db, 42, "rrrrrrrrrrrrrrrrrrrrrhoLvTp", False, "Mozilla/5.0")
    assert ok is True
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT nft_number, ref_wallet, is_bot, user_agent FROM share_clicks"
    ).fetchone()
    conn.close()
    assert row == (42, "rrrrrrrrrrrrrrrrrrrrrhoLvTp", 0, "Mozilla/5.0")


def test_record_click_null_ref_and_bot_flag(tmp_path):
    db = str(tmp_path / "app.db")
    assert share_clicks.record_click(db, 7, None, True, "Twitterbot/1.0") is True
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT ref_wallet, is_bot FROM share_clicks").fetchone()
    conn.close()
    assert row == (None, 1)


def test_record_click_truncates_user_agent(tmp_path):
    db = str(tmp_path / "app.db")
    share_clicks.record_click(db, 1, None, False, "x" * 1000)
    conn = sqlite3.connect(db)
    (ua,) = conn.execute("SELECT user_agent FROM share_clicks").fetchone()
    conn.close()
    assert len(ua) == 256


def test_record_click_swallows_db_failure(tmp_path):
    # Unwritable path: a directory where the file should be.
    bad = str(tmp_path / "adir")
    import os

    os.mkdir(bad)
    assert share_clicks.record_click(bad, 1, None, False, "ua") is False


def test_record_click_stamps_clicked_at(tmp_path):
    db = str(tmp_path / "app.db")
    share_clicks.record_click(db, 1, None, False, "ua")
    conn = sqlite3.connect(db)
    (ts,) = conn.execute("SELECT clicked_at FROM share_clicks").fetchone()
    conn.close()
    assert ts  # non-empty ISO timestamp
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_share_clicks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.share_clicks'`

- [ ] **Step 3: Implement `lfg_core/share_clicks.py`**

```python
# lfg_core/share_clicks.py
"""Share-link click log (#41 follow-on): one row per GET /nft/{number} hit.

Best-effort by design — the card page must render even if this table can't
be written, so record_click swallows every sqlite error and returns False.
Lives in the per-network app DB (db_path.app_db_path), self-migrating like
the other stores: init happens lazily inside record_click.
"""

import logging
import sqlite3

log = logging.getLogger(__name__)

_UA_MAX = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS share_clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nft_number INTEGER NOT NULL,
    ref_wallet TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT NOT NULL DEFAULT '',
    clicked_at TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


def init_db(db_file: str) -> None:
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def record_click(
    db_file: str, nft_number: int, ref_wallet: str | None, is_bot: bool, user_agent: str
) -> bool:
    try:
        init_db(db_file)
        conn = sqlite3.connect(db_file)
        try:
            conn.execute(
                "INSERT INTO share_clicks (nft_number, ref_wallet, is_bot, user_agent)"
                " VALUES (?, ?, ?, ?)",
                (nft_number, ref_wallet, 1 if is_bot else 0, (user_agent or "")[:_UA_MAX]),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        log.warning("share_clicks write failed (nft #%s)", nft_number, exc_info=True)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_share_clicks.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add lfg_core/share_clicks.py tests/test_share_clicks.py
git commit -m "feat(share): add best-effort share_clicks click log store

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `SHARE_FORWARD_URL` config + card forwarding + click logging in `handle_nft_card`

**Files:**
- Modify: `lfg_core/config.py` (right after the `PUBLIC_SHARE_BASE_URL` block, ~line 362)
- Modify: `lfg_service/app.py` (`handle_nft_card`, ~lines 3979–4060)
- Test: `tests/test_og_page.py` (append)

**Interfaces:**
- Consumes: `share_clicks.record_click` (Task 1), `db_path.app_db_path()` (existing), `is_valid_classic_address` (already imported at `lfg_service/app.py:33`), `config.SHARE_FORWARD_URL` (new).
- Produces: the served HTML contains, when `SHARE_FORWARD_URL` is set, exactly one `location.replace(...)` script and a fallback `<a>` — Task 3's client sends the `?ref=` these consume.

- [ ] **Step 1: Add the config constant**

In `lfg_core/config.py`, directly below the `PUBLIC_SHARE_BASE_URL` assignment:

```python
# Where a HUMAN clicking a share link is forwarded (JS location.replace on
# the OG card page, GET /nft/{number}) — e.g. https://build.letseffinggo.com.
# Never an HTTP redirect: X's crawler follows those and would render the
# destination's generic card instead of the per-NFT image. Unset (default)
# = feature off, the card page body renders exactly as before.
SHARE_FORWARD_URL = os.getenv("SHARE_FORWARD_URL", "").strip().rstrip("/")
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_og_page.py` (reuse the existing `_seed_onchain` / `_onchain` / `_req` helpers and monkeypatch style already in the file). Note `_req` must gain UA/query support — replace the existing `_req` helper with:

```python
def _req(number, query="", headers=None):
    request = make_mocked_request(
        "GET", f"/nft/{number}{('?' + query) if query else ''}", headers=headers or {}
    )
    request.match_info["number"] = str(number)
    return request
```

(Existing callers pass only `number`; the defaults keep them working.)

Then append:

```python
_REF = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"  # valid classic address (ACCOUNT_ZERO)


def _seed_basic(tmp_path, monkeypatch, number=42):
    _seed_onchain(tmp_path, monkeypatch, [_onchain("AAA", number)])
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)


def _clicks_db(tmp_path, monkeypatch):
    db = str(tmp_path / "app_clicks.db")
    monkeypatch.setattr(server.db_path, "app_db_path", lambda network=None: db)
    return db


def test_forward_unset_keeps_legacy_body(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "")
    _seed_basic(tmp_path, monkeypatch)
    _clicks_db(tmp_path, monkeypatch)
    body = _run(server.handle_nft_card(_req(42))).text
    assert "location.replace" not in body
    assert "<h1>LFGO #42</h1>" in body


def test_forward_set_injects_js_redirect_and_keeps_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)
    _clicks_db(tmp_path, monkeypatch)
    resp = _run(server.handle_nft_card(_req(42)))
    assert resp.status == 200  # no HTTP redirect, ever
    body = resp.text
    # Meta tags untouched — the crawler contract.
    assert 'name="twitter:card" content="summary_large_image"' in body
    assert 'name="twitter:image" content="https://cdn.example/img.png"' in body
    # JS-only forward + visible fallback link, Bithomp retained.
    assert 'location.replace("https:\\/\\/build.example")' in body
    assert 'href="https://build.example"' in body
    assert "View on Bithomp" in body
    assert "http-equiv" not in body  # no meta-refresh


def test_forward_appends_valid_ref_and_logs_click(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    _seed_basic(tmp_path, monkeypatch)
    db = _clicks_db(tmp_path, monkeypatch)
    body = _run(
        server.handle_nft_card(
            _req(42, query=f"ref={_REF}", headers={"User-Agent": "Mozilla/5.0"})
        )
    ).text
    assert f'location.replace("https:\\/\\/build.example?ref={_REF}")' in body
    # og:url / canonical stay ref-less so X dedupes card variants.
    assert 'property="og:url" content="https://share.example/nft/42"' in body
    assert 'rel="canonical" href="https://share.example/nft/42"' in body
    import sqlite3

    row = sqlite3.connect(db).execute(
        "SELECT nft_number, ref_wallet, is_bot FROM share_clicks"
    ).fetchone()
    assert row == (42, _REF, 0)


def test_invalid_ref_ignored_not_echoed(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)
    db = _clicks_db(tmp_path, monkeypatch)
    evil = '"><script>alert(1)</script>'
    body = _run(server.handle_nft_card(_req(42, query="ref=" + escape(evil)))).text
    assert "alert(1)" not in body
    assert 'location.replace("https:\\/\\/build.example")' in body  # no ref appended
    import sqlite3

    (ref,) = sqlite3.connect(db).execute("SELECT ref_wallet FROM share_clicks").fetchone()
    assert ref is None


def test_bot_user_agent_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)
    db = _clicks_db(tmp_path, monkeypatch)
    _run(server.handle_nft_card(_req(42, headers={"User-Agent": "Twitterbot/1.0"})))
    import sqlite3

    (is_bot,) = sqlite3.connect(db).execute("SELECT is_bot FROM share_clicks").fetchone()
    assert is_bot == 1


def test_click_log_failure_never_breaks_card(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_FORWARD_URL", "https://build.example")
    _seed_basic(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(server.share_clicks, "record_click", boom)
    resp = _run(server.handle_nft_card(_req(42)))
    assert resp.status == 200


def test_config_share_forward_url_defaults_empty():
    import importlib
    import os

    assert os.getenv("SHARE_FORWARD_URL") is None
    from lfg_core import config as cfg

    assert cfg.SHARE_FORWARD_URL == ""
    del importlib  # imported for parity with sibling config tests; constant is frozen at import
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_og_page.py -v -k "forward or ref or bot or click_log or share_forward"`
Expected: FAIL — `AttributeError: module 'lfg_service.app' has no attribute 'share_clicks'` / missing `location.replace` assertions. Also run the FULL file to confirm the `_req` helper change didn't break existing tests: `.venv/bin/python -m pytest tests/test_og_page.py -v` (pre-existing tests must still pass or fail only on the new names).

- [ ] **Step 4: Implement in `lfg_service/app.py`**

Add imports near the other `lfg_core` imports:

```python
from lfg_core import db_path, share_clicks
```

(`db_path` may already be imported — check; keep a single import.)

Add module-level helpers above `handle_nft_card`:

```python
_BOT_UA_MARKERS = ("twitterbot", "facebookexternalhit", "slackbot", "discordbot", "telegrambot")


def _share_ref(request: Any) -> str | None:
    """?ref=<sharer wallet> — shape-validated, never trusted further."""
    ref = (request.query.get("ref") or "").strip()
    return ref if ref and is_valid_classic_address(ref) else None


def _is_share_bot(user_agent: str) -> bool:
    ua = user_agent.lower()
    return any(m in ua for m in _BOT_UA_MARKERS)
```

Inside `handle_nft_card`, after the `onchain is None` 404 return (so 404s aren't logged) insert:

```python
    ref_wallet = _share_ref(request)
    user_agent = request.headers.get("User-Agent", "")
    try:
        share_clicks.record_click(
            db_path.app_db_path(),
            number,
            ref_wallet,
            _is_share_bot(user_agent),
            user_agent,
        )
    except Exception:  # noqa: BLE001 — logging must never break the card
        logging.getLogger(__name__).warning("share click log failed", exc_info=True)
```

Then replace the `body_image` / `html_doc` construction at the end with:

```python
    body_image = (
        f'<img src="{esc_image}" alt="{esc_title}" style="max-width:100%;">' if image_url else ""
    )
    if config.SHARE_FORWARD_URL:
        # Human click-through: JS-only forward into the webapp. The crawler
        # doesn't execute JS, so the per-NFT card tags above still render;
        # an HTTP redirect here would card the destination instead. The
        # validated ref rides along so the webapp can stash it (#41 follow-on).
        forward_url = config.SHARE_FORWARD_URL + (f"?ref={ref_wallet}" if ref_wallet else "")
        esc_forward = escape(forward_url, quote=True)
        js_forward = json.dumps(forward_url).replace("/", "\\/")
        body_html = (
            '<div style="min-height:100vh;display:flex;flex-direction:column;'
            "align-items:center;justify-content:center;background:#0b0b12;"
            'color:#fff;font-family:sans-serif;text-align:center;margin:0;">'
            + f"<h1>{esc_title}</h1>"
            + f'<p><a href="{esc_forward}" style="color:#9ecbff;">'
            + "Open Let&#x27;s Effing Go &#x2192;</a></p>"
            + f'<p><a href="{esc_bithomp}" style="color:#666;">View on Bithomp</a></p>'
            + "</div>"
            + f"<script>location.replace({js_forward});</script>"
        )
    else:
        body_html = (
            f"<h1>{esc_title}</h1>"
            + body_image
            + f"<p>{esc_description}</p>"
            + f'<p><a href="{esc_bithomp}">View on Bithomp</a></p>'
        )
    html_doc = "<!doctype html><html><head>" + "".join(meta_tags) + "</head><body>" + body_html + "</body></html>"
    return web.Response(text=html_doc, content_type="text/html")
```

(`json` is already imported at the top of `lfg_service/app.py` — verify; add if not.)

- [ ] **Step 5: Run the full test file and gate checks**

Run: `.venv/bin/python -m pytest tests/test_og_page.py tests/test_share_clicks.py -v`
Expected: all PASS (legacy tests unchanged — unset flag path identical output).
Run: `.venv/bin/ruff check lfg_service/app.py lfg_core/config.py && .venv/bin/mypy lfg_service/app.py lfg_core/share_clicks.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add lfg_core/config.py lfg_service/app.py tests/test_og_page.py
git commit -m "feat(share): SHARE_FORWARD_URL JS click-through + ref click logging on the OG card page

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Client — append `?ref=` to share URLs, stash incoming `ref`

**Files:**
- Modify: `webapp/client/app.js` (`shareUrlFor` ~line 234; init path in `main()`)

**Interfaces:**
- Consumes: module-level `me` (`webapp/client/app.js:87`, `me.wallet` set on session) and `shareBase`. Produces the `?ref=` that Task 2's `_share_ref` validates. Stash key: `localStorage["lfg_ref"]` (the future mint-attribution issue will read this exact key).

- [ ] **Step 1: Modify `shareUrlFor`**

Replace the existing function body:

```javascript
// XRPL classic-address shape (client-side gate only; the service re-validates).
const XRPL_ADDR_RE = /^r[1-9A-HJ-NP-Za-km-z]{24,34}$/;

function shareUrlFor(nftNumber, nftId) {
  if (shareBase && nftNumber != null) {
    // Attribution (#41 follow-on): tag the link with the sharer's wallet so
    // the card page can log whose shares get clicked. Wallets are public
    // on-chain — nothing new is leaked.
    const ref = me && me.wallet && XRPL_ADDR_RE.test(me.wallet)
      ? `?ref=${encodeURIComponent(me.wallet)}`
      : '';
    return `${shareBase}/nft/${nftNumber}${ref}`;
  }
  if (bithompBase && nftId) return bithompNftUrl(nftId);
  // No base is known (every /api/config fetch failed) — return '' so the
  // callers skip/hide the share control instead of rendering a dead
  // relative link.
  return '';
}
```

- [ ] **Step 2: Stash an incoming `ref` on load**

Near the top of `main()` (the client's init function — locate with `grep -n "function main" webapp/client/app.js`), before any awaits:

```javascript
  // Referral stash (#41 follow-on): a share click-through arrives as
  // ?ref=<wallet>. Persist it for the future mint-attribution flow; shape-
  // check so arbitrary query junk never lands in storage.
  try {
    const refParam = new URLSearchParams(location.search).get('ref');
    if (refParam && XRPL_ADDR_RE.test(refParam)) localStorage.setItem('lfg_ref', refParam);
  } catch (_) { /* private mode / no storage */ }
```

- [ ] **Step 3: Verify syntax (no JS test harness exists in this repo)**

Run: `node --check webapp/client/app.js`
Expected: no output (exit 0).
Run: `grep -c "lfg_ref\|XRPL_ADDR_RE" webapp/client/app.js`
Expected: ≥ 4 (const, two uses in shareUrlFor/stash, storage key).
Also run the smoke suite (it serves the client and would catch load-time breakage): `.venv/bin/python -m pytest webapp/test_smoke.py -q` — expected: pass counts unchanged from `main`.

- [ ] **Step 4: Commit**

```bash
git add webapp/client/app.js
git commit -m "feat(share): tag share links with sharer wallet ref; stash incoming ref for attribution

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Docs, full gate, follow-up issue

**Files:**
- Modify: `CLAUDE.md` (env-var block — add one line after `PUBLIC_SHARE_BASE_URL`)

- [ ] **Step 1: Document the env var**

Add to the `.env` block in `CLAUDE.md`, directly under the `PUBLIC_SHARE_BASE_URL` line:

```
SHARE_FORWARD_URL=https://build.letseffinggo.com              # optional (#41); humans clicking a share card are JS-forwarded here (never HTTP-redirect — the X crawler must stay on the per-NFT card page); unset = legacy card body
```

- [ ] **Step 2: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: everything green (same failures as `main` if any pre-exist — verify against a clean-`main` run before blaming this branch).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: SHARE_FORWARD_URL env var for share-card click-through

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: File the mint-attribution follow-up issue**

```bash
gh issue create --repo Team-Hamsa/LFG \
  --title "Share-link mint attribution: record stashed ref on mint, conversion metrics" \
  --body "Follow-up to the X share-card forwarding work (spec docs/superpowers/specs/2026-07-17-x-share-forwarding-design.md).

Already in place: share links carry ?ref=<sharer wallet>; the card page logs clicks to the share_clicks table (app DB); the webapp client stashes a valid ref in localStorage under key lfg_ref.

Remaining: the client sends the stashed ref when starting a mint; the service validates it (is_valid_classic_address, reject self-referral) and records a referrer column on the mint record; a metrics query/board for whose shares convert to mints. Touches the mint API + DB schema — deserves its own design pass for reward-abuse considerations (self-referral, wash-sharing).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

**Ops note (not a code step):** after merge + promote, set `SHARE_FORWARD_URL=https://build.letseffinggo.com` in prod `.env` and restart `lfg-activity` (staging: its own host or unset).

---

# Addendum tasks (2026-07-18): Branded share-card PNGs

Spec: the 2026-07-18 addendum in `docs/superpowers/specs/2026-07-17-x-share-forwarding-design.md`.

Additional global constraints:
- `SHARE_CARD_RENDER_ENABLED` unset/0 ⇒ zero behavior change anywhere.
- The card endpoint NEVER 500s for a live NFT: any render/dep failure ⇒ 302 to the raw art URL.
- Only art URLs passing the existing `_img_url_allowed` check may be fetched/rendered.
- The Cowork renderer source is at `.superpowers/sdd/cowork-share-card.mjs` — copy it VERBATIM to `scripts/share_card/share-card.mjs` (do not reformat or "improve" it).

### Task 5: Renderer package (`scripts/share_card/`)

**Files:**
- Create: `scripts/share_card/share-card.mjs` (verbatim copy of `.superpowers/sdd/cowork-share-card.mjs`)
- Create: `scripts/share_card/render.mjs`
- Create: `scripts/share_card/package.json`
- Modify: `.gitignore` (add `scripts/share_card/node_modules/` and `share_cards/`)

**Interfaces:**
- Produces the CLI Task 6's subprocess calls: `node scripts/share_card/render.mjs --token <N> --avatar <path-or-url> --out <path>` → exit 0 and PNG written on success, exit 1 + stderr message on failure. Optional `--logo <path>` (default: `../../assets/logo.png` relative to the script).

- [ ] **Step 1: Copy the renderer verbatim**

```bash
mkdir -p scripts/share_card
cp .superpowers/sdd/cowork-share-card.mjs scripts/share_card/share-card.mjs
```

- [ ] **Step 2: Write `scripts/share_card/render.mjs`**

```javascript
#!/usr/bin/env node
/**
 * render.mjs — CLI wrapper around share-card.mjs for the lfg_service
 * subprocess call:
 *   node render.mjs --token 4035 --avatar <path-or-url> --out out.png [--logo path]
 * Exit 0 = PNG written to --out. Exit 1 = failure (message on stderr).
 */
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { parseArgs } from 'node:util';
import renderShareCard from './share-card.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_LOGO = path.resolve(HERE, '../../assets/logo.png');

const { values } = parseArgs({
  options: {
    token: { type: 'string' },
    avatar: { type: 'string' },
    out: { type: 'string' },
    logo: { type: 'string', default: DEFAULT_LOGO },
  },
});

if (!values.token || !values.avatar || !values.out) {
  console.error('usage: render.mjs --token N --avatar <path|url> --out <path> [--logo <path>]');
  process.exit(1);
}

try {
  await renderShareCard({
    tokenId: values.token,
    avatarSrc: values.avatar,
    logoSrc: values.logo,
    outPath: values.out,
  });
} catch (err) {
  console.error(`render failed: ${err && err.message ? err.message : err}`);
  process.exit(1);
}
```

- [ ] **Step 3: Write `scripts/share_card/package.json`**

```json
{
  "name": "lfg-share-card",
  "private": true,
  "type": "module",
  "description": "Renders branded X share-card PNGs (see docs/superpowers/specs/2026-07-17-x-share-forwarding-design.md addendum)",
  "dependencies": {
    "@fontsource/fredoka": "^5.0.0",
    "@fontsource/jetbrains-mono": "^5.0.0",
    "playwright": "^1.45.0"
  }
}
```

- [ ] **Step 4: Gitignore the artifacts**

Append to `.gitignore`:

```
scripts/share_card/node_modules/
share_cards/
```

- [ ] **Step 5: Verify syntax + missing-args behavior**

Run: `node --check scripts/share_card/render.mjs && node --check scripts/share_card/share-card.mjs`
Expected: exit 0, no output.
Run: `node scripts/share_card/render.mjs 2>&1; echo "exit=$?"`
Expected: fails BEFORE importing playwright? It does NOT — the import of share-card.mjs pulls playwright. So expected output is either the usage line (if deps installed) or a module-not-found error mentioning `playwright` with exit != 0. Either is acceptable at this task; the real render smoke happens at ops time. Record which you saw in the report.

- [ ] **Step 6: Commit**

```bash
git add scripts/share_card .gitignore
git commit -m "feat(share): playwright share-card renderer package (Cowork design)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: `GET /nft/{number}/card.png` endpoint + `SHARE_CARD_RENDER_ENABLED` tag switch

**Files:**
- Modify: `lfg_core/config.py` (below `SHARE_FORWARD_URL`)
- Modify: `lfg_service/app.py` (new handler + route; `handle_nft_card` image-tag switch)
- Test: `tests/test_og_page.py` (append)

**Interfaces:**
- Consumes: Task 5's CLI contract; existing `_fetch_cdn`, `_img_url_allowed`, `nft_index`, `get_nft_data`; the `_isolate_app_db` autouse fixture already in `tests/test_og_page.py`.
- Produces: route `GET /nft/{number}/card.png`; module constants `_SHARE_CARD_DIR = "share_cards"`, `_RENDER_TIMEOUT_S = 60`; helper `_share_card_path(number, image_url) -> pathlib.Path`.

- [ ] **Step 1: Config flag**

In `lfg_core/config.py`, directly below `SHARE_FORWARD_URL`:

```python
# Branded share-card PNG rendering (GET /nft/{number}/card.png). Requires
# node + playwright chromium on the box (scripts/share_card/ — see the spec
# addendum). Off (default) = twitter:image keeps pointing at the raw art.
SHARE_CARD_RENDER_ENABLED = os.getenv("SHARE_CARD_RENDER_ENABLED", "0").strip() == "1"
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_og_page.py`:

```python
def test_card_png_route_registered():
    app = server.create_app()
    method_paths = {(r.method, getattr(r.resource, "canonical", "")) for r in app.router.routes()}
    assert ("GET", "/nft/{number}/card.png") in method_paths


def _card_req(number):
    request = make_mocked_request("GET", f"/nft/{number}/card.png")
    request.match_info["number"] = str(number)
    return request


def test_card_png_unknown_edition_404(tmp_path, monkeypatch):
    _seed_onchain(tmp_path, monkeypatch, [])
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)
    resp = _run(server.handle_nft_card_png(_card_req(9999)))
    assert resp.status == 404


def test_card_png_cache_hit_serves_without_render(tmp_path, monkeypatch):
    _seed_basic(tmp_path, monkeypatch)
    card_dir = tmp_path / "cards"
    monkeypatch.setattr(server, "_SHARE_CARD_DIR", str(card_dir))
    cached = server._share_card_path(42, "https://cdn.example/img.png")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"\x89PNG-cached")

    def no_render(*a, **k):
        raise AssertionError("render must not run on cache hit")

    monkeypatch.setattr(server, "_render_share_card", no_render)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 200
    assert resp.body == b"\x89PNG-cached"
    assert resp.content_type == "image/png"


def test_card_png_cache_key_changes_with_art(tmp_path, monkeypatch):
    a = server._share_card_path(42, "https://cdn.example/img.png")
    b = server._share_card_path(42, "https://cdn.example/img-v2.png")
    assert a != b
    assert a.name.startswith("42-") and b.name.startswith("42-")


def test_card_png_miss_renders_and_caches(tmp_path, monkeypatch):
    _seed_basic(tmp_path, monkeypatch)
    card_dir = tmp_path / "cards"
    monkeypatch.setattr(server, "_SHARE_CARD_DIR", str(card_dir))

    async def fake_fetch(url):
        assert url == "https://cdn.example/img.png"
        return b"rawart", "image/png"

    async def fake_render(number, art_path, out_path):
        out_path.write_bytes(b"\x89PNG-rendered")

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    monkeypatch.setattr(server, "_render_share_card", fake_render)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 200
    assert resp.body == b"\x89PNG-rendered"
    assert server._share_card_path(42, "https://cdn.example/img.png").exists()


def test_card_png_render_failure_302_to_raw_art(tmp_path, monkeypatch):
    _seed_basic(tmp_path, monkeypatch)
    card_dir = tmp_path / "cards"
    monkeypatch.setattr(server, "_SHARE_CARD_DIR", str(card_dir))

    async def fake_fetch(url):
        return b"rawart", "image/png"

    async def broken_render(number, art_path, out_path):
        raise RuntimeError("chromium missing")

    monkeypatch.setattr(server, "_fetch_cdn", fake_fetch)
    monkeypatch.setattr(server, "_render_share_card", broken_render)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 302
    assert resp.headers["Location"] == "https://cdn.example/img.png"


def test_card_png_disallowed_art_url_302(tmp_path, monkeypatch):
    _seed_onchain(
        tmp_path, monkeypatch, [_onchain("AAA", 42, image="https://evil.example/x.png")]
    )
    monkeypatch.setattr(server, "get_nft_data", lambda n: None)
    resp = _run(server.handle_nft_card_png(_card_req(42)))
    assert resp.status == 302
    assert resp.headers["Location"] == "https://evil.example/x.png"


def test_card_page_image_tags_switch_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", True)
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    _seed_basic(tmp_path, monkeypatch)
    body = _run(server.handle_nft_card(_req(42))).text
    assert 'name="twitter:image" content="https://share.example/nft/42/card.png"' in body
    assert 'property="og:image" content="https://share.example/nft/42/card.png"' in body
    assert "cdn.example/img.png" not in body.split("</head>")[0]


def test_card_page_image_tags_raw_when_disabled_or_no_base(tmp_path, monkeypatch):
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", False)
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    _seed_basic(tmp_path, monkeypatch)
    body = _run(server.handle_nft_card(_req(42))).text
    assert 'name="twitter:image" content="https://cdn.example/img.png"' in body
    # enabled but no public base -> still raw art (can't build an absolute card URL)
    monkeypatch.setattr(server.config, "SHARE_CARD_RENDER_ENABLED", True)
    monkeypatch.setattr(server.config, "PUBLIC_SHARE_BASE_URL", "")
    body = _run(server.handle_nft_card(_req(42))).text
    assert 'name="twitter:image" content="https://cdn.example/img.png"' in body
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_og_page.py -v -k card_png or card_page_image`
Expected: FAIL — `handle_nft_card_png` missing.

- [ ] **Step 4: Implement in `lfg_service/app.py`**

Module-level (near `handle_nft_card`; add `import hashlib`, `import pathlib`, `import tempfile` if absent — check first):

```python
_SHARE_CARD_DIR = "share_cards"
_RENDER_TIMEOUT_S = 60
_RENDER_SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent / "scripts/share_card/render.mjs")
_share_card_locks: dict[int, asyncio.Lock] = {}


def _share_card_path(number: int, image_url: str) -> pathlib.Path:
    key = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:12]
    return pathlib.Path(_SHARE_CARD_DIR) / f"{number}-{key}.png"


def _share_card_url(number: int) -> str:
    """Absolute card-PNG URL, or '' when it can't be built/served."""
    if not (config.SHARE_CARD_RENDER_ENABLED and config.PUBLIC_SHARE_BASE_URL):
        return ""
    return f"{config.PUBLIC_SHARE_BASE_URL}/nft/{number}/card.png"


async def _render_share_card(number: int, art_path: pathlib.Path, out_path: pathlib.Path) -> None:
    """Run the node renderer; raises on any failure (caller falls back)."""
    proc = await asyncio.create_subprocess_exec(
        "node",
        _RENDER_SCRIPT,
        "--token",
        str(number),
        "--avatar",
        str(art_path),
        "--out",
        str(out_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_RENDER_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"share-card render timed out for #{number}") from None
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"share-card render failed for #{number}: {stderr.decode(errors='replace')[:300]}"
        )


async def handle_nft_card_png(request: Any) -> Any:
    """Branded 1200x630 share-card PNG for twitter:image. Cache-on-disk keyed
    by (number, art URL) so swaps/remints self-invalidate; ANY failure falls
    back to a 302 at the raw art (X's crawler follows image redirects), so
    this endpoint can never make sharing worse than the pre-card behavior."""
    raw_number = request.match_info.get("number", "")
    try:
        number = int(raw_number)
    except (TypeError, ValueError):
        return web.HTTPNotFound()

    lfg_row = get_nft_data(number)
    conn = nft_index.init_db(nft_index.index_db_path(config.XRPL_NETWORK))
    try:
        onchain = nft_index.nft_by_number(conn, number)
    finally:
        conn.close()
    if onchain is None:
        return web.HTTPNotFound()
    image_url = onchain.image or (lfg_row or {}).get("image_url") or ""
    if not image_url:
        return web.HTTPNotFound()

    cached = _share_card_path(number, image_url)
    if cached.exists():
        return web.Response(body=cached.read_bytes(), content_type="image/png")

    if not _img_url_allowed(image_url):
        return web.HTTPFound(image_url)

    lock = _share_card_locks.setdefault(number, asyncio.Lock())
    async with lock:
        if cached.exists():  # rendered while we waited
            return web.Response(body=cached.read_bytes(), content_type="image/png")
        try:
            art_body, _ctype = await _fetch_cdn(image_url)
            cached.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as art_f:
                art_f.write(art_body)
                art_path = pathlib.Path(art_f.name)
            tmp_out = cached.with_suffix(".tmp.png")
            try:
                await _render_share_card(number, art_path, tmp_out)
                os.replace(tmp_out, cached)
            finally:
                art_path.unlink(missing_ok=True)
                tmp_out.unlink(missing_ok=True)
        except Exception:
            logging.getLogger(__name__).warning(
                "share-card render fell back to raw art for #%s", number, exc_info=True
            )
            return web.HTTPFound(image_url)
    return web.Response(body=cached.read_bytes(), content_type="image/png")
```

In `handle_nft_card`, where the image meta tags are built, switch the tag URL (raw art stays the fallback):

```python
    card_png_url = _share_card_url(number)
    tag_image = card_png_url or image_url
    esc_tag_image = escape(tag_image, quote=True)
```

and use `esc_tag_image` in the `twitter:image` / `og:image` meta tags (the body `<img>` keeps using `esc_image` — humans see the real art, only the crawler tags switch).

Register the route in `create_app`, BEFORE the existing `/nft/{number}` line (aiohttp matches most-specific first regardless, but keep them adjacent):

```python
    app.router.add_get("/nft/{number}/card.png", handle_nft_card_png)
```

- [ ] **Step 5: Run tests + gate checks**

Run: `.venv/bin/python -m pytest tests/test_og_page.py tests/test_share_clicks.py -v`
Expected: all PASS (legacy card tests unaffected — flag defaults off; the autouse fixture must also pin `SHARE_CARD_RENDER_ENABLED=False`, add that to `_isolate_app_db`).
Run: `.venv/bin/ruff check lfg_service/app.py lfg_core/config.py tests/test_og_page.py && .venv/bin/ruff format lfg_service/app.py lfg_core/config.py tests/test_og_page.py && .venv/bin/mypy lfg_service/app.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add lfg_core/config.py lfg_service/app.py tests/test_og_page.py
git commit -m "feat(share): on-demand branded share-card PNG endpoint behind SHARE_CARD_RENDER_ENABLED

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs + full gate + push

**Files:**
- Modify: `CLAUDE.md` (env block, below `SHARE_FORWARD_URL`)

- [ ] **Step 1: Document the env var**

Add below the `SHARE_FORWARD_URL` line in the worktree `CLAUDE.md`:

```
SHARE_CARD_RENDER_ENABLED=0                                   # optional (#41); 1 = twitter:image serves branded PNG from /nft/{n}/card.png (needs node + `cd scripts/share_card && npm i && npx playwright install --with-deps chromium`); render failures 302 to raw art
```

- [ ] **Step 2: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: green (compare any failures against clean main before blaming the branch).

- [ ] **Step 3: Commit and push**

```bash
git add CLAUDE.md
git commit -m "docs: SHARE_CARD_RENDER_ENABLED env var + share-card ops note

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push
```
