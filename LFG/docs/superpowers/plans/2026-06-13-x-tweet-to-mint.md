# Tweet-to-Mint (X Integration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user mint an LFG NFT by tweeting `@letseffinggo !mint` from an X handle they have linked to their Xaman wallet, with payment pushed to Xaman and the NFT image replied to their tweet.

**Architecture:** A background polling worker reads @letseffinggo mentions (X API v2, 30s, owned-reads dedup), filters them (linked-only, strict command, not blacklisted, not duplicate), and hands each valid mention to a resumable orchestrator. The orchestrator pushes a Xaman payment payload (via a stored `user_token`), runs the existing `lfg_core.mint_flow` pipeline on payment, replies to the tweet with the NFT image (no URL), and pushes an accept-offer payload. State lives in three new SQLite tables so the worker is restart-safe and never double-mints or double-replies. Account linking happens in the existing authenticated webapp via X OAuth2 (PKCE) plus a Xaman SignIn to capture the push `user_token`.

**Tech Stack:** Python 3, aiohttp (webapp + async worker), SQLite (via `sqlite3`), `requests` (X + XUMM REST, called through `asyncio.to_thread` like the existing code), pytest. Reuses `lfg_core/{mint_flow,xumm_ops,xrpl_ops,config}.py`.

**Spec:** `docs/superpowers/specs/2026-06-13-x-tweet-to-mint-design.md`

---

## File Structure

**New files**
- `lfg_core/x_config.py` — X-specific config constants (or extend `lfg_core/config.py`; this plan extends `config.py`).
- `lfg_core/x_db.py` — SQLite access for `x_links`, `x_mint_sessions`, `x_ingest_state`; blacklist queries.
- `lfg_core/x_command.py` — pure parser: does a tweet text contain the strict `!mint` command?
- `lfg_core/x_filter.py` — pure-ish filter chain: given a mention + db, return accept or a drop reason.
- `lfg_core/x_client.py` — thin X API v2 wrapper: get_mentions, upload_media, post_reply (mockable).
- `lfg_core/x_mint.py` — social-mint orchestrator + status machine.
- `lfg_core/x_ingest.py` — polling worker loop wiring filter → orchestrator.
- `tests/test_x_command.py`, `tests/test_x_db.py`, `tests/test_x_filter.py`, `tests/test_x_client.py`, `tests/test_x_mint.py`, `tests/test_x_ingest.py`, `tests/test_x_link_route.py`, `tests/test_x_e2e.py`.

**Modified files**
- `lfg_core/config.py` — add X constants + feature flag.
- `lfg_core/xumm_ops.py` — push payloads via `user_token`; extract `user_token` from a signed payload.
- `webapp/server.py` — `/x/connect` and `/x/callback` OAuth routes; worker startup hook.
- `init_db.py` — create the three new tables.

---

## Task 1: X config constants + feature flag

**Files:**
- Modify: `lfg_core/config.py`
- Test: `tests/test_x_db.py` (env defaults asserted in Task 3); no standalone test here.

- [ ] **Step 1: Add constants to `lfg_core/config.py`** (append near the other `os.getenv` blocks)

```python
# --- X (Twitter) tweet-to-mint integration (issue #41) ---
X_FEATURE_ENABLED = os.getenv("X_FEATURE_ENABLED", "false").strip().lower() == "true"
X_API_BASE = os.getenv("X_API_BASE", "https://api.twitter.com/2").rstrip("/")
X_UPLOAD_BASE = os.getenv("X_UPLOAD_BASE", "https://upload.twitter.com/1.1").rstrip("/")
X_OAUTH_AUTHORIZE_URL = os.getenv("X_OAUTH_AUTHORIZE_URL", "https://twitter.com/i/oauth2/authorize")
X_OAUTH_TOKEN_URL = os.getenv("X_OAUTH_TOKEN_URL", "https://api.twitter.com/2/oauth2/token")
X_CLIENT_ID = os.getenv("X_CLIENT_ID", "")
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET", "")
X_REDIRECT_URI = os.getenv("X_REDIRECT_URI", "")
# App bearer/credentials used by the worker to read mentions + post replies.
X_APP_BEARER_TOKEN = os.getenv("X_APP_BEARER_TOKEN", "")
# The bot account whose mentions we read and reply from.
X_ACCOUNT_ID = os.getenv("X_ACCOUNT_ID", "")          # numeric X user id
X_ACCOUNT_HANDLE = os.getenv("X_ACCOUNT_HANDLE", "letseffinggo")
X_MINT_COMMAND = os.getenv("X_MINT_COMMAND", "!mint")
X_POLL_INTERVAL_SECONDS = int(os.getenv("X_POLL_INTERVAL_SECONDS", "30"))
X_PAYMENT_TIMEOUT_SECONDS = int(os.getenv("X_PAYMENT_TIMEOUT_SECONDS", str(PAYMENT_TIMEOUT_SECONDS)))
# Abuse control: N failed/incomplete sessions within the window -> 24h blacklist.
X_BLACKLIST_THRESHOLD = int(os.getenv("X_BLACKLIST_THRESHOLD", "5"))
X_BLACKLIST_WINDOW_HOURS = int(os.getenv("X_BLACKLIST_WINDOW_HOURS", "24"))
X_BLACKLIST_BAN_HOURS = int(os.getenv("X_BLACKLIST_BAN_HOURS", "24"))
```

- [ ] **Step 2: Verify import still works**

Run: `cd /home/hamsa/LFG && python -c "from lfg_core import config; print(config.X_MINT_COMMAND, config.X_POLL_INTERVAL_SECONDS)"`
Expected: prints `!mint 30`

- [ ] **Step 3: Commit**

```bash
git add lfg_core/config.py
git commit -m "feat(x): add tweet-to-mint config constants and feature flag"
```

---

## Task 2: Database schema

**Files:**
- Modify: `init_db.py`
- Test: covered by Task 3 (`tests/test_x_db.py` creates the schema in a temp DB).

- [ ] **Step 1: Add a schema-creation function**

