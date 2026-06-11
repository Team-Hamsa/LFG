import sqlite3
import logging
from typing import Dict, Optional
import traceback

def get_next_nft_number() -> int:
    """Get the next available NFT number"""
    try:
        conn = sqlite3.connect('lfg_nfts.db')
        cursor = conn.cursor()
        
        logging.info("=== Getting next NFT number ===")
        logging.info("Connected to database: lfg_nfts.db")
        
        # First, check if table exists
        cursor.execute('''
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='LFG'
        ''')
        table_exists = cursor.fetchone()
        logging.info(f"LFG table exists: {bool(table_exists)}")
        
        # Create table if it doesn't exist
        if not table_exists:
            logging.info("Creating LFG table...")
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS LFG (
                nft_number INTEGER PRIMARY KEY,
                nft_id TEXT,
                discord_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            conn.commit()
            logging.info("Initialized LFG table")
        
        # Get the highest NFT number
        cursor.execute('SELECT MAX(nft_number) FROM LFG')
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
        if 'conn' in locals():
            conn.close()

def record_nft_mint(nft_number: int, nft_id: str, discord_id: str, owner_address: str, metadata_url: str, image_url: str, traits: dict) -> bool:
    """Record a new NFT mint in the database"""
    try:
        conn = sqlite3.connect('lfg_nfts.db')
        cursor = conn.cursor()
        
        # Check existing columns
        cursor.execute("PRAGMA table_info(LFG)")
        existing_columns = {col[1] for col in cursor.fetchall()}
        
        # Add any missing columns
        new_columns = {
            'nft_id': 'TEXT',
            'discord_id': 'TEXT',
            'owner_address': 'TEXT',
            'metadata_url': 'TEXT',
            'image_url': 'TEXT',
            'Background': 'TEXT',
            'Back': 'TEXT',
            'Body': 'TEXT',
            'Clothing': 'TEXT',
            'Eyes': 'TEXT',
            'Eyebrows': 'TEXT',
            'Mouth': 'TEXT',
            'Hat': 'TEXT',
            'Accessory': 'TEXT',
            'created_at': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
        }
        
        for col_name, col_type in new_columns.items():
            if col_name not in existing_columns:
                try:
                    cursor.execute(f'ALTER TABLE LFG ADD COLUMN {col_name} {col_type}')
                    logging.info(f"Added column {col_name} to LFG table")
                except Exception as e:
                    logging.error(f"Error adding column {col_name}: {e}")
        
        # Insert the mint record with all data
        cursor.execute('''
        INSERT INTO LFG (
            nft_number, nft_id, discord_id, owner_address,
            metadata_url, image_url,
            Background, Back, Body, Clothing, Eyes, Eyebrows,
            Mouth, Hat, Accessory
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            nft_number,
            nft_id,
            discord_id,
            owner_address,
            metadata_url,
            image_url,
            traits.get('Background', ''),
            traits.get('Back', ''),
            traits.get('Body', ''),
            traits.get('Clothing', ''),
            traits.get('Eyes', ''),
            traits.get('Eyebrows', ''),
            traits.get('Mouth', ''),
            traits.get('Hat', ''),
            traits.get('Accessory', '')
        ))
        
        conn.commit()
        logging.info(f"Recorded NFT mint: #{nft_number} to owner {owner_address}")
        return True
        
    except Exception as e:
        logging.error(f"Error recording NFT mint: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def get_nft_data(nft_number: int) -> Optional[Dict]:
    """Get data for a specific NFT number"""
    try:
        conn = sqlite3.connect('lfg_nfts.db')
        cursor = conn.cursor()
        
        cursor.execute('''
        SELECT * FROM LFG WHERE nft_number = ?
        ''', (nft_number,))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'nft_number': row[0],
                'metadata_url': row[1],
                'image_url': row[2],
                'traits': {
                    'background': row[3],
                    'body': row[4],
                    'clothing': row[5],
                    'eyes': row[6],
                    'eyebrows': row[7],
                    'mouth': row[8],
                    'hat': row[9],
                    'accessory': row[10]
                },
                'created_at': row[11]
            }
        return None
        
    except Exception as e:
        logging.error(f"Error getting NFT data: {e}")
        return None 