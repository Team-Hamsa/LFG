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

key = "1ff10248-50e6-429b-94fe5097bf56-4502-4a55"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database setup (SQLite in this example)
conn = sqlite3.connect('nftdata.db')
c = conn.cursor()

# Create the nfts table
c.execute('''
    CREATE TABLE IF NOT EXISTS nfts (
        NFTokenID TEXT PRIMARY KEY,
        owner_address TEXT,
        name TEXT,
        image TEXT,
        burnCount INTEGER
    );
''')

# Create the nft_attributes table
c.execute('''
    CREATE TABLE IF NOT EXISTS nft_attributes (
        NFTokenID TEXT,
        trait_type TEXT,
        value TEXT,
        FOREIGN KEY (NFTokenID) REFERENCES nfts(NFTokenID)
    );
''')
conn.commit()



# API URL template
api_url_template = "https://api.xrpldata.com/api/v1/xls20-nfts/issuer/{issuer}/taxon/{taxon}"


# Replace with your issuer's address and desired taxon
issuer_address = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
taxon = 1760

    
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

# STORE NFTS IN DATABASE NEW
def store_nft_in_database(nftoken_id, owner_address, metadata):
    name = metadata.get("name", "")
    image = metadata.get("image", "")
    burnCount = metadata.get("burnCount", 0)

    # Extract all the attributes from the metadata
    attributes = {attr["trait_type"]: attr["value"] for attr in metadata.get("attributes", [])}

    # Download the image from IPFS if the image link exists
    bunnynet_image_url = None
    if image.startswith("ipfs://"):
        image_file_path = download_image_from_ipfs(image)

        if image_file_path:
            # Upload the image file to BunnyNet
            bunnynet_image_url = asyncio.run(upload_to_bunnycdn(image_file_path, name, burnCount))

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

    # Modify the metadata to update the image link to the BunnyCDN link
    metadata["image"] = bunnynet_image_url

    # Save the modified metadata to a JSON file
    metadata_file_path = f"/tmp/{nftoken_id}_{burnCount}.json"
    with open(metadata_file_path, 'w') as metadata_file:
        json.dump(metadata, metadata_file)

    # Upload the modified metadata JSON file to BunnyCDN
    bunnynet_metadata_url = asyncio.run(upload_to_bunnycdn(metadata_file_path, name, burnCount, is_metadata=True))

    if bunnynet_metadata_url:
        logger.info(f"Metadata JSON uploaded to BunnyNet: {bunnynet_metadata_url}")
    else:
        logger.error("Failed to upload metadata JSON to BunnyNet.")

    # Insert NFT basic data into the `nfts` table
    c.execute('''
        INSERT OR REPLACE INTO nfts (
            NFTokenID, owner_address, name, image, burnCount
        ) VALUES (?, ?, ?, ?, ?)
    ''', (nftoken_id, owner_address, name, image_to_store, burnCount))
    conn.commit()

    logger.info(f"Stored NFT {nftoken_id} in nfts table.")

    # Insert each attribute into the `nft_attributes` table
    for trait_type, value in attributes.items():
        c.execute('''
            INSERT OR REPLACE INTO nft_attributes (
                NFTokenID, trait_type, value
            ) VALUES (?, ?, ?)
        ''', (nftoken_id, trait_type, value))
    conn.commit()

    logger.info(f"Stored attributes for NFT {nftoken_id} in nft_attributes table.")


# UPLOAD TO BUNNYNET STORAGE
async def upload_to_bunnycdn(file_path, name, burnCount, is_metadata=False):
    # Parse NFT name to get the number for saving
    pattern = r"#(\d+)"
    match = re.search(pattern, name)
    if match:
        nft_number = match.group(1)
    else:
        nft_number = "null"
        logger.error("No NFT number found")

    key = "1ff10248-50e6-429b-94fe5097bf56-4502-4a55"
    base_url = "https://storage.bunnycdn.com/lfgo/LFGO/"

    file_name = os.path.basename(file_path)
    file_extension = file_name.split(".")[-1].lower()

    with open(file_path, 'rb') as file:
        file_bytes = file.read()

    # Content types for images, mp4s, and JSON (metadata)
    content_types = {
        "png": "image/png",
        "mp4": "video/mp4",
        "json": "application/json"
    }

    if is_metadata:
        file_extension = "json"  # Ensure file is treated as JSON for metadata
        url = f"{base_url}{nft_number}/{nft_number}_{burnCount}.json"
    else:
        url = f"{base_url}{nft_number}/{nft_number}_{burnCount}.{file_extension}"

    logger.info(f"Passing URL: {url}")

    headers = {
        "AccessKey": key,
        "Content-Type": content_types.get(file_extension),
    }

    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers, data=file_bytes) as response:
            if response.status == 201:
                logger.info("File uploaded successfully")
                public_url = f"https://lfgo.b-cdn.net/LFGO/{nft_number}/{nft_number}_{burnCount}.{file_extension}"
                return public_url
            else:
                logger.error(f"Failed to upload file: {response.status}")
                logger.error(traceback.format_exc())
                return None

            
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


def main():
    process_nfts(issuer_address, taxon)
    logger.info("All NFTs have been processed and stored in the database.")

    # Close the database connection
    conn.close()

# Run the script
main()