Add to `init_db.py` (and call it from the module's main init path alongside the existing table creators):

```python
def create_x_tables(db_path="lfg_nfts.db"):
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS x_links (
                x_user_id TEXT PRIMARY KEY,
                x_handle TEXT NOT NULL,
                discord_id TEXT,
                wallet TEXT NOT NULL,
                xumm_user_token TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS x_mint_sessions (
                tweet_id TEXT PRIMARY KEY,
                x_user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                nft_number INTEGER,
                reply_tweet_id TEXT,
                error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_x_sessions_user_time
                ON x_mint_sessions (x_user_id, created_at);
            CREATE TABLE IF NOT EXISTS x_ingest_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                since_id TEXT,
                last_poll_at TIMESTAMP
            );
            """
        )
        conn.commit()
    finally:
        conn.close()
```

- [ ] **Step 2: Verify it runs**

Run: `cd /home/hamsa/LFG && python -c "import init_db; init_db.create_x_tables('/tmp/x_test.db'); print('ok')"`
Expected: prints `ok`, no error.

- [ ] **Step 3: Commit**

```bash
git add init_db.py
git commit -m "feat(x): create x_links, x_mint_sessions, x_ingest_state tables"
```

---

## Task 3: x_db data-access layer

**Files:**
- Create: `lfg_core/x_db.py`
- Test: `tests/test_x_db.py`

Status constants used across the codebase: `PENDING_PAYMENT, PAID, MINTED, REPLIED, FAILED, EXPIRED`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_db.py
import os, sys, time
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import init_db
from lfg_core import x_db


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "x.db")
    init_db.create_x_tables(path)
    x_db.DB_PATH = path
    return path


def test_upsert_and_get_link(db):
    x_db.upsert_link("123", "alice", "disc1", "rWallet", "utok")
    link = x_db.get_link_by_x_user_id("123")
    assert link["x_handle"] == "alice"
    assert link["wallet"] == "rWallet"
    assert link["xumm_user_token"] == "utok"
    # re-link updates
    x_db.upsert_link("123", "alice2", "disc1", "rWallet2", "utok2")
    assert x_db.get_link_by_x_user_id("123")["wallet"] == "rWallet2"


def test_session_lifecycle(db):
    assert x_db.get_session("t1") is None
    assert x_db.create_session("t1", "123") is True
    # duplicate create is rejected (idempotency)
    assert x_db.create_session("t1", "123") is False
    x_db.update_session("t1", status=x_db.MINTED, nft_number=42)
    s = x_db.get_session("t1")
    assert s["status"] == x_db.MINTED and s["nft_number"] == 42


def test_blacklist_counts_recent_failures(db):
    x_db.create_session("t1", "123")
    x_db.update_session("t1", status=x_db.FAILED)
    x_db.create_session("t2", "123")
    x_db.update_session("t2", status=x_db.EXPIRED)
    # 2 failures within window, threshold 5 -> not blacklisted
    assert x_db.recent_failure_count("123", window_hours=24) == 2
    assert x_db.is_blacklisted("123", threshold=5, window_hours=24) is False
    assert x_db.is_blacklisted("123", threshold=2, window_hours=24) is True


def test_cursor_roundtrip(db):
    assert x_db.get_since_id() is None
    x_db.set_since_id("99999")
    assert x_db.get_since_id() == "99999"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.x_db'`

- [ ] **Step 3: Implement `lfg_core/x_db.py`**

```python
"""SQLite access for the X tweet-to-mint feature: account links, per-tweet
mint sessions, the mention-poll cursor, and failure-based blacklisting."""
import sqlite3

DB_PATH = "lfg_nfts.db"

# x_mint_sessions.status values
PENDING_PAYMENT = "pending_payment"
PAID = "paid"
MINTED = "minted"
REPLIED = "replied"
FAILED = "failed"
EXPIRED = "expired"

# Terminal statuses that count as a failed/incomplete attempt for blacklisting.
FAILURE_STATUSES = (FAILED, EXPIRED)


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_link(x_user_id, x_handle, discord_id, wallet, xumm_user_token):
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO x_links (x_user_id, x_handle, discord_id, wallet, xumm_user_token)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(x_user_id) DO UPDATE SET
                x_handle=excluded.x_handle,
                discord_id=excluded.discord_id,
                wallet=excluded.wallet,
                xumm_user_token=excluded.xumm_user_token,
                updated_at=CURRENT_TIMESTAMP
            """,
            (x_user_id, x_handle, discord_id, wallet, xumm_user_token),
        )


def get_link_by_x_user_id(x_user_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM x_links WHERE x_user_id = ?", (x_user_id,)
        ).fetchone()
        return dict(row) if row else None


def create_session(tweet_id, x_user_id):
    """Insert a new pending session. Returns False if tweet already seen."""
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO x_mint_sessions (tweet_id, x_user_id, status) VALUES (?, ?, ?)",
                (tweet_id, x_user_id, PENDING_PAYMENT),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_session(tweet_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM x_mint_sessions WHERE tweet_id = ?", (tweet_id,)
        ).fetchone()
        return dict(row) if row else None


def update_session(tweet_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(
            f"UPDATE x_mint_sessions SET {cols}, updated_at = CURRENT_TIMESTAMP "
            f"WHERE tweet_id = ?",
            (*fields.values(), tweet_id),
        )


def list_resumable_sessions():
    """Non-terminal sessions, for resuming after a restart."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM x_mint_sessions WHERE status IN (?, ?)",
            (PENDING_PAYMENT, PAID),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_failure_count(x_user_id, window_hours):
    placeholders = ", ".join("?" for _ in FAILURE_STATUSES)
    with _conn() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM x_mint_sessions
            WHERE x_user_id = ?
              AND status IN ({placeholders})
              AND created_at >= datetime('now', ?)
            """,
            (x_user_id, *FAILURE_STATUSES, f"-{int(window_hours)} hours"),
        ).fetchone()
        return row["n"]


def is_blacklisted(x_user_id, threshold, window_hours):
    return recent_failure_count(x_user_id, window_hours) >= threshold


def get_since_id():
    with _conn() as conn:
        row = conn.execute(
            "SELECT since_id FROM x_ingest_state WHERE id = 1"
        ).fetchone()
        return row["since_id"] if row else None


def set_since_id(since_id):
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO x_ingest_state (id, since_id, last_poll_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                since_id = excluded.since_id,
                last_poll_at = CURRENT_TIMESTAMP
            """,
            (since_id,),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_db.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_db.py tests/test_x_db.py
git commit -m "feat(x): x_db data layer for links, sessions, cursor, blacklist"
```

---

## Task 4: Strict command parser

**Files:**
- Create: `lfg_core/x_command.py`
- Test: `tests/test_x_command.py`

The command must appear as a standalone whitespace-delimited token so `!minted`
or `email!mint` do not trigger. Matching is case-insensitive.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_command.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lfg_core import x_command


def test_detects_command():
    assert x_command.is_mint_command("@letseffinggo !mint") is True
    assert x_command.is_mint_command("hey @letseffinggo  !mint please") is True
    assert x_command.is_mint_command("@letseffinggo !MINT") is True


def test_rejects_near_misses():
    assert x_command.is_mint_command("@letseffinggo mint") is False
    assert x_command.is_mint_command("@letseffinggo !minted") is False
    assert x_command.is_mint_command("@letseffinggo email!mint") is False
    assert x_command.is_mint_command("@letseffinggo love the art") is False
    assert x_command.is_mint_command("") is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_command.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.x_command'`

