# nft_db.py - Database module for NFT ownership tracking
# Uses the same database as LFG MINT BOT

import sqlite3
import logging
import os
from typing import List, Dict, Optional

# Use the same database as LFG MINT BOT
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE = os.path.join(BASE_DIR, "LFG MINT BOT", "lfg_nfts.db")

def create_nft_ownership_table() -> None:
    """
    Create the NFT_Ownership table if it doesn't already exist.
    Stores NFT ownership data to avoid querying XRPL every time.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS NFT_Ownership (
                nftoken_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                issuer TEXT NOT NULL,
                uri_hex TEXT,
                decoded_uri TEXT,
                metadata_url TEXT,
                taxon INTEGER,
                transfer_fee INTEGER,
                flags INTEGER,
                sequence INTEGER,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Create index on owner for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_owner ON NFT_Ownership(owner)
        ''')
        # Create index on issuer for filtering
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_issuer ON NFT_Ownership(issuer)
        ''')
        conn.commit()
        logging.info("NFT_Ownership table ensured in database.")
    except Exception as e:
        logging.error(f"Error creating NFT_Ownership table: {e}")
    finally:
        conn.close()

def upsert_nft(nftoken_id: str, owner: str, issuer: str, uri_hex: str = None, 
                decoded_uri: str = None, metadata_url: str = None, 
                taxon: int = None, transfer_fee: int = None, 
                flags: int = None, sequence: int = None) -> bool:
    """
    Insert or update an NFT ownership record.
    
    Returns:
        bool: True if successful, False on error
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO NFT_Ownership 
            (nftoken_id, owner, issuer, uri_hex, decoded_uri, metadata_url, 
             taxon, transfer_fee, flags, sequence, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (nftoken_id, owner, issuer, uri_hex, decoded_uri, metadata_url,
              taxon, transfer_fee, flags, sequence))
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"Error upserting NFT: {e}")
        return False
    finally:
        conn.close()

def get_user_nfts(owner: str, issuer: str = None) -> List[Dict]:
    """
    Get all NFTs owned by a specific address.
    
    Args:
        owner: XRP wallet address
        issuer: Optional issuer filter (e.g., "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ")
    
    Returns:
        List of dicts with NFT data
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        if issuer:
            cursor.execute('''
                SELECT nftoken_id, uri_hex, decoded_uri, metadata_url
                FROM NFT_Ownership
                WHERE owner = ? AND issuer = ?
                ORDER BY nftoken_id
            ''', (owner, issuer))
        else:
            cursor.execute('''
                SELECT nftoken_id, uri_hex, decoded_uri, metadata_url
                FROM NFT_Ownership
                WHERE owner = ?
                ORDER BY nftoken_id
            ''', (owner,))
        
        rows = cursor.fetchall()
        nfts = []
        for row in rows:
            nfts.append({
                "NFTokenID": row[0],
                "URI": row[1] if row[1] else "",
                "decoded_uri": row[2],
                "metadata_url": row[3]
            })
        return nfts
    except Exception as e:
        logging.error(f"Error getting user NFTs: {e}")
        return []
    finally:
        conn.close()

def decode_uri(uri_hex: str) -> Optional[str]:
    """
    Decode a hex-encoded URI to ASCII and convert IPFS to HTTP URL.
    Returns the decoded metadata URL or None on error.
    """
    try:
        if not uri_hex:
            return None
            
        ascii_uri = bytes.fromhex(uri_hex).decode("ascii")
        
        if ascii_uri.startswith("ipfs://"):
            ascii_uri = ascii_uri.replace("ipfs://", "")
            parts = ascii_uri.split("/")
            
            if len(parts) == 2:
                # e.g., ipfs://hash/filename.json -> https://hash.ipfs.dweb.link/filename.json
                return f"https://{parts[0]}.ipfs.dweb.link/{parts[1]}"
            else:
                # Just the hash
                return f"https://{parts[0]}.ipfs.dweb.link/"
        
        # Already HTTP/HTTPS or BunnyCDN
        elif ascii_uri.startswith("https://") or ascii_uri.startswith("http://"):
            return ascii_uri
        
        return None
    except Exception as e:
        logging.error(f"Error decoding URI: {e}")
        return None

def update_nft_metadata_url(nftoken_id: str, metadata_url: str) -> bool:
    """
    Update the metadata_url for an NFT after decoding.
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE NFT_Ownership
            SET metadata_url = ?, last_updated = CURRENT_TIMESTAMP
            WHERE nftoken_id = ?
        ''', (metadata_url, nftoken_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Error updating metadata URL: {e}")
        return False
    finally:
        conn.close()

def get_nft_attributes(nftoken_id: str) -> Optional[Dict]:
    """
    Get all attributes for a specific NFT from the nft_attributes table.
    
    Args:
        nftoken_id: The NFTokenID
    
    Returns:
        Dict with trait types as keys and values, or None if not found
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT Background, Back, Body, Clothing, Mouth, 
                   Eyebrows, Eyes, Head, Accessory
            FROM nft_attributes
            WHERE NFTokenID = ?
        ''', (nftoken_id,))
        
        row = cursor.fetchone()
        if row:
            return {
                "Background": row[0],
                "Back": row[1],
                "Body": row[2],
                "Clothing": row[3],
                "Mouth": row[4],
                "Eyebrows": row[5],
                "Eyes": row[6],
                "Head": row[7],
                "Accessory": row[8]
            }
        return None
    except Exception as e:
        logging.error(f"Error getting NFT attributes: {e}")
        return None
    finally:
        conn.close()

