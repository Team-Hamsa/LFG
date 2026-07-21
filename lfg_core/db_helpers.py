import logging
import sqlite3
import traceback
from typing import Any

from lfg_core import config


def get_next_nft_number() -> int:
    """Get the next available NFT number"""
    try:
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()

        logging.info("=== Getting next NFT number ===")
        logging.info(f"Connected to database: {config.DB_PATH}")

        # First, check if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='LFG'
        """)
        table_exists = cursor.fetchone()
        logging.info(f"LFG table exists: {bool(table_exists)}")

        # Create table if it doesn't exist
        if not table_exists:
            logging.info("Creating LFG table...")
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS LFG (
                nft_number INTEGER PRIMARY KEY,
                nft_id TEXT,
                discord_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            conn.commit()
            logging.info("Initialized LFG table")

        # Get the highest NFT number
        cursor.execute("SELECT MAX(nft_number) FROM LFG")
        result = cursor.fetchone()
        current_max = result[0] if result[0] is not None else 3535
        logging.info(f"Current highest NFT number: {current_max}")

        next_number = current_max + 1
        logging.info(f"Next NFT number will be: {next_number}")

        # Don't insert a placeholder - just return the next number
        return next_number

    except Exception as e:
        logging.error(f"Database error in get_next_nft_number: {str(e)}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        raise  # Re-raise the exception instead of falling back
    finally:
        if "conn" in locals():
            conn.close()


def record_nft_mint(
    nft_number: int,
    nft_id: str,
    discord_id: str,
    owner_address: str,
    metadata_url: str,
    image_url: str,
    traits: dict[str, Any],
    network: str = "mainnet",
    body_type: str = "*",
    db_path: str | None = None,
) -> bool:
    """Record a new NFT mint in the database"""
    try:
        conn = sqlite3.connect(db_path or config.DB_PATH)
        cursor = conn.cursor()

        # Check existing columns
        cursor.execute("PRAGMA table_info(LFG)")
        existing_columns = {col[1] for col in cursor.fetchall()}

        # Add any missing columns
        new_columns = {
            "nft_id": "TEXT",
            "discord_id": "TEXT",
            "owner_address": "TEXT",
            "metadata_url": "TEXT",
            "image_url": "TEXT",
            "Background": "TEXT",
            "Back": "TEXT",
            "Body": "TEXT",
            "Clothing": "TEXT",
            "Eyes": "TEXT",
            "Eyebrows": "TEXT",
            "Mouth": "TEXT",
            "Hat": "TEXT",
            "Accessory": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "network": "TEXT NOT NULL DEFAULT 'mainnet'",
            "body_type": "TEXT NOT NULL DEFAULT '*'",
        }

        for col_name, col_type in new_columns.items():
            if col_name not in existing_columns:
                try:
                    cursor.execute(f"ALTER TABLE LFG ADD COLUMN {col_name} {col_type}")
                    logging.info(f"Added column {col_name} to LFG table")
                except Exception as e:
                    logging.error(f"Error adding column {col_name}: {e}")

        # Insert the mint record with all data
        cursor.execute(
            """
        INSERT INTO LFG (
            nft_number, nft_id, discord_id, owner_address,
            metadata_url, image_url,
            Background, Back, Body, Clothing, Eyes, Eyebrows,
            Mouth, Hat, Accessory, network, body_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                nft_number,
                nft_id,
                discord_id,
                owner_address,
                metadata_url,
                image_url,
                traits.get("Background", ""),
                traits.get("Back", ""),
                traits.get("Body", ""),
                traits.get("Clothing", ""),
                traits.get("Eyes", ""),
                traits.get("Eyebrows", ""),
                traits.get("Mouth", ""),
                traits.get("Hat", ""),
                traits.get("Accessory", ""),
                network,
                body_type,
            ),
        )

        conn.commit()
        logging.info(f"Recorded NFT mint: #{nft_number} to owner {owner_address}")
        return True

    except Exception as e:
        logging.error(f"Error recording NFT mint: {e}")
        return False
    finally:
        if "conn" in locals():
            conn.close()