- [ ] **Step 3: Implement `lfg_core/x_command.py`**

```python
"""Pure parser for the strict tweet-to-mint command."""
import re
from lfg_core import config


def is_mint_command(text, command=None):
    """True if `text` contains the mint command as a standalone token."""
    if not text:
        return False
    command = command or config.X_MINT_COMMAND
    pattern = r"(?:^|\s)" + re.escape(command) + r"(?:\s|$)"
    return re.search(pattern, text, re.IGNORECASE) is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_command.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_command.py tests/test_x_command.py
git commit -m "feat(x): strict !mint command parser"
```

---

## Task 5: Mention filter chain

**Files:**
- Create: `lfg_core/x_filter.py`
- Test: `tests/test_x_filter.py`

A mention dict has at least `{"id": tweet_id, "author_id": x_user_id, "text": str}`.
`evaluate_mention` returns `(accepted: bool, reason: str)`. `reason` is `"ok"` on
accept, else one of: `feature_disabled`, `unlinked`, `blacklisted`, `no_command`,
`duplicate`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_filter.py
import os, sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import init_db
from lfg_core import x_db, x_filter


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "x.db")
    init_db.create_x_tables(path)
    x_db.DB_PATH = path
    return path


def _mention(tid="t1", aid="123", text="@letseffinggo !mint"):
    return {"id": tid, "author_id": aid, "text": text}


def test_disabled_feature_drops_all(db):
    accepted, reason = x_filter.evaluate_mention(_mention(), enabled=False)
    assert accepted is False and reason == "feature_disabled"


def test_unlinked_author_dropped(db):
    accepted, reason = x_filter.evaluate_mention(_mention(), enabled=True)
    assert accepted is False and reason == "unlinked"


def test_no_command_dropped(db):
    x_db.upsert_link("123", "alice", "d1", "rW", "utok")
    accepted, reason = x_filter.evaluate_mention(
        _mention(text="@letseffinggo gm"), enabled=True)
    assert accepted is False and reason == "no_command"


def test_blacklisted_dropped(db):
    x_db.upsert_link("123", "alice", "d1", "rW", "utok")
    for i in range(5):
        x_db.create_session(f"f{i}", "123")
        x_db.update_session(f"f{i}", status=x_db.FAILED)
    accepted, reason = x_filter.evaluate_mention(_mention(), enabled=True)
    assert accepted is False and reason == "blacklisted"


def test_duplicate_dropped(db):
    x_db.upsert_link("123", "alice", "d1", "rW", "utok")
    x_db.create_session("t1", "123")
    accepted, reason = x_filter.evaluate_mention(_mention(tid="t1"), enabled=True)
    assert accepted is False and reason == "duplicate"


def test_valid_mention_accepted(db):
    x_db.upsert_link("123", "alice", "d1", "rW", "utok")
    accepted, reason = x_filter.evaluate_mention(_mention(tid="new"), enabled=True)
    assert accepted is True and reason == "ok"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_filter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.x_filter'`

- [ ] **Step 3: Implement `lfg_core/x_filter.py`**

```python
"""Decide whether a mention should trigger a mint. Pure decision logic over
x_db state; performs no network or side effects."""
from lfg_core import config, x_db, x_command


def evaluate_mention(mention, enabled=None):
    """Return (accepted, reason). reason == 'ok' only when accepted."""
    if enabled is None:
        enabled = config.X_FEATURE_ENABLED
    if not enabled:
        return False, "feature_disabled"

    author_id = str(mention["author_id"])
    link = x_db.get_link_by_x_user_id(author_id)
    if link is None:
        return False, "unlinked"
    if x_db.is_blacklisted(author_id, config.X_BLACKLIST_THRESHOLD,
                           config.X_BLACKLIST_WINDOW_HOURS):
        return False, "blacklisted"
    if not x_command.is_mint_command(mention.get("text", "")):
        return False, "no_command"
    if x_db.get_session(str(mention["id"])) is not None:
        return False, "duplicate"
    return True, "ok"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_filter.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_filter.py tests/test_x_filter.py
git commit -m "feat(x): mention filter chain (linked/command/blacklist/dedup)"
```

---

## Task 6: Xaman push payloads + user_token extraction

**Files:**
- Modify: `lfg_core/xumm_ops.py`
- Test: `tests/test_x_client.py` is for the X client; add Xaman push tests here in `tests/test_xumm_push.py`.

XUMM delivers a payload as a push notification when the request body includes a
top-level `user_token`. The signed-payload GET response exposes the issued token
at `data["response"]["user_token"]` (a fresh token is returned on each signed
payload; persist the latest). **Verification step included below** — confirm the
field against the XUMM platform API response before finalizing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_xumm_push.py
import os, sys
import asyncio
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
from lfg_core import xumm_ops


def test_push_payment_includes_user_token():
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["body"] = json
        resp = MagicMock()
        resp.json.return_value = {
            "uuid": "u-1", "refs": {"qr_png": "q"},
            "next": {"always": "http://x"}, "pushed": True,
        }
        return resp

    with patch("lfg_core.xumm_ops.requests.post", side_effect=fake_post):
        out = asyncio.run(xumm_ops.create_payment_payload(
            "rDest", value="1", currency=None, issuer=None, user_token="utok"))
    assert out["uuid"] == "u-1"
    assert captured["body"]["user_token"] == "utok"


def test_extract_user_token_from_status():
    data = {"meta": {"signed": True}, "response": {"account": "rA", "user_token": "newtok"}}
    assert xumm_ops.extract_user_token(data) == "newtok"
    assert xumm_ops.extract_user_token({"response": {}}) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_xumm_push.py -v`
Expected: FAIL — `create_payment_payload() got an unexpected keyword argument 'user_token'` and `AttributeError: extract_user_token`.

- [ ] **Step 3: Implement the changes in `lfg_core/xumm_ops.py`**

Thread an optional `user_token` through `_create_xumm_payload` and the payload
creators, and add an extractor. Modify `_create_xumm_payload`:

```python
async def _create_xumm_payload(txjson: dict, options: dict = None,
                               user_token: str = None):
    """POST a payload to the XUMM platform API; returns qr/deeplink dict or None.
    When user_token is set, XUMM delivers the request as a push notification to
    that user's Xaman instead of (only) returning a QR."""
    payload = {"txjson": txjson}
    if options:
        payload["options"] = options
    if user_token:
        payload["user_token"] = user_token
    try:
        response = await asyncio.to_thread(
            requests.post, config.XUMM_API_URL, json=payload,
            headers=_XUMM_HEADERS, timeout=10
        )
        data = response.json()
        return {
            'qr_url': data['refs']['qr_png'],
            'xumm_url': data['next']['always'],
            'uuid': data['uuid'],
            'pushed': bool(data.get('pushed')),
        }
    except Exception as e:
        logging.error(f"Error creating XUMM payload: {e}")
        return None