def get_nft_attributes_batch(nftoken_ids: List[str]) -> Dict[str, Dict]:
    """
    Get attributes for multiple NFTs at once.
    
    Args:
        nftoken_ids: List of NFTokenIDs
    
    Returns:
        Dict mapping NFTokenID to attributes dict
    """
    if not nftoken_ids:
        return {}
    
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Create placeholders for IN clause
        placeholders = ','.join('?' * len(nftoken_ids))
        cursor.execute(f'''
            SELECT NFTokenID, Background, Back, Body, Clothing, Mouth, 
                   Eyebrows, Eyes, Head, Accessory
            FROM nft_attributes
            WHERE NFTokenID IN ({placeholders})
        ''', nftoken_ids)
        
        results = {}
        for row in cursor.fetchall():
            results[row[0]] = {
                "Background": row[1],
                "Back": row[2],
                "Body": row[3],
                "Clothing": row[4],
                "Mouth": row[5],
                "Eyebrows": row[6],
                "Eyes": row[7],
                "Head": row[8],
                "Accessory": row[9]
            }
        return results
    except Exception as e:
        logging.error(f"Error getting NFT attributes batch: {e}")
        return {}
    finally:
        conn.close()

def ensure_mutable_column() -> None:
    """
    Ensure the mutable column exists in the LFG table.
    """
    conn = None
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Check if column exists
        cursor.execute("PRAGMA table_info(LFG)")
        existing_columns = {col[1] for col in cursor.fetchall()}
        
        if 'mutable' not in existing_columns:
            cursor.execute('ALTER TABLE LFG ADD COLUMN mutable INTEGER DEFAULT 0')
            conn.commit()
            logging.info("Added mutable column to LFG table")
            
            # Initialize mutable values: NFTs 3535 and below are not mutable (0), above are mutable (1)
            cursor.execute('UPDATE LFG SET mutable = 1 WHERE nft_number > 3535')
            conn.commit()
            logging.info("Initialized mutable values in LFG table")
    except Exception as e:
        logging.error(f"Error ensuring mutable column: {e}")
    finally:
        if conn is not None:
            conn.close()

def is_nft_mutable(nft_number: int) -> bool:
    """
    Check if an NFT is mutable based on its number.
    NFTs 3535 and below are not mutable (False), above are mutable (True).
    
    Args:
        nft_number: The NFT number extracted from the name
    
    Returns:
        bool: True if mutable, False otherwise
    """
    # NFTs 3535 and below do not have the mutable flag set
    return nft_number > 3535

def get_nft_mutable_from_db(nft_number: int) -> Optional[bool]:
    """
    Get the mutable status from the database for a specific NFT number.
    
    Args:
        nft_number: The NFT number
    
    Returns:
        bool if found, None if not found
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('SELECT mutable FROM LFG WHERE nft_number = ?', (nft_number,))
        row = cursor.fetchone()
        if row is not None:
            return bool(row[0])
        return None
    except Exception as e:
        logging.error(f"Error getting mutable status from DB: {e}")
        return None
    finally:
        conn.close()

def update_nft_mutable_in_db(nft_number: int, mutable: bool) -> bool:
    """
    Update the mutable status in the database for a specific NFT number.
    
    Args:
        nft_number: The NFT number
        mutable: The mutable status (True/False)
    
    Returns:
        bool: True if successful, False on error
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        cursor.execute('UPDATE LFG SET mutable = ? WHERE nft_number = ?', (1 if mutable else 0, nft_number))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Error updating mutable status in DB: {e}")
        return False
    finally:
        conn.close()

