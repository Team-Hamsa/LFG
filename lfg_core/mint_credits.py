# lfg_core/mint_credits.py
# Last-resort tail for bulk minting (#215): a unit that is permanently
# undeliverable (cap-hit race, exhausted retries) becomes a durable credit
# the user can redeem later with no re-payment.
import sqlite3


def ensure_table(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mint_credits ("
            "discord_id TEXT NOT NULL, network TEXT NOT NULL, "
            "credits INTEGER NOT NULL DEFAULT 0, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (discord_id, network))"
        )
        conn.commit()
    finally:
        conn.close()


def add_credit(db_path: str, discord_id: str, network: str, n: int = 1) -> int:
    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO mint_credits (discord_id, network, credits) VALUES (?,?,?) "
            "ON CONFLICT(discord_id, network) DO UPDATE SET "
            "credits = credits + excluded.credits, updated_at = CURRENT_TIMESTAMP",
            (discord_id, network, n),
        )
        conn.commit()
        row = conn.execute(
            "SELECT credits FROM mint_credits WHERE discord_id=? AND network=?",
            (discord_id, network),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def get_credits(db_path: str, discord_id: str, network: str) -> int:
    ensure_table(db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT credits FROM mint_credits WHERE discord_id=? AND network=?",
            (discord_id, network),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