```

Add `user_token=None` to `create_payment_payload` and `create_accept_offer_payload`
signatures and pass it through:

```python
async def create_payment_payload(destination: str, value: str = "1",
                                 currency: str = None, issuer: str = None,
                                 expire_minutes: int = None,
                                 return_url: dict = None,
                                 user_token: str = None):
    if expire_minutes is None:
        expire_minutes = max(1, -(-config.PAYMENT_TIMEOUT_SECONDS // 60))
    return await _create_xumm_payload(
        {
            "TransactionType": "Payment",
            "Destination": destination,
            "Amount": _payment_amount(value, currency, issuer),
        },
        options=_with_return_url({"expire": expire_minutes}, return_url),
        user_token=user_token,
    )


async def create_accept_offer_payload(offer_id: str, return_url: dict = None,
                                      user_token: str = None):
    return await _create_xumm_payload({
        "TransactionType": "NFTokenAcceptOffer",
        "NFTokenSellOffer": offer_id,
    }, options=_with_return_url({}, return_url), user_token=user_token)
```

Add the extractor near `get_payload_status`:

```python
def extract_user_token(payload_data: dict):
    """Pull the issued Xaman push user_token from a payload GET response.
    XUMM returns a fresh token under response.user_token each time a user
    signs a payload; callers persist the latest one for future pushes."""
    return ((payload_data or {}).get("response") or {}).get("user_token")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_xumm_push.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Verify the XUMM field against the live API contract**

Read the XUMM platform API docs / a real signed-payload response and confirm the
push token is at `response.user_token` (and that a top-level `user_token` in the
create body triggers push). If the field differs, update `extract_user_token` and
the test accordingly. Run: `cd /home/hamsa/LFG && python -m pytest tests/test_xumm_push.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lfg_core/xumm_ops.py tests/test_xumm_push.py
git commit -m "feat(x): XUMM push payloads via user_token + token extraction"
```

---

## Task 7: X API client

**Files:**
- Create: `lfg_core/x_client.py`
- Test: `tests/test_x_client.py`

Thin wrapper over the X API v2 REST endpoints, each HTTP call run via
`asyncio.to_thread(requests.*)` to match the codebase style. Network is mocked in
tests; this task verifies request shaping and response parsing, not live calls.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_client.py
import os, sys, asyncio
from unittest.mock import patch, MagicMock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test"); os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test"); os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ["X_ACCOUNT_ID"] = "999"
from lfg_core import x_client


def _resp(json_body, status=200):
    r = MagicMock(); r.json.return_value = json_body; r.status_code = status; return r


def test_get_mentions_parses_and_passes_since_id():
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url; captured["params"] = params
        return _resp({"data": [
            {"id": "t1", "author_id": "a1", "text": "@letseffinggo !mint"}]})

    with patch("lfg_core.x_client.requests.get", side_effect=fake_get):
        out = asyncio.run(x_client.get_mentions(since_id="55"))
    assert out[0]["id"] == "t1"
    assert captured["params"]["since_id"] == "55"
    assert "999/mentions" in captured["url"]


def test_get_mentions_empty_data_returns_list():
    with patch("lfg_core.x_client.requests.get", side_effect=lambda *a, **k: _resp({})):
        assert asyncio.run(x_client.get_mentions(since_id=None)) == []


def test_post_reply_sends_in_reply_to_and_media():
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url; captured["body"] = json
        return _resp({"data": {"id": "reply1"}})

    with patch("lfg_core.x_client.requests.post", side_effect=fake_post):
        rid = asyncio.run(x_client.post_reply("t1", "gm", media_ids=["m1"]))
    assert rid == "reply1"
    assert captured["body"]["reply"]["in_reply_to_tweet_id"] == "t1"
    assert captured["body"]["media"]["media_ids"] == ["m1"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.x_client'`

- [ ] **Step 3: Implement `lfg_core/x_client.py`**

```python
"""Thin async wrapper over the X (Twitter) API v2 used by the tweet-to-mint
worker: read @account mentions, upload media, post image replies. All HTTP runs
on a thread to match the codebase's requests-in-to_thread pattern."""
import asyncio
import logging
import requests
from lfg_core import config


def _auth_headers():
    return {"Authorization": f"Bearer {config.X_APP_BEARER_TOKEN}"}


async def get_mentions(since_id=None, max_results=20):
    """Return a list of mention dicts ({id, author_id, text, ...}) newest-first
    as provided by X. Empty list on no data or error."""
    url = f"{config.X_API_BASE}/users/{config.X_ACCOUNT_ID}/mentions"
    params = {
        "max_results": max_results,
        "tweet.fields": "author_id,created_at",
    }
    if since_id:
        params["since_id"] = since_id
    try:
        resp = await asyncio.to_thread(
            requests.get, url, headers=_auth_headers(), params=params, timeout=15)
        return (resp.json() or {}).get("data", []) or []
    except Exception as e:
        logging.error(f"X get_mentions failed: {e}")
        return []


async def upload_media(image_bytes, mime="image/png"):
    """Upload media via the v1.1 media endpoint; return media_id_string or None."""
    url = f"{config.X_UPLOAD_BASE}/media/upload.json"
    try:
        resp = await asyncio.to_thread(
            requests.post, url, headers=_auth_headers(),
            files={"media": ("nft.png", image_bytes, mime)}, timeout=30)
        return (resp.json() or {}).get("media_id_string")
    except Exception as e:
        logging.error(f"X upload_media failed: {e}")
        return None


async def post_reply(in_reply_to_tweet_id, text, media_ids=None):
    """Post a reply tweet. Returns the new tweet id or None. No URL in `text`
    keeps it on the cheap ($0.015) posting tier."""
    url = f"{config.X_API_BASE}/tweets"
    body = {"text": text, "reply": {"in_reply_to_tweet_id": in_reply_to_tweet_id}}
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    try:
        resp = await asyncio.to_thread(
            requests.post, url, headers=_auth_headers(), json=body, timeout=15)
        return ((resp.json() or {}).get("data") or {}).get("id")
    except Exception as e:
        logging.error(f"X post_reply failed: {e}")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_client.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_client.py tests/test_x_client.py
git commit -m "feat(x): X API v2 client (mentions, media upload, reply)"
```

---

## Task 8: Social mint orchestrator

**Files:**
- Create: `lfg_core/x_mint.py`
- Test: `tests/test_x_mint.py`

`process_session(tweet_id)` drives one session forward. It is keyed by `tweet_id`,
reads/writes `x_mint_sessions`, and is safe to call again on a session at any
non-terminal status (resume). External effects — payment push, the mint pipeline,
the reply, the accept push — are injected as awaitable callables so the test can
stub them; production wiring passes the real functions.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_mint.py
import os, sys, asyncio
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test"); os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test"); os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
import init_db
from lfg_core import x_db, x_mint


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "x.db")
    init_db.create_x_tables(path)
    x_db.DB_PATH = path
    x_db.upsert_link("123", "alice", "d1", "rWallet", "utok")
    x_db.create_session("t1", "123")
    return path


