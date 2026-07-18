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

# The two most likely future analytics read patterns: by edition ("which NFTs
# get the most shares?") and by sharer ("which wallets drive clicks?"). Declared
# up front so those queries never full-scan once the log grows.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS sc_nft ON share_clicks(nft_number)",
    "CREATE INDEX IF NOT EXISTS sc_ref ON share_clicks(ref_wallet)",
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    for idx in _INDEXES:
        conn.execute(idx)


def init_db(db_file: str) -> None:
    conn = sqlite3.connect(db_file)
    try:
        _ensure_schema(conn)
        conn.commit()
    finally:
        conn.close()


def record_click(
    db_file: str, nft_number: int, ref_wallet: str | None, is_bot: bool, user_agent: str
) -> bool:
    # One connection does both the (idempotent) schema ensure and the INSERT —
    # halves the per-click open cost and closes the window between two opens.
    try:
        conn = sqlite3.connect(db_file)
        try:
            _ensure_schema(conn)
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
