import logging
import os
import sqlite3
import sys

# Deliberately NOT lfg_core.config: this standalone initializer must run with
# only DB_PATH / XRPL_NETWORK set, without the bot's runtime secrets. db_path is
# dependency-free, so that stays true. Bootstrap the repo root onto sys.path so
# `python scripts/init_db.py` resolves lfg_core (matches every other scripts/ tool).
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core.db_path import app_db_path  # noqa: E402

logging.basicConfig(level=logging.INFO)


def init_db():
    try:
        # Connect to SQLite database (creates it if it doesn't exist)
        conn = sqlite3.connect(app_db_path())
        cursor = conn.cursor()

        # Create the LFG table with capitalized column names
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS LFG (
            nft_number INTEGER PRIMARY KEY,
            metadata_url TEXT,
            image_url TEXT,
            Background TEXT,
            Body TEXT,
            Clothing TEXT,
            Eyes TEXT,
            Eyebrows TEXT,
            Mouth TEXT,
            Hat TEXT,
            Accessory TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Get the current max NFT number (if any)
        cursor.execute("SELECT MAX(nft_number) FROM LFG")
        max_nft = cursor.fetchone()[0]

        if not max_nft:
            # Add placeholder rows for NFTs 1-3535
            logging.info("Adding placeholder rows for NFTs 1-3535...")

            placeholder_data = []
            for i in range(1, 3536):
                placeholder_data.append(
                    (
                        i,  # nft_number
                        "placeholder_metadata",  # metadata_url
                        "placeholder_image",  # image_url
                        "placeholder",  # background
                        "placeholder",  # body
                        "placeholder",  # clothing
                        "placeholder",  # eyes
                        "placeholder",  # eyebrows
                        "placeholder",  # mouth
                        "placeholder",  # hat
                        "placeholder",  # accessory
                    )
                )

            cursor.executemany(
                """
            INSERT INTO LFG (
                nft_number, metadata_url, image_url,
                background, body, clothing, eyes, eyebrows,
                mouth, hat, accessory
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                placeholder_data,
            )

            logging.info("Placeholder rows added successfully")

        # Create the burned_nfts table. Surrogate key: the same nft_number can
        # be burned more than once over time (burn → remint → burn), and each
        # burn must be kept as its own audit row.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS burned_nfts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nft_number INTEGER,
            nft_id TEXT,
            discord_id TEXT,
            burned_by TEXT,
            reason TEXT,
            burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            original_mint_time TIMESTAMP
        )
        """)

        # Commit the changes and close the connection
        conn.commit()
        conn.close()
        logging.info("Database initialized successfully")

    except Exception as e:
        logging.error(f"Error initializing database: {e}")
        raise


if __name__ == "__main__":
    init_db()