def _ok_deps():
    async def pay(link): return True                      # payment signed
    async def mint(link): return {"nft_number": 7, "nft_id": "NID",
                                  "image_bytes": b"img", "offer_id": "OID",
                                  "traits_caption": "LFG #7"}
    async def reply(tweet_id, caption, image_bytes): return "reply1"
    async def accept(link, offer_id): return True
    return dict(pay=pay, mint=mint, reply=reply, accept=accept)


def test_happy_path(db):
    asyncio.run(x_mint.process_session("t1", **_ok_deps()))
    s = x_db.get_session("t1")
    assert s["status"] == x_db.REPLIED
    assert s["nft_number"] == 7
    assert s["reply_tweet_id"] == "reply1"


def test_payment_timeout_marks_expired(db):
    deps = _ok_deps()
    async def no_pay(link): return False
    deps["pay"] = no_pay
    asyncio.run(x_mint.process_session("t1", **deps))
    assert x_db.get_session("t1")["status"] == x_db.EXPIRED


def test_mint_failure_marks_failed(db):
    deps = _ok_deps()
    async def bad_mint(link): return None
    deps["mint"] = bad_mint
    asyncio.run(x_mint.process_session("t1", **deps))
    assert x_db.get_session("t1")["status"] == x_db.FAILED


