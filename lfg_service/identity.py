# lfg_service/identity.py
# Generalized identity: maps (platform, platform_user_id) -> XRPL wallet.
# The wallet is the canonical account; account_id is a reserved hook for
# future linked multi-surface profiles (nullable, unused now).

import logging
import sqlite3

from user_db import DATABASE  # single source of truth for the db path


def ensure_identities_table() -> None:
    conn = sqlite3.connect(DATABASE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS identities (
                platform          TEXT NOT NULL,
                platform_user_id  TEXT NOT NULL,
                platform_username TEXT,
                wallet            TEXT NOT NULL,
                account_id        INTEGER,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (platform, platform_user_id)
            )
            """
        )
        # Self-migrating, forward-only: add the #90 columns if an older table
        # shape is on disk, then backfill display_handle from the value we
        # already captured (platform_username). SQLite ADD COLUMN is non-
        # destructive; safe to run on every boot (mirrors migrate_users_*).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(identities)")}
        if "display_handle" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN display_handle TEXT")
            conn.execute(
                "UPDATE identities SET display_handle = platform_username "
                "WHERE display_handle IS NULL"
            )
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE identities ADD COLUMN updated_at TIMESTAMP")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_identities_wallet ON identities(wallet)")
        conn.commit()
    finally:
        conn.close()


def link(
    platform: str,
    platform_user_id: str,
    platform_username: str,
    wallet: str,
    *,
    display_handle: str | None = None,
) -> bool:
    # display_handle defaults to platform_username when not supplied, so legacy
    # positional callers (register / signin) keep their existing behaviour while
    # the column is always populated and updated_at is stamped on every upsert.
    handle = display_handle if display_handle is not None else platform_username
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            INSERT INTO identities
                (platform, platform_user_id, platform_username, display_handle, wallet, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                platform_username = excluded.platform_username,
                display_handle = excluded.display_handle,
                wallet = excluded.wallet,
                updated_at = CURRENT_TIMESTAMP
            """,
            (platform, platform_user_id, platform_username, handle, wallet),
        )
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"identity.link failed: {e}")
        return False
    finally:
        conn.close()


def touch_handle(platform: str, platform_user_id: str, handle: str) -> None:
    """Best-effort refresh of a known identity's display_handle. No-op if the
    row doesn't exist or the handle is unchanged; never raises (caller treats
    this as a fire-and-forget side effect on authenticated touches)."""
    if not handle:
        return
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            "UPDATE identities SET display_handle = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE platform = ? AND platform_user_id = ? "
            "AND (display_handle IS NULL OR display_handle != ?)",
            (handle, platform, platform_user_id, handle),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"identity.touch_handle failed: {e}")
    finally:
        if conn is not None:
            conn.close()


def resolve(platform: str, platform_user_id: str) -> str | None:
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "SELECT wallet FROM identities WHERE platform = ? AND platform_user_id = ?",
            (platform, platform_user_id),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logging.error(f"identity.resolve failed: {e}")
        return None
    finally:
        conn.close()


def identities_for_wallet(wallet: str) -> list[dict[str, object]]:
    """All surface identities linked to a wallet-account, ordered by created_at.

    Returns [] when none. The wallet is matched verbatim — XRPL classic
    addresses are case-sensitive (the base58check checksum makes a case-folded
    address invalid), so callers must NEVER lower-case the wallet.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cur = conn.execute(
            "SELECT platform, platform_user_id, display_handle, platform_username, "
            "created_at, updated_at FROM identities WHERE wallet = ? ORDER BY created_at",
            (wallet,),
        )
        return [
            {
                "platform": r[0],
                "platform_user_id": r[1],
                "display_handle": r[2],
                "platform_username": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in cur.fetchall()
        ]
    except Exception as e:
        logging.error(f"identity.identities_for_wallet failed: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()


def migrate_users_to_identities() -> int:
    """Copy legacy Users rows into identities as platform='discord'. Idempotent."""
    conn = sqlite3.connect(DATABASE)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        if "Users" not in names:
            return 0
        rows = conn.execute("SELECT discord_id, discord_name, wallet FROM Users").fetchall()
        migrated = 0
        for discord_id, discord_name, wallet in rows:
            exists = conn.execute(
                "SELECT 1 FROM identities WHERE platform='discord' AND platform_user_id=?",
                (discord_id,),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO identities (platform, platform_user_id, platform_username, wallet) "
                "VALUES ('discord', ?, ?, ?)",
                (discord_id, discord_name, wallet),
            )
            migrated += 1
        conn.commit()
        return migrated
    finally:
        conn.close()