def get_nft_data(nft_number: int) -> dict[str, Any] | None:
    """Get data for a specific NFT number"""
    try:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
        SELECT * FROM LFG WHERE nft_number = ?
        """,
            (nft_number,),
        )

        row = cursor.fetchone()
        conn.close()

        if row:
            # Columns are added over time by record_nft_mint's auto-ALTER, so
            # map by name (never by position) and tolerate missing ones.
            cols = set(row.keys())

            def col(name):
                return row[name] if name in cols else None

            return {
                "nft_number": row["nft_number"],
                "nft_id": col("nft_id"),
                "discord_id": col("discord_id"),
                "owner_address": col("owner_address"),
                "metadata_url": col("metadata_url"),
                "image_url": col("image_url"),
                "traits": {
                    "background": col("Background"),
                    "back": col("Back"),
                    "body": col("Body"),
                    "clothing": col("Clothing"),
                    "eyes": col("Eyes"),
                    "eyebrows": col("Eyebrows"),
                    "mouth": col("Mouth"),
                    "hat": col("Hat"),
                    "accessory": col("Accessory"),
                },
                "created_at": col("created_at"),
            }
        return None

    except Exception as e:
        logging.error(f"Error getting NFT data: {e}")
        return None


# ---------------------------------------------------------------------------
# Harvest/assemble rarity bookkeeping (#305)
#
# The rarity live-count (rarity.recalculate_rarity) counts LFG minus
# non-revived burned_nfts rows. Economy harvests burn characters on-ledger,
# so they must land here too or the Trait Shop price never sees them; an
# assemble revives the edition, which stamps the harvest row instead of
# deleting it (burned_nfts stays an append-only audit log).
# ---------------------------------------------------------------------------

HARVEST_BURN_REASON = "harvest"


def ensure_burned_nfts_schema(conn: sqlite3.Connection) -> None:
    """Create burned_nfts if missing (surrogate key: the same nft_number can
    burn more than once over time) and self-migrate the revived_at column."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS burned_nfts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nft_number INTEGER,
            nft_id TEXT,
            discord_id TEXT,
            burned_by TEXT,
            reason TEXT,
            burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            original_mint_time TIMESTAMP
        )"""
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(burned_nfts)")}
    if "revived_at" not in cols:
        conn.execute("ALTER TABLE burned_nfts ADD COLUMN revived_at TIMESTAMP")
    conn.commit()


def record_harvest_burn(
    conn: sqlite3.Connection, nft_number: int, nft_id: str | None, owner: str
) -> None:
    """Audit a harvest burn so the edition leaves the rarity live-count."""
    ensure_burned_nfts_schema(conn)
    row = conn.execute(
        "SELECT discord_id, created_at FROM LFG WHERE nft_number=?", (nft_number,)
    ).fetchone()
    discord_id, minted_at = (row[0], row[1]) if row else (None, None)
    conn.execute(
        """INSERT INTO burned_nfts
           (nft_number, nft_id, discord_id, burned_by, reason, original_mint_time)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (nft_number, nft_id, discord_id, owner, HARVEST_BURN_REASON, minted_at),
    )
    conn.commit()


def revive_harvested_edition(conn: sqlite3.Connection, nft_number: int) -> bool:
    """Stamp the most recent un-revived harvest burn of this edition as
    revived (an assemble reminted it). Returns False when no such row exists
    (e.g. the edition was harvested before #305 started recording burns)."""
    ensure_burned_nfts_schema(conn)
    cur = conn.execute(
        """UPDATE burned_nfts SET revived_at=CURRENT_TIMESTAMP
           WHERE id = (SELECT id FROM burned_nfts
                       WHERE nft_number=? AND reason=? AND revived_at IS NULL
                       ORDER BY id DESC LIMIT 1)""",
        (nft_number, HARVEST_BURN_REASON),
    )
    conn.commit()
    return cur.rowcount > 0