def test_reply_runs_before_accept(db):
    order = []
    deps = _ok_deps()
    base_reply, base_accept = deps["reply"], deps["accept"]
    async def reply(*a): order.append("reply"); return await base_reply(*a)
    async def accept(*a): order.append("accept"); return await base_accept(*a)
    deps["reply"], deps["accept"] = reply, accept
    asyncio.run(x_mint.process_session("t1", **deps))
    assert order == ["reply", "accept"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_mint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.x_mint'`

- [ ] **Step 3: Implement `lfg_core/x_mint.py`**

```python
"""Social-mint orchestrator: drive one tweet-keyed mint session forward through
payment -> mint -> reply -> accept. External effects are injected so the state
machine is unit-testable; `process_tweet` wires the real implementations."""
import logging
from lfg_core import x_db


async def process_session(tweet_id, pay, mint, reply, accept):
    """Advance the session for `tweet_id`. Forward-only; safe to re-invoke.

    pay(link) -> bool                : push payment, await signed. False = timeout.
    mint(link) -> dict|None          : run mint pipeline; dict has nft_number,
                                       nft_id, image_bytes, offer_id, traits_caption.
                                       None = mint failed.
    reply(tweet_id, caption, image)  : post image reply; returns reply tweet id.
    accept(link, offer_id) -> bool   : push accept-offer payload to Xaman.
    """
    session = x_db.get_session(tweet_id)
    if session is None or session["status"] in (
            x_db.REPLIED, x_db.FAILED, x_db.EXPIRED):
        return
    link = x_db.get_link_by_x_user_id(session["x_user_id"])
    if link is None:
        x_db.update_session(tweet_id, status=x_db.FAILED, error="link missing")
        return

    try:
        if not await pay(link):
            x_db.update_session(tweet_id, status=x_db.EXPIRED,
                                error="payment not signed in time")
            return
        x_db.update_session(tweet_id, status=x_db.PAID)

        result = await mint(link)
        if not result:
            x_db.update_session(tweet_id, status=x_db.FAILED, error="mint failed")
            return
        x_db.update_session(tweet_id, status=x_db.MINTED,
                            nft_number=result["nft_number"])

        reply_id = await reply(tweet_id, result["traits_caption"],
                               result["image_bytes"])
        x_db.update_session(tweet_id, status=x_db.REPLIED, reply_tweet_id=reply_id)

        # Best-effort: the accept push must not flip a successful mint to failed.
        try:
            await accept(link, result["offer_id"])
        except Exception:
            logging.error(f"accept push failed for {tweet_id}", exc_info=True)
    except Exception as e:
        logging.error(f"x_mint.process_session error for {tweet_id}: {e}",
                      exc_info=True)
        x_db.update_session(tweet_id, status=x_db.FAILED, error=str(e))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_mint.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_mint.py tests/test_x_mint.py
git commit -m "feat(x): social-mint orchestrator state machine"
```

---

## Task 9: Production dependency wiring for the orchestrator

**Files:**
- Modify: `lfg_core/x_mint.py` (add `process_tweet`)
- Test: `tests/test_x_mint.py` (add a wiring test with all I/O patched)

`process_tweet(tweet_id)` builds the four real dependency callables and calls
`process_session`. The payment dep pushes a Xaman payment and polls
`get_payload_status` until signed/expired; the mint dep runs a `MintSession`
through `run_mint_session` and gathers the image bytes + offer id; the reply dep
uploads media and posts; the accept dep pushes the accept-offer payload.

- [ ] **Step 1: Write the failing test** (append to `tests/test_x_mint.py`)

```python
def test_process_tweet_wires_real_deps(db, monkeypatch):
    import lfg_core.x_mint as xm

    async def fake_pay_dep(link): return True
    async def fake_mint_dep(link): return {"nft_number": 9, "nft_id": "N",
        "image_bytes": b"x", "offer_id": "O", "traits_caption": "LFG #9"}
    async def fake_reply_dep(t, c, i): return "r9"
    async def fake_accept_dep(link, oid): return True

    monkeypatch.setattr(xm, "_make_pay_dep", lambda: fake_pay_dep)
    monkeypatch.setattr(xm, "_make_mint_dep", lambda: fake_mint_dep)
    monkeypatch.setattr(xm, "_make_reply_dep", lambda: fake_reply_dep)
    monkeypatch.setattr(xm, "_make_accept_dep", lambda: fake_accept_dep)

    import asyncio
    asyncio.run(xm.process_tweet("t1"))
    assert x_db.get_session("t1")["status"] == x_db.REPLIED
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_mint.py::test_process_tweet_wires_real_deps -v`
Expected: FAIL — `AttributeError: module 'lfg_core.x_mint' has no attribute '_make_pay_dep'`

- [ ] **Step 3: Implement the wiring in `lfg_core/x_mint.py`**

```python
import asyncio
import time
from lfg_core import (config, xumm_ops, xrpl_ops, mint_flow, x_client, x_db)


def _make_pay_dep():
    async def pay(link):
        params = _payment_params_for(link)
        payload = await xumm_ops.create_payment_payload(
            params["destination"], value=params["value"],
            currency=params["currency"], issuer=params["issuer"],
            user_token=link["xumm_user_token"])
        if not payload:
            return False
        deadline = time.time() + config.X_PAYMENT_TIMEOUT_SECONDS
        while time.time() < deadline:
            status = await xumm_ops.get_payload_status(payload["uuid"])
            if status and status.get("signed"):
                return True
            if status and status.get("expired"):
                return False
            await asyncio.sleep(3)
        return False
    return pay


def _payment_params_for(link):
    """LFGO holders burn LFGO to the issuer; everyone else pays XRP to the bot."""
    # Mirror MintSession.prepare_payment's path detection, minus the UI.
    return dict(destination=config.TOKEN_ISSUER_ADDRESS,
                value=config.MINT_PRICE_LFGO,
                currency=config.TOKEN_CURRENCY_HEX,
                issuer=config.TOKEN_ISSUER_ADDRESS)


def _make_mint_dep():
    async def mint(link):
        session = mint_flow.MintSession(
            discord_id=link.get("discord_id") or f"x:{link['x_user_id']}",
            wallet_address=link["wallet"])
        await session.prepare_payment()  # path detection only; payment already signed
        await mint_flow.run_mint_session(session)
        if session.nft_id is None or session.image_url is None:
            return None
        image_bytes = await _fetch_bytes(session.image_url)
        # Re-derive the sell offer id the mint session created.
        return {
            "nft_number": session.nft_number,
            "nft_id": session.nft_id,
            "image_bytes": image_bytes,
            "offer_id": getattr(session, "offer_id", None),
            "traits_caption": _caption(session),
        }
    return mint
```

> Implementation note for the executor: `run_mint_session` already waits for
> payment internally via `xrpl_ops.wait_for_payment`. Because the X flow has
> already confirmed the Xaman signature in `pay`, prefer extracting the
> post-payment body of `run_mint_session` into a reusable
> `mint_flow.run_mint_after_payment(session)` so the X path does not wait for
> payment twice. Add `session.offer_id = offer_id` where the offer is created in
> `mint_flow` so the caption/accept step can read it. Make these two small
> refactors in `lfg_core/mint_flow.py` as part of this step, keeping the existing
> Discord flow green (run `python -m pytest webapp/test_smoke.py`).

```python
async def _fetch_bytes(url):
    import requests
    resp = await asyncio.to_thread(requests.get, url, timeout=30)
    return resp.content


def _caption(session):
    return f"{config.NFT_COLLECTION_NAME} #{session.nft_number} minted! 🐸 #LFG"


def _make_reply_dep():
    async def reply(tweet_id, caption, image_bytes):
        media_id = await x_client.upload_media(image_bytes)
        media_ids = [media_id] if media_id else None
        return await x_client.post_reply(tweet_id, caption, media_ids=media_ids)
    return reply


def _make_accept_dep():
    async def accept(link, offer_id):
        if not offer_id:
            return False
        payload = await xumm_ops.create_accept_offer_payload(
            offer_id, user_token=link["xumm_user_token"])
        return bool(payload)
    return accept


async def process_tweet(tweet_id):
    await process_session(
        tweet_id,
        pay=_make_pay_dep(),
        mint=_make_mint_dep(),
        reply=_make_reply_dep(),
        accept=_make_accept_dep(),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_mint.py webapp/test_smoke.py -v`
Expected: PASS (all X mint tests + existing smoke tests stay green)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_mint.py lfg_core/mint_flow.py tests/test_x_mint.py
git commit -m "feat(x): wire orchestrator to real payment/mint/reply/accept deps"
```

---

## Task 10: Ingestion worker

**Files:**
- Create: `lfg_core/x_ingest.py`
- Test: `tests/test_x_ingest.py`

`poll_once()` fetches mentions since the cursor, evaluates each via `x_filter`,
creates a session + schedules `process_tweet` for accepted ones, advances the
cursor to the newest id seen, and returns a per-mention result list for testing.
`run_worker()` loops `poll_once` every `X_POLL_INTERVAL_SECONDS` while the feature
flag is on, and resumes non-terminal sessions on startup.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_ingest.py
import os, sys, asyncio
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test"); os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test"); os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
import init_db
from lfg_core import x_db, x_ingest


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "x.db")
    init_db.create_x_tables(path)
    x_db.DB_PATH = path
    x_db.upsert_link("123", "alice", "d1", "rW", "utok")
    return path


def test_poll_once_accepts_valid_and_advances_cursor(db, monkeypatch):
    mentions = [
        {"id": "10", "author_id": "123", "text": "@letseffinggo !mint"},
        {"id": "11", "author_id": "999", "text": "@letseffinggo !mint"},  # unlinked
    ]
    async def fake_mentions(since_id=None, max_results=20): return mentions
    scheduled = []
    async def fake_process(tweet_id): scheduled.append(tweet_id)
    monkeypatch.setattr(x_ingest.x_client, "get_mentions", fake_mentions)
    monkeypatch.setattr(x_ingest, "schedule_process", fake_process)

    results = asyncio.run(x_ingest.poll_once(enabled=True))
    reasons = {r["id"]: r["reason"] for r in results}
    assert reasons["10"] == "ok" and reasons["11"] == "unlinked"
    assert x_db.get_session("10") is not None
    assert scheduled == ["10"]
    assert x_db.get_since_id() == "11"  # newest id, regardless of accept


def test_poll_once_disabled_noop(db, monkeypatch):
    async def fake_mentions(**k): raise AssertionError("should not poll")
    monkeypatch.setattr(x_ingest.x_client, "get_mentions", fake_mentions)
    assert asyncio.run(x_ingest.poll_once(enabled=False)) == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lfg_core.x_ingest'`

- [ ] **Step 3: Implement `lfg_core/x_ingest.py`**

```python
"""Mention ingestion worker: poll @account mentions, filter, and schedule a mint
per accepted tweet. Polling-only for the initial rollout (no filtered stream)."""
import asyncio
import logging
from lfg_core import config, x_client, x_db, x_filter, x_mint


async def schedule_process(tweet_id):
    """Run a mint session as a background task (overridable in tests)."""
    asyncio.create_task(x_mint.process_tweet(tweet_id))


def _max_id(ids):
    """X ids are snowflakes — numerically increasing. Return the largest."""
    return max(ids, key=int) if ids else None


async def poll_once(enabled=None):
    if enabled is None:
        enabled = config.X_FEATURE_ENABLED
    if not enabled:
        return []
    mentions = await x_client.get_mentions(since_id=x_db.get_since_id())
    results = []
    for m in mentions:
        accepted, reason = x_filter.evaluate_mention(m, enabled=True)
        if accepted and x_db.create_session(str(m["id"]), str(m["author_id"])):
            await schedule_process(str(m["id"]))
        results.append({"id": str(m["id"]), "reason": reason})
    newest = _max_id([str(m["id"]) for m in mentions])
    if newest:
        x_db.set_since_id(newest)
    return results


async def resume_inflight():
    """On startup, re-drive sessions that were mid-flight before a restart."""
    for s in x_db.list_resumable_sessions():
        await schedule_process(s["tweet_id"])


async def run_worker():
    if not config.X_FEATURE_ENABLED:
        logging.info("X tweet-to-mint worker disabled (X_FEATURE_ENABLED=false)")
        return
    await resume_inflight()
    logging.info("X tweet-to-mint worker started (%ss poll)",
                 config.X_POLL_INTERVAL_SECONDS)
    while config.X_FEATURE_ENABLED:
        try:
            await poll_once(enabled=True)
        except Exception:
            logging.error("X poll_once failed", exc_info=True)
        await asyncio.sleep(config.X_POLL_INTERVAL_SECONDS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_ingest.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_ingest.py tests/test_x_ingest.py
git commit -m "feat(x): mention ingestion worker (poll, filter, schedule)"
```

---

## Task 11: Webapp X OAuth link routes

**Files:**
- Modify: `webapp/server.py`
- Test: `tests/test_x_link_route.py`

Two routes on the existing authenticated app:
- `GET /x/connect` → redirect the signed-in user to X's OAuth2 (PKCE) authorize URL,
  storing `code_verifier` + `state` in their session.
- `GET /x/callback` → exchange `code` for an X access token, fetch the X user
  (`/2/users/me`) to get `x_user_id` + `x_handle`, run a Xaman SignIn payload,
  capture `user_token` via `xumm_ops.extract_user_token`, and `x_db.upsert_link`.

Capturing the Xaman `user_token` requires the user to complete a SignIn in Xaman.
For the initial rollout, the callback creates a SignIn payload and returns its
deep-link/QR to the user; a follow-up poll (reusing `get_payload_status` +
`extract_user_token`) finalizes the link once signed. Encapsulate the linking
write in a pure helper `finalize_link(x_user_id, x_handle, wallet, user_token,
discord_id)` so it is unit-testable without HTTP.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_link_route.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test"); os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test"); os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
import init_db
from lfg_core import x_db
from webapp import server


