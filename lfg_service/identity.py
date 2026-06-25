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
        conn.commit()
    finally:
        conn.close()


def link(platform: str, platform_user_id: str, platform_username: str, wallet: str) -> bool:
    try:
        conn = sqlite3.connect(DATABASE)
        conn.execute(
            """
            INSERT INTO identities (platform, platform_user_id, platform_username, wallet)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                platform_username = excluded.platform_username,
                wallet = excluded.wallet
            """,
            (platform, platform_user_id, platform_username, wallet),
        )
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"identity.link failed: {e}")
        return False
    finally:
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
