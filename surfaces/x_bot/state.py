"""x_state.db: dedup/budget/pause store for the X posting surface (#41).

Two tables in one sqlite file (path always passed explicitly by the caller —
callers use `config.X_STATE_DB_PATH`; no module-level connection/path is
cached here, so tests just point at a `tmp_path` file with a plain string
argument, no fixture needed):

- `x_posts` — one row per tweet-worthy event (`event_key`, e.g.
  "mint:<nft_id>"), recording whether/how it was posted. Both dedup
  (`already_posted`) and monthly budget accounting (`month_count`) read this
  table. Schema copied verbatim from spec §5.5.
- `settings` — a generic key/value table (spec §5.6); today holds one row,
  `posting_paused` ("1"/"0"), the runtime kill switch a Discord admin button
  flips via a PR-2 service endpoint.

Table creation is idempotent (`CREATE TABLE IF NOT EXISTS`) and runs on every
connect — this module never assumes the file/tables already exist.

Budget months are always computed in UTC, never local time:
`datetime.now(timezone.utc)` when `now` is omitted; an explicit tz-aware
`now` is normalized to UTC, and a naive one is treated as already-UTC. This
keeps the monthly cutoff unambiguous and reproducible in tests regardless of
the host's local timezone.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# Schema verbatim from spec §5.5 (x_posts) / §5.6 (settings).
_CREATE_X_POSTS = """
CREATE TABLE IF NOT EXISTS x_posts (
  event_key   TEXT PRIMARY KEY,
  tweet_id    TEXT,
  posted_at   TIMESTAMP,
  month       TEXT,
  status      TEXT
)
"""

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
)
"""

_POSTING_PAUSED_KEY = "posting_paused"


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_X_POSTS)
    conn.execute(_CREATE_SETTINGS)
    return conn


def _to_utc(now: datetime | None) -> datetime:
    """Normalize `now` to a UTC datetime; default to the current UTC time."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def already_posted(path: str, event_key: str) -> bool:
    """True only when `event_key` has a row with status == 'posted'."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT status FROM x_posts WHERE event_key = ?", (event_key,)
        ).fetchone()
        return row is not None and row[0] == "posted"
    finally:
        conn.close()


def record(
    path: str,
    event_key: str,
    status: str,
    tweet_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """Upsert the outcome of a posting attempt for `event_key`.

    `posted_at`/`month` are derived from `now` (default: current UTC time),
    always in UTC. A later call for the same `event_key` overwrites the
    earlier row (e.g. a retry that ultimately succeeds replaces an earlier
    `failed` row) — callers (bot.py, T5) own retry sequencing; this function
    just records whatever outcome it's told.
    """
    utc_now = _to_utc(now)
    conn = _connect(path)
    try:
        conn.execute(
            """
            INSERT INTO x_posts (event_key, tweet_id, posted_at, month, status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                tweet_id = excluded.tweet_id,
                posted_at = excluded.posted_at,
                month = excluded.month,
                status = excluded.status
            """,
            (event_key, tweet_id, utc_now.isoformat(), utc_now.strftime("%Y-%m"), status),
        )
        conn.commit()
    finally:
        conn.close()


def month_count(path: str, now: datetime | None = None) -> int:
    """Count of `status == 'posted'` rows for `now`'s UTC month."""
    month = _to_utc(now).strftime("%Y-%m")
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM x_posts WHERE status = 'posted' AND month = ?", (month,)
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def posting_paused(path: str) -> bool:
    """Runtime kill switch, defaults False (never paused) when unset."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_POSTING_PAUSED_KEY,)
        ).fetchone()
        return row is not None and row[0] == "1"
    finally:
        conn.close()


def set_posting_paused(path: str, paused: bool) -> None:
    """Set the runtime kill switch (PR-2 service endpoint calls this)."""
    conn = _connect(path)
    try:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_POSTING_PAUSED_KEY, "1" if paused else "0"),
        )
        conn.commit()
    finally:
        conn.close()