def test_finalize_link_writes_row(tmp_path):
    path = str(tmp_path / "x.db"); init_db.create_x_tables(path); x_db.DB_PATH = path
    server.finalize_link(x_user_id="55", x_handle="bob", wallet="rBob",
                         user_token="utok", discord_id="d9")
    link = x_db.get_link_by_x_user_id("55")
    assert link["x_handle"] == "bob" and link["wallet"] == "rBob"
    assert link["xumm_user_token"] == "utok"


def test_routes_registered():
    paths = {r.resource.canonical for r in server.build_app().router.routes()
             if r.resource is not None}
    assert "/x/connect" in paths and "/x/callback" in paths
```

> If `webapp/server.py` builds its app differently (e.g. an `init_app()` or a
> module-level `app`), adapt `test_routes_registered` to that constructor — check
> how `webapp/test_smoke.py` obtains the app and mirror it.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_link_route.py -v`
Expected: FAIL — `AttributeError: module 'webapp.server' has no attribute 'finalize_link'`

- [ ] **Step 3: Implement in `webapp/server.py`**

Add the helper and routes (follow the existing Discord OAuth + session pattern in
the file for `require_session`, token storage, and redirects):

```python
import base64, hashlib, secrets
import requests
from lfg_core import config, xumm_ops, x_db


def finalize_link(x_user_id, x_handle, wallet, user_token, discord_id=None):
    """Persist an X<->wallet link. Pure DB write; unit-testable."""
    x_db.upsert_link(str(x_user_id), x_handle, discord_id, wallet, user_token)


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def handle_x_connect(request):
    session = _require_session(request)            # existing helper in this file
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    session["x_oauth"] = {"verifier": verifier, "state": state}
    params = {
        "response_type": "code", "client_id": config.X_CLIENT_ID,
        "redirect_uri": config.X_REDIRECT_URI, "scope": "tweet.read users.read",
        "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    from urllib.parse import urlencode
    return web.HTTPFound(f"{config.X_OAUTH_AUTHORIZE_URL}?{urlencode(params)}")


async def handle_x_callback(request):
    session = _require_session(request)
    saved = session.get("x_oauth") or {}
    if request.query.get("state") != saved.get("state"):
        return web.json_response({"error": "bad state"}, status=400)
    code = request.query.get("code")
    token = await asyncio.to_thread(
        requests.post, config.X_OAUTH_TOKEN_URL,
        data={"grant_type": "authorization_code", "code": code,
              "client_id": config.X_CLIENT_ID,
              "redirect_uri": config.X_REDIRECT_URI,
              "code_verifier": saved.get("verifier")},
        auth=(config.X_CLIENT_ID, config.X_CLIENT_SECRET), timeout=15)
    access = (token.json() or {}).get("access_token")
    me = await asyncio.to_thread(
        requests.get, f"{config.X_API_BASE}/users/me",
        headers={"Authorization": f"Bearer {access}"}, timeout=15)
    user = (me.json() or {}).get("data") or {}
    # Kick off the Xaman SignIn to capture a push user_token.
    signin = await xumm_ops.create_signin_payload()
    session["x_pending"] = {"x_user_id": user.get("id"),
                            "x_handle": user.get("username"),
                            "signin_uuid": signin and signin["uuid"]}
    return web.json_response({"x_handle": user.get("username"),
                              "xaman_signin_url": signin and signin["xumm_url"]})
```

> The executor wires a finalize step that polls the SignIn payload
> (`xumm_ops.get_payload_status` + `xumm_ops.extract_user_token`), reads the
> signed-in wallet from `status["account"]`, and calls `finalize_link(...)`. Add
> the two routes in the same place the file registers its other routes:
> `app.router.add_get("/x/connect", handle_x_connect)` and
> `app.router.add_get("/x/callback", handle_x_callback)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_link_route.py webapp/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add webapp/server.py tests/test_x_link_route.py
git commit -m "feat(x): webapp X OAuth link routes + finalize_link helper"
```

---

## Task 12: Worker startup + admin toggle

**Files:**
- Modify: `webapp/server.py` (start `x_ingest.run_worker` as a background task on app startup, guarded by the flag)
- Modify: `main.py` (admin panel control to flip `X_FEATURE_ENABLED` at runtime — store the live flag in a small settings row or module global so the worker loop and filters honor it)
- Test: `tests/test_x_ingest.py` (add a toggle test)