def update_nft_after_swap(nft_number: int, nft_id: str, owner_address: str,
                          metadata_url: str, image_url: str, traits: Dict[str, str], burn_count: int = 0) -> bool:
    """
    Update LFG table after a swap operation (for both modified and reminted NFTs).
    
    Args:
        nft_number: The NFT number (doesn't change during swap)
        nft_id: The new or existing NFT ID
        owner_address: The owner's address
        metadata_url: The metadata URL
        image_url: The image URL
        traits: Dictionary of trait_type -> trait_value
    
    Returns:
        bool: True if successful, False on error
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Check existing columns
        cursor.execute("PRAGMA table_info(LFG)")
        existing_columns = {col[1] for col in cursor.fetchall()}

        # Build update fields
        update_fields = ["nft_id = ?", "owner_address = ?", "metadata_url = ?", "image_url = ?", "burnCount = ?"]
        update_values = [nft_id, owner_address, metadata_url, image_url, burn_count]
        
        # Map trait names: "Head" in metadata becomes "Hat" in LFG
        trait_mapping = {
            'Background': 'Background',
            'Back': 'Back',
            'Body': 'Body',
            'Clothing': 'Clothing',
            'Mouth': 'Mouth',
            'Eyebrows': 'Eyebrows',
            'Eyes': 'Eyes',
            'Head': 'Hat',  # Map Head to Hat for LFG table
            'Accessory': 'Accessory'
        }
        
        # Add trait updates
        for trait_type, lfg_column in trait_mapping.items():
            if lfg_column in existing_columns:
                trait_value = traits.get(trait_type, '')
                update_fields.append(f'"{lfg_column}" = ?')
                update_values.append(trait_value if trait_value and trait_value.lower() != 'none' else '')
        
        # Add nft_number for WHERE clause
        update_values.append(nft_number)
        
        sql = f'''
        UPDATE LFG
        SET {', '.join(update_fields)}
        WHERE nft_number = ?
        '''
        
        cursor.execute(sql, update_values)
        conn.commit()
        logging.info(f"Updated NFT #{nft_number} in LFG table after swap")
        return cursor.rowcount > 0
        
    except Exception as e:
        logging.error(f"Error updating NFT #{nft_number} in LFG after swap: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

def record_swap_in_history(nft_number: int, nft_id: str, owner_address: str,
                           metadata_url: str, image_url: str, name: str,
                           traits: Dict[str, str], burn_count: int) -> bool:
    """
    Record a swap operation in the history table.
    
    Args:
        nft_number: The NFT number
        nft_id: The NFT ID (new if reminted, same if modified)
        owner_address: The owner's address
        metadata_url: The metadata URL
        image_url: The image URL
        name: The NFT name
        traits: Dictionary of trait_type -> trait_value
        burn_count: The burn count after the swap
    
    Returns:
        bool: True if successful, False on error
    """
    try:
        conn = sqlite3.connect(DATABASE)
        cursor = conn.cursor()
        
        # Ensure history table exists
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            nft_id TEXT PRIMARY KEY,
            nft_number INTEGER,
            nft_serial INTEGER,
            burned INTEGER DEFAULT 0,
            burnCount INTEGER DEFAULT 0,
            owner TEXT,
            name TEXT,
            image_url TEXT,
            Background TEXT,
            Back TEXT,
            Body TEXT,
            Clothing TEXT,
            Mouth TEXT,
            Eyebrows TEXT,
            Eyes TEXT,
            Head TEXT,
            Accessory TEXT,
            metadata_url TEXT,
            ledger_index INTEGER,
            flags INTEGER,
            transfer_fee INTEGER,
            edition INTEGER,
            uri_hex TEXT,
            uri_decoded TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # Build data dictionary
        data = {
            'nft_id': nft_id,
            'nft_number': nft_number,
            'burned': 0,
            'burnCount': burn_count,
            'owner': owner_address,
            'name': name,
            'image_url': image_url,
            'metadata_url': metadata_url
        }
        
        # Add trait values (history table uses "Head" not "Hat")
        trait_columns = ['Background', 'Back', 'Body', 'Clothing', 'Mouth', 
                        'Eyebrows', 'Eyes', 'Head', 'Accessory']
        for trait_type in trait_columns:
            trait_value = traits.get(trait_type, '')
            data[trait_type] = trait_value if trait_value and trait_value.lower() != 'none' else None
        
        # Build INSERT OR REPLACE query
        columns = list(data.keys())
        placeholders = ['?' for _ in columns]
        values = [data[col] for col in columns]
        
        sql = f'''
        INSERT OR REPLACE INTO history ({', '.join(columns)}, last_updated)
        VALUES ({', '.join(placeholders)}, CURRENT_TIMESTAMP)
        '''
        
        cursor.execute(sql, values)
        conn.commit()
        logging.info(f"Recorded swap for NFT #{nft_number} ({nft_id}) in history table")
        return True
        
    except Exception as e:
        logging.error(f"Error recording swap in history table: {e}")
        return False
    finally:
        if 'conn' in locals():
            conn.close()

