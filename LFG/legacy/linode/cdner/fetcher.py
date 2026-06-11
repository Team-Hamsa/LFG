import asyncio
import websockets
import json
import os
import aiohttp
import re
import traceback

import sqlite3
import binascii
import time
import requests
import logging
from xrpl.utils import hex_to_str
from BunnyCDN.Storage import Storage
from BunnyCDN.CDN import CDN

key = os.getenv("BUNNYCDN_ACCESS_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database setup (SQLite in this example)
conn = sqlite3.connect('nftdata.db')
c = conn.cursor()

# Create the NFTs table if it doesn't exist
# c.execute('''
#     CREATE TABLE IF NOT EXISTS nfts (
#         NFTokenID TEXT PRIMARY KEY,
#         owner_address TEXT,
#         name TEXT,
#         image TEXT,
#         trait1 TEXT,
#         trait2 TEXT,
#         trait3 TEXT,
#         trait4 TEXT,
#         trait5 TEXT,
#         trait6 TEXT
#     )
# ''')
c.execute('''
    CREATE TABLE IF NOT EXISTS nfts (
    NFTokenID TEXT PRIMARY KEY,
    owner_address TEXT,
    name TEXT,
    image TEXT
    );

CREATE TABLE IF NOT EXISTS nft_attributes (
    NFTokenID TEXT,
    trait_type TEXT,
    value TEXT,
    FOREIGN KEY (NFTokenID) REFERENCES nfts(NFTokenID)
    );
''')
conn.commit()



'''
# List of WebSocket server URIs to rotate through
ws_uris = [
    "wss://s1.ripple.com/",
    "wss://xrplcluster.com/",
    "wss://s2.ripple.com/"
]
'''

# API URL template
api_url_template = "https://api.xrpldata.com/api/v1/xls20-nfts/issuer/{issuer}/taxon/{taxon}"


# Replace with your issuer's address and desired taxon
issuer_address = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
taxon = 1760

'''
async def fetch_nfts_by_issuer_and_taxon(issuer, taxon, ws_uris, marker=None):
    for ws_uri in ws_uris:
        try:
            logger.info(f"Attempting to connect to WebSocket server: {ws_uri}")
            async with websockets.connect(ws_uri) as websocket:
                request = {
                    "command": "nfts_by_issuer",
                    "issuer": issuer,
                    "nft_taxon": taxon,
                    "limit": 100,
                    "ledger_index": "validated"
                }
                if marker:
                    request["marker"] = marker

                await websocket.send(json.dumps(request))

                response = await websocket.recv()
                nft_data = json.loads(response)

                # Log the entire response for debugging
                logger.debug(f"Response: {json.dumps(nft_data, indent=2)}")

                if 'result' in nft_data:
                    nfts = nft_data['result'].get('nfts', [])
                    next_marker = nft_data['result'].get('marker', None)
                    logger.info(f"Fetched {len(nfts)} NFTs from {ws_uri}")
                    return nfts, next_marker
                else:
                    logger.error(f"Error in response from {ws_uri}: {nft_data}")
                    continue
        except Exception as e:
            logger.warning(f"Error connecting to {ws_uri}: {e}")

    logger.error("Failed to fetch NFTs from all available WebSocket servers.")
    return [], None
'''
    
def fetch_nfts_by_issuer_and_taxon(issuer, taxon):
    url = api_url_template.format(issuer=issuer, taxon=taxon)
    try:
        logger.info(f"Fetching NFTs from API: {url}")
        response = requests.get(url)
        if response.status_code == 200:
            nft_data = response.json()
            nfts = nft_data['data'].get('nfts', [])
            logger.info(f"Fetched {len(nfts)} NFTs")
            return nfts
        else:
            logger.error(f"Error fetching NFTs from API: {response.status_code}")
            return []
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")
        return []

# HEX TO STR OLD
'''
def hex_to_ascii(hex_string):
    try:
        ascii_string = binascii.unhexlify(hex_string).decode('utf-8')
    except Exception as e:
        logger.error(f"Error decoding hex: {e}")
        ascii_string = ""
    return ascii_string
'''

# IPFS Gateways OLD
'''
ipfs_gateways = [
    "https://ipfs.io/ipfs/",
    "https://gateway.pinata.cloud/ipfs/"
]
'''

# FETCH IPFS METADATA OLD
'''
def fetch_ipfs_metadata(uri, max_retries=50):
    if uri.startswith("ipfs://"):
        ipfs_hash = uri.split("ipfs://")[1]

        for gateway in ipfs_gateways:
            retries = 0
            while retries < max_retries:
                try:
                    ipfs_url = gateway + ipfs_hash
                    response = requests.get(ipfs_url)
                    if response.status_code == 200:
                        logger.info(f"Successfully fetched data from {gateway}")
                        return response.json()
                    else:
                        logger.warning(f"Error fetching IPFS data from {gateway}: {response.status_code}")
                except requests.exceptions.RequestException as e:
                    logger.warning(f"Request failed from {gateway}: {e}")

                retries += 1
                wait_time = 2 ** retries  # Exponential backoff: 2, 4, 8, 16, etc., seconds
                logger.info(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)

            logger.info(f"Switching to next gateway after {max_retries} retries with {gateway}")

    logger.error("All gateways failed")
    return {}
'''


# FETCH IPFS METADATA NEW
def fetch_ipfs_metadata(uri, max_retries=50):
    if uri.startswith("ipfs://"):
        # Split the IPFS URI into its hash and path components
        parts = uri.replace("ipfs://", "").split("/", 1)
        ipfs_hash = parts[0]
        ipfs_path = parts[1] if len(parts) > 1 else ""

        # Construct the dweb.link URL
        ascii_uri = f"https://{ipfs_hash}.ipfs.dweb.link/{ipfs_path}"

        retries = 0
        while retries < max_retries:
            try:
                logger.info(f"Fetching IPFS data from {ascii_uri}")
                response = requests.get(ascii_uri)
                if response.status_code == 200:
                    logger.info(f"Successfully fetched data from {ascii_uri}")
                    return response.json()
                else:
                    logger.warning(f"Error fetching IPFS data: {response.status_code}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request failed: {e}")

            retries += 1
            wait_time = 2 ** retries  # Exponential backoff: 2, 4, 8, 16, etc., seconds
            logger.info(f"Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

        logger.info(f"Failed to fetch data after {max_retries} retries.")
    
    logger.error("Invalid URI format or all gateways failed.")
    return {}


# STORE NFT IN DATABASE OLD
'''
def store_nft_in_database(nftoken_id, owner_address, metadata):
    name = metadata.get("name", "")
    image = metadata.get("image", "")
    
    attributes = {attr["trait_type"]: attr["value"] for attr in metadata.get("attributes", [])}

    c.execute('''
#        INSERT OR REPLACE INTO nfts (
#            NFTokenID, owner_address, name, image, background, weapon, mask, helmet, chest, arms
#        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
#    ''', (
'''        nftoken_id, owner_address, name, image,
        attributes.get("Background", ""),
        attributes.get("Weapon", ""),
        attributes.get("Mask", ""),
        attributes.get("Helmet", ""),
        attributes.get("Chest", ""),
        attributes.get("Arms", "")
    ))
    conn.commit()
'''

# DOWNLOAD THE IMAGE FROM IPFS
def download_image_from_ipfs(ipfs_link):
    # Convert the IPFS link to a gateway URL
    parts = ipfs_link.replace("ipfs://", "").split("/", 1)
    ipfs_hash = parts[0]
    ipfs_path = parts[1] if len(parts) > 1 else ""
    image_url = f"https://{ipfs_hash}.ipfs.dweb.link/{ipfs_path}"

    try:
        logger.info(f"Downloading image from IPFS: {image_url}")
        response = requests.get(image_url, stream=True)

        if response.status_code == 200:
            # Save the image to a temporary file
            file_name = os.path.basename(ipfs_path)
            file_path = os.path.join("/tmp", file_name)

            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)

            logger.info(f"Image downloaded successfully: {file_path}")
            return file_path
        else:
            logger.error(f"Failed to download image from IPFS: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error downloading image from IPFS: {e}")
        return None

# STORE NFTS NEW
def store_nft_in_database(nftoken_id, owner_address, metadata):
    name = metadata.get("name", "")
    image = metadata.get("image", "")
    burnCount = metadata.get("burnCount", 0)  # Assuming 'burnCount' is present in the metadata

    # Extract all the attributes from the metadata
    attributes = {attr["trait_type"]: attr["value"] for attr in metadata.get("attributes", [])}

    # Download the image from IPFS if the image link exists
    bunnynet_image_url = None
    if image.startswith("ipfs://"):
        # Download the actual image file from IPFS
        image_file_path = download_image_from_ipfs(image)

        if image_file_path:
            # Upload the image file to BunnyNet
            bunnynet_image_url = asyncio.run(upload_to_bunnycdn(image_file_path, name, metadata.get("burnCount", 0)))

            if bunnynet_image_url:
                logger.info(f"Image uploaded to BunnyNet: {bunnynet_image_url}")
            else:
                logger.error("Failed to upload image to BunnyNet.")
        else:
            logger.error("Failed to download image from IPFS.")
    else:
        logger.error("Image link is not a valid IPFS link.")

    # Use the BunnyNet URL in the database instead of the IPFS link
    image_to_store = bunnynet_image_url if bunnynet_image_url else image

    # Check if there are any attributes in the metadata
    if attributes:
        # Dynamically create the SQL statement based on the attribute keys
        attribute_keys = list(attributes.keys())  # List of trait types
        attribute_values = list(attributes.values())  # Corresponding trait values

        # Construct the field names for the SQL statement (columns)
        fields = ", ".join([f'"{key}"' for key in attribute_keys])
        placeholders = ", ".join(["?" for _ in attribute_values])  # SQL placeholders for values

        # Construct the full SQL command to insert the NFT metadata, including dynamic attributes
        sql = f'''
            INSERT OR REPLACE INTO nfts (
                NFTokenID, owner_address, name, image, {fields}
            ) VALUES (?, ?, ?, ?, {placeholders})
        '''

        # Prepare the values for insertion
        values = (nftoken_id, owner_address, name, image) + tuple(attribute_values)

        # Execute the SQL command with dynamic fields and values
        c.execute(sql, values)
        conn.commit()

        logger.info(f"Stored NFT {nftoken_id} with attributes: {attributes}")
    else:
        # Handle case where no attributes are present
        c.execute('''
            INSERT OR REPLACE INTO nfts (
                NFTokenID, owner_address, name, image, burnCount
            ) VALUES (?, ?, ?, ?)
        ''', (nftoken_id, owner_address, name, image))
        conn.commit()
        logger.info(f"Stored NFT {nftoken_id} with no attributes")

    # After storing the NFT metadata, upload the file to BunnyCDN
    if image:
        upload_url = asyncio.run(upload_to_bunnycdn(image, name, burnCount))
        logger.info(f"Uploaded to BunnyCDN: {upload_url}")


# UPLOAD TO BUNNYNET STORAGE
async def upload_to_bunnycdn(file_path, name, burnCount):
    # Parse NFT name to get the number for saving
    pattern = r"#(\d+)"
    match = re.search(pattern, name)
    if match:
        nft_number = match.group(1)
    else:
        nft_number = "null"
        logger.error("No NFT number found")

    key = os.getenv("BUNNYCDN_ACCESS_KEY")  # Bunny Access Key
    base_url = "https://storage.bunnycdn.com/lfgo/LFGO/"  # Base URL for CDN

    file_name = os.path.basename(file_path)
    file_extension = file_name.split(".")[-1].lower()

    with open(file_path, 'rb') as file:
        file_bytes = file.read()

    content_types = {
        "png": "image/png",
        "mp4": "video/mp4",
        "json": "application/json"
    }
    if file_extension not in content_types:
        raise ValueError("Unsupported file type. Only PNG, MP4 and json are allowed.")

    async with aiohttp.ClientSession() as session:
        url = f"{base_url}{nft_number}/{nft_number}_{burnCount}.{file_extension}"
        logger.info(f"Passing URL: {url}")

        headers = {
            "AccessKey": key,
            "Content-Type": content_types.get(file_extension),
        }
        async with session.put(url, headers=headers, data=file_bytes) as response:
            if response.status == 201:
                logger.info("File uploaded successfully")
                public_url = f"https://lfgo.b-cdn.net/LFGO/{nft_number}/{nft_number}_{burnCount}.{file_extension}"
                return public_url
            else:
                logger.error(f"Failed to upload file: {response.status}")
                logger.error(traceback.format_exc())
                return None

# PROCESS NFTS OLD
'''
async def process_nfts(issuer, taxon, ws_uris):
    marker = None
    total_nfts = 0
    while True:
        nfts, marker = await fetch_nfts_by_issuer_and_taxon(issuer, taxon, ws_uris, marker)
        if not nfts:
            logger.info("No more NFTs to process.")
            break

        for nft in nfts:
            if not nft.get("is_burned", False):
                nftoken_id = nft["nft_id"]
                owner_address = nft["owner"]
                uri_hex = nft.get("uri", "")
                uri_ascii = hex_to_ascii(uri_hex)
                metadata = fetch_ipfs_metadata(uri_ascii)

                store_nft_in_database(nftoken_id, owner_address, metadata)
                total_nfts += 1

        logger.info(f"Processed {total_nfts} NFTs so far.")

        if not marker:
            break  # No more pages to fetch
'''
            
def process_nfts(issuer, taxon):
    nfts = fetch_nfts_by_issuer_and_taxon(issuer, taxon)
    total_nfts = 0

    for nft in nfts:
        nftoken_id = nft["NFTokenID"]
        owner_address = nft["Owner"]
        uri_hex = nft.get("URI", "")
        uri_ascii = hex_to_str(uri_hex)  # Using hex_to_str from xrpl-py library
        metadata = fetch_ipfs_metadata(uri_ascii)

        store_nft_in_database(nftoken_id, owner_address, metadata)
        total_nfts += 1

    logger.info(f"Processed {total_nfts} NFTs.")

# MAIN OLD
'''
async def main():
    await process_nfts(issuer_address, taxon, ws_uris)
    logger.info("All NFTs have been processed and stored in the database.")

    # Close the database connection
    conn.close()

# Run the WebSocket client
asyncio.run(main())
'''

def main():
    process_nfts(issuer_address, taxon)
    logger.info("All NFTs have been processed and stored in the database.")

    # Close the database connection
    conn.close()

# Run the script
main()