- [ ] **Step 1: Write the failing test** (append to `tests/test_x_ingest.py`)

```python
def test_set_enabled_runtime_toggle(db):
    from lfg_core import x_ingest
    x_ingest.set_enabled(False)
    assert x_ingest.is_enabled() is False
    x_ingest.set_enabled(True)
    assert x_ingest.is_enabled() is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_ingest.py::test_set_enabled_runtime_toggle -v`
Expected: FAIL — `AttributeError: module 'lfg_core.x_ingest' has no attribute 'set_enabled'`

- [ ] **Step 3: Implement the runtime toggle in `lfg_core/x_ingest.py`**

```python
# module-level live flag, seeded from config but flippable by admin at runtime
_enabled = config.X_FEATURE_ENABLED


def is_enabled():
    return _enabled


def set_enabled(value):
    global _enabled
    _enabled = bool(value)
```

Then change `poll_once`/`run_worker` to default `enabled` from `is_enabled()`
instead of `config.X_FEATURE_ENABLED`, and have `run_worker` loop on
`while is_enabled():` after an initial `if not is_enabled(): return`-free start so
it can be toggled on later (start the worker unconditionally; it idles when
disabled). In `webapp/server.py`'s app-startup hook, schedule it:

```python
async def _start_x_worker(app):
    from lfg_core import x_ingest
    app["x_worker"] = asyncio.create_task(x_ingest.run_worker())

# where the app is built:
app.on_startup.append(_start_x_worker)
```

Adjust `run_worker` to poll on an interval whenever enabled and sleep-check when
disabled (so an admin toggle resumes it without a restart):

```python
async def run_worker():
    await resume_inflight()
    while True:
        if is_enabled():
            try:
                await poll_once(enabled=True)
            except Exception:
                logging.error("X poll_once failed", exc_info=True)
        await asyncio.sleep(config.X_POLL_INTERVAL_SECONDS)
```

In `main.py`, add an admin-panel button/command that calls
`x_ingest.set_enabled(True/False)` and reports the new state.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_ingest.py webapp/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add lfg_core/x_ingest.py webapp/server.py main.py tests/test_x_ingest.py
git commit -m "feat(x): worker startup hook + runtime admin enable/disable toggle"
```

---

## Task 13: End-to-end integration test

**Files:**
- Test: `tests/test_x_e2e.py`

Exercises mention → filter → session → orchestrator → reply with X + Xaman + the
mint pipeline all stubbed, plus a restart/resume case and a blacklist case.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_x_e2e.py
import os, sys, asyncio
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test"); os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test"); os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
import init_db
from lfg_core import x_db, x_ingest, x_mint


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "x.db"); init_db.create_x_tables(path); x_db.DB_PATH = path
    x_db.upsert_link("123", "alice", "d1", "rW", "utok")
    return path


def test_end_to_end_happy_path(db, monkeypatch):
    mentions = [{"id": "100", "author_id": "123", "text": "@letseffinggo !mint"}]
    async def fake_mentions(since_id=None, max_results=20): return mentions
    monkeypatch.setattr(x_ingest.x_client, "get_mentions", fake_mentions)

    async def fake_process(tweet_id):
        # stub the orchestrator's deps for a clean success
        async def pay(l): return True
        async def mint(l): return {"nft_number": 1, "nft_id": "N",
            "image_bytes": b"x", "offer_id": "O", "traits_caption": "LFG #1"}
        async def reply(t, c, i): return "r1"
        async def accept(l, o): return True
        await x_mint.process_session(tweet_id, pay, mint, reply, accept)
    monkeypatch.setattr(x_ingest, "schedule_process", fake_process)

    asyncio.run(x_ingest.poll_once(enabled=True))
    assert x_db.get_session("100")["status"] == x_db.REPLIED
    assert x_db.get_since_id() == "100"


def test_resume_after_restart(db):
    x_db.create_session("200", "123")
    x_db.update_session("200", status=x_db.PAID)
    resumed = []
    async def fake_process(tid): resumed.append(tid)
    import lfg_core.x_ingest as xi
    xi.schedule_process = fake_process
    asyncio.run(xi.resume_inflight())
    assert resumed == ["200"]


def test_blacklist_blocks_after_five_failures(db, monkeypatch):
    for i in range(5):
        x_db.create_session(f"f{i}", "123"); x_db.update_session(f"f{i}", status=x_db.FAILED)
    mentions = [{"id": "300", "author_id": "123", "text": "@letseffinggo !mint"}]
    async def fake_mentions(**k): return mentions
    monkeypatch.setattr(x_ingest.x_client, "get_mentions", fake_mentions)
    results = asyncio.run(x_ingest.poll_once(enabled=True))
    assert results[0]["reason"] == "blacklisted"
    assert x_db.get_session("300") is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /home/hamsa/LFG && python -m pytest tests/test_x_e2e.py -v`
Expected: FAIL initially if any wiring detail is off; fix until green.

- [ ] **Step 3: Make it pass**

No new production code should be required — if a test fails, the defect is in an
earlier task's implementation; fix it there. (If `poll_once` schedules via the
module attribute `schedule_process`, ensure `test_resume_after_restart`'s
monkeypatch target matches how `resume_inflight` calls it.)

- [ ] **Step 4: Run the full suite**

Run: `cd /home/hamsa/LFG && python -m pytest tests/ webapp/test_smoke.py -v`
Expected: PASS (all X tests + existing suite)

- [ ] **Step 5: Commit**

```bash
git add tests/test_x_e2e.py
git commit -m "test(x): end-to-end tweet-to-mint integration (happy/resume/blacklist)"
```

---

## Self-Review Notes (spec coverage)

- **Linking via X OAuth + Xaman user_token** → Tasks 6, 11.
- **Mention ingestion, 30s polling, owned-reads cursor** → Tasks 7, 10.
- **Strict `!mint` + payment paywall** → Tasks 4, 8, 9.
- **Image-only reply (no URL)** → Tasks 7, 9 (`post_reply`, caption has no URL).
- **Push payment + accept to Xaman** → Tasks 6, 9.
- **x_links / x_mint_sessions / x_ingest_state + tweet-id idempotency** → Tasks 2, 3, 8.
- **Failed-attempt blacklist (5/24h → 24h ban)** → Tasks 3, 5, 13.
- **Admin toggle / feature flag (default off)** → Tasks 1, 12.
- **Restart-safe resume** → Tasks 3 (`list_resumable_sessions`), 10, 13.
- **Polling-only rollout (no stream)** → Task 10.

**Open implementation risk to confirm during execution:** the exact XUMM field for
the push `user_token` (Task 6, Step 5) and the precise reuse seam in
`mint_flow.run_mint_session` so the X path doesn't double-wait for payment
(Task 9, Step 3 note).
