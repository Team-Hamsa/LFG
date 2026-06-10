import sqlite3
import logging

logging.basicConfig(level=logging.INFO)

def init_db():
    try:
        # Connect to SQLite database (creates it if it doesn't exist)
        conn = sqlite3.connect('lfg_nfts.db')
        cursor = conn.cursor()

        # Create the LFG table with capitalized column names
        cursor.execute('''
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
        ''')

        # Get the current max NFT number (if any)
        cursor.execute('SELECT MAX(nft_number) FROM LFG')
        max_nft = cursor.fetchone()[0]

        if not max_nft:
            # Add placeholder rows for NFTs 1-3535
            logging.info("Adding placeholder rows for NFTs 1-3535...")
            
            placeholder_data = []
            for i in range(1, 3536):
                placeholder_data.append((
                    i,                          # nft_number
                    'placeholder_metadata',     # metadata_url
                    'placeholder_image',        # image_url
                    'placeholder',              # background
                    'placeholder',              # body
                    'placeholder',              # clothing
                    'placeholder',              # eyes
                    'placeholder',              # eyebrows
                    'placeholder',              # mouth
                    'placeholder',              # hat
                    'placeholder'               # accessory
                ))

            cursor.executemany('''
            INSERT INTO LFG (
                nft_number, metadata_url, image_url, 
                background, body, clothing, eyes, eyebrows, 
                mouth, hat, accessory
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', placeholder_data)

            logging.info("Placeholder rows added successfully")

        # Create the burned_nfts table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS burned_nfts (
            nft_number INTEGER PRIMARY KEY,
            nft_id TEXT,
            discord_id TEXT,
            burned_by TEXT,
            reason TEXT,
            burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            original_mint_time TIMESTAMP
        )
        ''')

        # Commit the changes and close the connection
        conn.commit()
        conn.close()
        logging.info("Database initialized successfully")

    except Exception as e:
        logging.error(f"Error initializing database: {e}")
        raise

if __name__ == "__main__":
    init_db() 