# user_db.py

import logging
import sqlite3
from typing import Any

from lfg_core import config

# Patchable seam (tests override this); network-aware default from config.
DATABASE = config.DB_PATH


def create_users_table() -> None:
    """
    Create the Users table if it doesn't already exist.
    The table will have columns for an auto-incremented ID, Discord ID, Discord name, and wallet.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL UNIQUE,
                discord_name TEXT NOT NULL,
                wallet TEXT NOT NULL
            )
        """)
        conn.commit()
        logging.info("Users table ensured in database.")
    except Exception as e:
        logging.error(f"Error creating users table: {e}")
    finally:
        if conn is not None:
            conn.close()


def register_user(discord_id: str, discord_name: str, wallet: str) -> bool:
    """
    Register a user in the Users table, or update their wallet/name if the
    discord_id is already registered (so "change wallet" actually changes it).

    Args:
        discord_id (str): The Discord user's ID.
        discord_name (str): The Discord user's name.
        wallet (str): The user's wallet address.

    Returns:
        bool: True if the row was inserted or updated; False on error.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO Users (discord_id, discord_name, wallet)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                discord_name = excluded.discord_name,
                wallet = excluded.wallet
        """,
            (discord_id, discord_name, wallet),
        )
        conn.commit()
        logging.info(f"Registered user: {discord_name} ({discord_id}) -> {wallet}")
        return True
    except Exception as e:
        logging.error(f"Error registering user: {e}")
        return False
    finally:
        if conn is not None:
            conn.close()


def get_user(discord_id: str) -> dict[str, Any] | None:
    """
    Retrieve a user from the Users table by Discord ID.

    Args:
        discord_id (str): The Discord user's ID.

    Returns:
        Dict: A dictionary with keys 'id', 'address' (wallet), and 'name' (discord_name), or None if not found.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT discord_id, discord_name, wallet FROM Users WHERE discord_id = ?", (discord_id,)
        )
        row = cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "address": row[2],  # wallet
                "name": row[1],  # discord_name
            }
        return None
    except Exception as e:
        logging.error(f"Error retrieving user: {e}")
        return None
    finally:
        if conn is not None:
            conn.close()


def get_all_registered_users() -> list[dict[str, Any]]:
    """
    Retrieve all registered users from the Users table.

    Returns:
        List[Dict]: A list where each item is a dictionary with keys: 'discord_id', 'discord_name', and 'wallet'.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute("SELECT discord_id, discord_name, wallet FROM Users")
        rows = cursor.fetchall()
        users = [{"discord_id": row[0], "discord_name": row[1], "wallet": row[2]} for row in rows]
        return users
    except Exception as e:
        logging.error(f"Error retrieving registered users: {e}")
        return []
    finally:
        if conn is not None:
            conn.close()
