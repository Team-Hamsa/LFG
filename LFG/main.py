import os
import random
import json
import asyncio
import discord
from discord import app_commands, TextStyle
from discord.ui import Button, View, Modal, TextInput
from xumm import XummSdk
from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.transactions import NFTokenMint, NFTokenBurn
from BunnyCDN.Storage import Storage
from BunnyCDN.CDN import CDN
import ffmpeg
from dotenv import load_dotenv
import logging
import re
import requests
import aiohttp
import time
from xrpl.models.requests import Tx
from xrpl.models.transactions import NFTokenCreateOffer, NFTokenCreateOfferFlag
from xrpl.models import IssuedCurrencyAmount
from xrpl.transaction import submit_and_wait
from discord import Embed
import tempfile
from PIL import Image
import qrcode
import io
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import Subscribe, StreamParameter
from db_helpers import get_next_nft_number, record_nft_mint
from discord.ext import commands
from discord.errors import DiscordServerError
import signal
import traceback
from user_db import register_user, create_users_table, get_user as get_user_from_db
import sqlite3
import shutil

# Import the NFT minting helper function from ts_helpers.py
from ts_helpers import mint_nft as helper_mint_nft

# Check if ffmpeg is installed
if not shutil.which('ffmpeg'):
    logging.error("=" * 60)
    logging.error("ERROR: ffmpeg is not installed!")
    logging.error("=" * 60)
    logging.error("ffmpeg is required for combining NFT trait images.")
    logging.error("")
    logging.error("To install ffmpeg:")
    logging.error("  - Run: ./setup.sh")
    logging.error("  - Or manually: sudo apt-get install ffmpeg")
    logging.error("  - Or see: https://ffmpeg.org/download.html")
    logging.error("")
    raise RuntimeError(
        "ffmpeg is not installed. Please install it using ./setup.sh or manually."
    )

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # This sends output to terminal
    ]
)

# Load environment variables from .env
load_dotenv()

# API Keys and Core Settings
XUMM_API_KEY = os.getenv("XUMM_API_KEY")
if not XUMM_API_KEY:
    raise ValueError("XUMM_API_KEY not found in environment variables")

XUMM_API_SECRET = os.getenv("XUMM_API_SECRET")
if not XUMM_API_SECRET:
    raise ValueError("XUMM_API_SECRET not found in environment variables")

XUMM_API_URL = os.getenv("XUMM_API_URL", "https://xumm.app/api/v1/platform/payload")

# Discord Settings
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN not found in environment variables")

# Discord Settings
ADMIN_LOG_CHANNEL_ID = int(os.getenv("ADMIN_LOG_CHANNEL_ID", "0"))
if not ADMIN_LOG_CHANNEL_ID:
    raise ValueError("ADMIN_LOG_CHANNEL_ID not found in environment variables")

# XRPL Settings
SEED = os.getenv("SEED")
if not SEED:
    raise ValueError("SEED not found in environment variables")

TOKEN_ISSUER_ADDRESS = os.getenv("TOKEN_ISSUER_ADDRESS")
if not TOKEN_ISSUER_ADDRESS:
    raise ValueError("TOKEN_ISSUER_ADDRESS not found in environment variables")
logging.info(f"Loaded TOKEN_ISSUER_ADDRESS: {TOKEN_ISSUER_ADDRESS}")

TOKEN_CURRENCY_HEX = os.getenv("TOKEN_CURRENCY_HEX")
if not TOKEN_CURRENCY_HEX:
    raise ValueError("TOKEN_CURRENCY_HEX not found in environment variables")

NFT_TAXON = int(os.getenv("NFT_TAXON", "0"))
logging.info(f"Using NFT_TAXON: {NFT_TAXON}")

TOKEN_TRUSTLINE_LIMIT = os.getenv("TOKEN_TRUSTLINE_LIMIT", "1000")


# BunnyCDN Settings
BUNNY_CDN_ACCESS_KEY = os.getenv("BUNNY_CDN_ACCESS_KEY")
if not BUNNY_CDN_ACCESS_KEY:
    raise ValueError("BUNNY_CDN_ACCESS_KEY not found in environment variables")

BUNNY_CDN_STORAGE_ZONE = os.getenv("BUNNY_CDN_STORAGE_ZONE")
if not BUNNY_CDN_STORAGE_ZONE:
    raise ValueError("BUNNY_CDN_STORAGE_ZONE not found in environment variables")

BUNNY_CDN_BASE_URL = os.getenv("BUNNY_CDN_BASE_URL", "https://storage.bunnycdn.com")
if not BUNNY_CDN_BASE_URL:
    raise ValueError("BUNNY_CDN_BASE_URL not found in environment variables")

BUNNY_CDN_FOLDER = os.getenv("BUNNY_CDN_FOLDER", "minttest")
if not BUNNY_CDN_FOLDER:
    raise ValueError("BUNNY_CDN_FOLDER not found in environment variables")


# NFT Settings
NFT_COLLECTION_NAME = os.getenv("NFT_COLLECTION_NAME", "Let's Effing Go!")
if not NFT_COLLECTION_NAME:
    raise ValueError("NFT_COLLECTION_NAME not found in environment variables")

NFT_COLLECTION_FAMILY = os.getenv("NFT_COLLECTION_FAMILY", "Test")
if not NFT_COLLECTION_FAMILY:
    raise ValueError("NFT_COLLECTION_FAMILY not found in environment variables")

NFT_DESCRIPTION = os.getenv("NFT_DESCRIPTION", "Test")
if not NFT_DESCRIPTION:
    raise ValueError("NFT_DESCRIPTION not found in environment variables")

NFT_TRANSFER_FEE = int(os.getenv("NFT_TRANSFER_FEE", "7000"))
if not NFT_TRANSFER_FEE:
    raise ValueError("NFT_TRANSFER_FEE not found in environment variables")

NFT_FLAGS = int(os.getenv("NFT_FLAGS", "24"))
if not NFT_FLAGS:
    raise ValueError("NFT_FLAGS not found in environment variables")

NFT_SCHEMA_URL = os.getenv("NFT_SCHEMA_URL", "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU")
if not NFT_SCHEMA_URL:
    raise ValueError("NFT_SCHEMA_URL not found in environment variables")

# External URLs
EXTERNAL_WEBSITE_URL = os.getenv("EXTERNAL_WEBSITE_URL", "https://letseffinggo.com")
if not EXTERNAL_WEBSITE_URL:
    raise ValueError("EXTERNAL_WEBSITE_URL not found in environment variables")

# Retry and Timeout Settings
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))
SESSION_TIMEOUT_TOTAL = int(os.getenv("SESSION_TIMEOUT_TOTAL", "60"))
SESSION_TIMEOUT_CONNECT = int(os.getenv("SESSION_TIMEOUT_CONNECT", "20"))
SESSION_TIMEOUT_READ = int(os.getenv("SESSION_TIMEOUT_READ", "30"))
VIEW_TIMEOUT = int(os.getenv("VIEW_TIMEOUT", "600"))

# Update the metadata template
METADATA_TEMPLATE = {
    "schema": NFT_SCHEMA_URL,
    "name": "",  # Will be filled in with collection name and number
    "description": NFT_DESCRIPTION,
    "image": "",  # Will be filled with CDN URL
    "video": "",  # Empty string instead of None
    "external_link": EXTERNAL_WEBSITE_URL,
    "collection": {
        "name": NFT_COLLECTION_NAME,
        "family": NFT_COLLECTION_FAMILY
    },
    "edition": 0,  # Integer instead of None
    "attributes": []  # Will be filled with the traits
}

logging.basicConfig(level=logging.INFO)

# Initialize Discord bot with slash commands and retry capability
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

class RetryBot(commands.Bot):
    async def start(self, *args, **kwargs):
        max_retries = RETRY_MAX_ATTEMPTS
        base_delay = RETRY_BASE_DELAY
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    jitter = random.uniform(0, 2)
                    actual_delay = (base_delay * (2 ** attempt)) + jitter
                    logging.info(f"Retry attempt {attempt + 1}/{max_retries} after {actual_delay:.2f}s delay")
                    await asyncio.sleep(actual_delay)
                    
                await super().start(*args, **kwargs)
                return
            except Exception as e:
                logging.error(f"Connection attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise
bot = RetryBot(command_prefix="!", intents=intents)
tree = bot.tree

# Initialize XUMM SDK
sdk = XummSdk(XUMM_API_KEY, XUMM_API_SECRET)

# XRPL Testnet client
JSON_RPC_URL = "https://s.altnet.rippletest.net:51234/"
WS_URL = "wss://s.altnet.rippletest.net:51233"  # WebSocket URL for subscriptions
client = JsonRpcClient(JSON_RPC_URL)

# Initialize BunnyCDN
storage = Storage(BUNNY_CDN_ACCESS_KEY, BUNNY_CDN_STORAGE_ZONE)

# Constants for NFT minting using the helper function from ts_helpers.py
NFT_TOKEN_TAXON = int(os.getenv("NFT_TOKEN_TAXON", "0"))  # Defaults to 0 if not defined

# Path to the trait layers directory
TRAIT_LAYERS_DIR = "trait_layers"

# Add these constants (or get from env)
X_API_KEY = os.getenv("XUMM_API_KEY")
X_API_SECRET = os.getenv("XUMM_API_SECRET")

DATABASE = "lfg_nfts.db"

def get_user(user):
    return get_user_from_db(str(user.id))


def get_trait_files(trait_layer_dir):
    """
    Get a list of image files in the given trait layer directory.
    Filters out system files and only includes supported image formats.
    """
    SUPPORTED_FORMATS = ['.png', '.jpg', '.jpeg', '.gif']
    return [
        f for f in os.listdir(trait_layer_dir) 
        if os.path.isfile(os.path.join(trait_layer_dir, f))
        and not f.startswith('.')  # Exclude hidden files
        and os.path.splitext(f)[1].lower() in SUPPORTED_FORMATS  # Only include supported image formats
    ]


def get_random_trait(trait_layer_dir):
    """
    Randomly select an image file from the given trait layer directory.
    """
    files = get_trait_files(trait_layer_dir)
    if not files:
        raise ValueError(f"No valid image files found in directory: {trait_layer_dir}")
    return random.choice(files)


def get_sorted_trait_layers(trait_layers_dir):
    """
    Returns a list of trait layer folder names in the order they should be applied.
    - If any folder name starts with a number, sort the folders by that numeric prefix.
    - Otherwise, use a fallback trait order.
    """
    directories = [
        d for d in os.listdir(trait_layers_dir)
        if os.path.isdir(os.path.join(trait_layers_dir, d))
    ]
    
    # Check if any folder starts with a number
    has_numeric_prefix = any(re.match(r'^\d+', d) for d in directories)
    
    if has_numeric_prefix:
        def sort_key(folder_name):
            match = re.match(r'^(\d+)', folder_name)
            return int(match.group(1)) if match else float('inf')
        sorted_dirs = sorted(directories, key=sort_key)
    else:
        # Use a fallback order for common NFT trait layers
        TRAIT_ORDER = ["background", "body", "clothing", "mouth", "eyebrows", "eyes", "mouth", "hat:hair", "accessory"]
        sorted_dirs = sorted(
            directories,
            key=lambda d: (TRAIT_ORDER.index(d.lower()) if d.lower() in TRAIT_ORDER else float('inf'), d)
        )
    
    return sorted_dirs


# Add these helper functions
def convert_str_to_hex(string):
    """Convert string to hex for XRPL URI"""
    return string.encode('utf-8').hex().upper()

def format_trait_name(text: str) -> str:
    """Convert a trait name to capitalized format"""
    # Remove numeric prefix and extra spaces
    clean_text = re.sub(r'^\d+\s+', '', text).strip()
    # Capitalize each word
    return ' '.join(word.capitalize() for word in clean_text.split())

async def mint_nft_for_user(metadata_cdn_url, taxon, issuer):
    """Async wrapper for minting NFT"""
    try:
        logging.info("=== Starting NFT minting process ===")
        logging.info(f"Parameters received:")
        logging.info(f"Metadata URL: {metadata_cdn_url}")
        logging.info(f"Taxon: {taxon}")
        logging.info(f"Issuer: {issuer}")

        wallet = Wallet.from_seed(SEED)
        logging.info(f"Wallet initialized with address: {wallet.classic_address}")
        
        client = JsonRpcClient(JSON_RPC_URL)
        logging.info(f"XRPL client initialized with URL: {JSON_RPC_URL}")

        # Create NFTokenMint transaction without issuer if it's the same as the account
        if issuer == wallet.classic_address:
            logging.info("Issuer matches wallet address, creating NFTokenMint without issuer")
            payment = NFTokenMint(
                account=wallet.classic_address,
                uri=convert_str_to_hex(metadata_cdn_url),
                nftoken_taxon=taxon,
                transfer_fee=NFT_TRANSFER_FEE,
                flags=NFT_FLAGS,
            )
        else:
            logging.info("Creating NFTokenMint with separate issuer")
            payment = NFTokenMint(
                account=wallet.classic_address,
                uri=convert_str_to_hex(metadata_cdn_url),
                nftoken_taxon=taxon,
                issuer=issuer,
                transfer_fee=NFT_TRANSFER_FEE,
                flags=NFT_FLAGS,
            )
        
        logging.info(f"NFTokenMint transaction prepared: {payment.to_dict()}")

        retries = 5
        for attempt in range(1, retries + 1):
            try:
                logging.info(f"Submitting transaction (attempt {attempt}/{retries})")
                payment_response = await asyncio.to_thread(submit_and_wait, payment, client, wallet)
                logging.info(f"Transaction submitted successfully")
                logging.info(f"Response: {json.dumps(payment_response.result, indent=2)}")
                hashTxn = payment_response.result["hash"]
                logging.info(f"Transaction hash: {hashTxn}")
                break
            except Exception as e:
                logging.error(f"=== Error in mint attempt {attempt} ===")
                logging.error(f"Error type: {type(e).__name__}")
                logging.error(f"Error message: {str(e)}")
                logging.error(f"Full traceback: {traceback.format_exc()}")
                if attempt == retries:
                    logging.error("All mint attempts failed")
                    return None
                logging.info(f"Waiting 5 seconds before retry...")
                await asyncio.sleep(5)

        # Check transaction status
        logging.info("Starting transaction status check")
        for check_attempt in range(1, retries + 1):
            try:
                logging.info(f"Checking transaction status (attempt {check_attempt}/{retries})")
                txn = await asyncio.to_thread(client.request, Tx(transaction=hashTxn))
                res = txn.result
                logging.info(f"Transaction result: {json.dumps(res, indent=2)}")
                
                if res["meta"]["TransactionResult"] == "tesSUCCESS":
                    nft_id = res["meta"].get("nftoken_id")
                    if nft_id:
                        logging.info(f"NFT minted successfully with ID: {nft_id}")
                        return nft_id
                    else:
                        logging.warning("Transaction successful but no NFT ID found")
                else:
                    logging.warning(f"Transaction result was not successful: {res['meta']['TransactionResult']}")
                break
            except Exception as e:
                logging.error(f"=== Error in status check attempt {check_attempt} ===")
                logging.error(f"Error type: {type(e).__name__}")
                logging.error(f"Error message: {str(e)}")
                logging.error(f"Full traceback: {traceback.format_exc()}")
                if check_attempt == retries:
                    logging.error("All status check attempts failed")
                    return None
                logging.info(f"Waiting 5 seconds before retry...")
                await asyncio.sleep(5)

        logging.warning("NFT minting process completed but no NFT ID returned")
        return None

    except Exception as e:
        logging.error("=== Error in mint_nft_for_user ===")
        logging.error(f"Error type: {type(e).__name__}")
        logging.error(f"Error message: {str(e)}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        return None

async def create_nft_offer(nft_id, destination):
    """Async wrapper for creating NFT offer"""
    try:
        client = JsonRpcClient(JSON_RPC_URL)
        wallet = Wallet.from_seed(SEED)
        
        payment = NFTokenCreateOffer(
            account=wallet.classic_address,
            destination=destination,
            amount="0",
            nftoken_id=nft_id,
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        )

        payment_response = await asyncio.to_thread(submit_and_wait, payment, client, wallet)
        hashTxn = payment_response.result["hash"]
        logging.info(f"Offer transaction submitted: {hashTxn}")

        # Check for offer ID
        for _ in range(3):
            try:
                txn = await asyncio.to_thread(client.request, Tx(transaction=hashTxn))
                res = txn.result
                if res["meta"]["TransactionResult"] == "tesSUCCESS":
                    offer_id = res["meta"]["offer_id"]
                    logging.info(f"Offer created successfully: {offer_id}")
                    return offer_id
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"Error checking offer status: {e}")
                await asyncio.sleep(5)

        return None

    except Exception as e:
        logging.error(f"Error in create_nft_offer: {e}")
        return None

def generate_static_payment_link(destination: str, currency: str, issuer: str, value: str = "1") -> str:
    """
    Generate a static payment link using xaman.app/detect format.
    This works with XUMM, Xaman, and other XRPL wallets.
    """
    transaction_json = {
        "TransactionType": "Payment",
        "Destination": destination,
        "Amount": {
            "currency": currency,
            "value": value,
            "issuer": issuer
        }
    }
    
    # Convert transaction JSON to hex string
    tx_str = json.dumps(transaction_json)
    tx_hex = tx_str.encode('utf-8').hex()
    
    # Generate the static link
    payment_link = f"https://xaman.app/detect/{tx_hex}"
    return payment_link

def generate_qr_code_image(data: str) -> io.BytesIO:
    """
    Generate a QR code image from a string and return it as BytesIO.
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    return img_bytes

async def create_payment_request_static(destination: str) -> dict:
    """
    Create a static payment link and QR code (no XUMM API required).
    Returns dict with 'payment_link' and 'qr_image_bytes' (BytesIO).
    """
    logging.info("=== Creating static payment request ===")
    logging.info(f"Destination address: {destination}")
    
    try:
        # Generate static payment link
        payment_link = generate_static_payment_link(
            destination=destination,
            currency=TOKEN_CURRENCY_HEX,
            issuer=TOKEN_ISSUER_ADDRESS,
            value="1"
        )
        
        logging.info(f"Generated payment link: {payment_link}")
        
        # Generate QR code locally
        qr_image_bytes = generate_qr_code_image(payment_link)
        
        return {
            'payment_link': payment_link,
            'qr_image_bytes': qr_image_bytes,
            'destination': destination
        }
        
    except Exception as e:
        logging.error(f"Error creating static payment request: {e}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        return None

async def wait_for_payment_via_subscription(
    destination: str,
    expected_sender: str,  # The wallet address of the user who should send the payment
    expected_amount: str,
    currency: str,
    issuer: str,
    timeout_seconds: int = 300
) -> bool:
    """
    Subscribe to account transactions and wait for a payment matching the criteria.
    
    IMPORTANT: This verifies BOTH:
    1. Payment is received at the destination address
    2. Payment is FROM the expected sender address (user's wallet)
    
    This prevents one user's payment from triggering another user's mint.
    
    Args:
        destination: The address receiving the payment (TOKEN_ISSUER_ADDRESS)
        expected_sender: The wallet address of the user who should send the payment
        expected_amount: Expected payment amount (e.g., "1")
        currency: Currency code (hex format)
        issuer: Issuer address
        timeout_seconds: How long to wait for payment
        
    Returns:
        True if payment is received from the expected sender, False if timeout
    """
    logging.info(f"Starting payment subscription for {destination}")
    logging.info(f"Waiting for payment FROM {expected_sender} TO {destination}")
    logging.info(f"Payment details: {expected_amount} {currency} from issuer {issuer}")
    
    start_time = time.time()
    
    try:
        async with AsyncWebsocketClient(WS_URL) as websocket:
            # Subscribe to account transactions for the destination
            subscribe_request = Subscribe(
                accounts=[destination]
            )
            await websocket.send(subscribe_request)
            logging.info(f"Subscribed to account: {destination}")
            
            # Listen for transactions
            async for message in websocket:
                # Check timeout
                if time.time() - start_time > timeout_seconds:
                    logging.info("Payment subscription timeout")
                    return False
                
                # Check if this is a transaction message
                if 'transaction' in message and message.get('type') == 'transaction':
                    tx = message.get('transaction', {})
                    
                    # Check if it's a Payment transaction
                    if tx.get('TransactionType') == 'Payment':
                        # CRITICAL: Verify the sender matches the expected user
                        sender = tx.get('Account', '')
                        if sender != expected_sender:
                            logging.debug(f"Ignoring payment from {sender} (expected {expected_sender})")
                            continue
                        
                        # Check if payment is TO the destination (incoming)
                        if tx.get('Destination') == destination:
                            amount = tx.get('Amount', {})
                            
                            # Check if it's the expected currency
                            if isinstance(amount, dict):
                                if (amount.get('currency') == currency and 
                                    amount.get('issuer') == issuer and
                                    amount.get('value') == expected_amount):
                                    logging.info(f"✅ Payment received from {expected_sender}! Transaction: {tx.get('hash')}")
                                    logging.info(f"   Amount: {expected_amount} {currency}")
                                    logging.info(f"   From: {sender}")
                                    logging.info(f"   To: {destination}")
                                    return True
                            # Also check XRP payments (though unlikely for token payments)
                            elif isinstance(amount, str):
                                # XRP payment - not what we're looking for
                                pass
                
                # Also check for account transactions in different message formats
                if 'account' in message and message.get('account') == destination:
                    if 'transaction' in message:
                        tx = message['transaction']
                        if tx.get('TransactionType') == 'Payment':
                            # CRITICAL: Verify the sender matches the expected user
                            sender = tx.get('Account', '')
                            if sender != expected_sender:
                                logging.debug(f"Ignoring payment from {sender} (expected {expected_sender})")
                                continue
                                
                            amount = tx.get('Amount', {})
                            if isinstance(amount, dict):
                                if (amount.get('currency') == currency and 
                                    amount.get('issuer') == issuer and
                                    amount.get('value') == expected_amount):
                                    logging.info(f"✅ Payment received from {expected_sender}! Transaction: {tx.get('hash')}")
                                    logging.info(f"   Amount: {expected_amount} {currency}")
                                    logging.info(f"   From: {sender}")
                                    logging.info(f"   To: {destination}")
                                    return True
                                    
    except asyncio.TimeoutError:
        logging.info("Payment subscription timeout")
        return False
    except Exception as e:
        logging.error(f"Error in payment subscription: {e}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        return False
    
    return False

async def generate_xumm_qr(offer_id):
    """Generate XUMM QR code for NFT acceptance"""
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": X_API_KEY,
        "X-API-Secret": X_API_SECRET,
    }

    # Create the transaction JSON
    transaction_json = {
        "TransactionType": "NFTokenAcceptOffer",
        "NFTokenSellOffer": offer_id
    }

    payload = {
        "txjson": transaction_json
    }

    try:
        # Make the API request
        response = await asyncio.to_thread(
            requests.post,
            XUMM_API_URL,
            json=payload,
            headers=headers
        )
        response_data = response.json()

        return {
            'qr_url': response_data['refs']['qr_png'],
            'xumm_url': response_data['next']['always'],
            'uuid': response_data['uuid']
        }

    except Exception as e:
        logging.error(f"Error generating XUMM QR: {e}")
        return None

async def create_payment_request(destination: str) -> dict:
    """
    Create a static payment request (no XUMM API required).
    Uses xaman.app/detect format that works with all XRPL wallets.
    """
    return await create_payment_request_static(destination)

async def check_payment_status(
    destination: str, 
    expected_sender: str,  # User's wallet address
    expected_amount: str = "1", 
    timeout_seconds: int = 300
) -> bool:
    """
    Check if a payment has been received by subscribing to account transactions.
    This replaces the old UUID-based checking with direct XRPL subscription.
    
    IMPORTANT: Verifies the payment came from the expected sender to prevent
    one user's payment from triggering another user's mint.
    """
    return await wait_for_payment_via_subscription(
        destination=destination,
        expected_sender=expected_sender,
        expected_amount=expected_amount,
        currency=TOKEN_CURRENCY_HEX,
        issuer=TOKEN_ISSUER_ADDRESS,
        timeout_seconds=timeout_seconds
    )

async def create_trustline_request() -> dict:
    """Create a XUMM request to set up token trustline"""
    logging.info("Creating trustline request")
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": X_API_KEY,
        "X-API-Secret": X_API_SECRET,
    }

    # Create the transaction JSON for setting up token trustline
    transaction_json = {
        "TransactionType": "TrustSet",
        "Flags": 131072,  # tfSetNoRipple flag
        "LimitAmount": {
            "currency": TOKEN_CURRENCY_HEX,
            "issuer": TOKEN_ISSUER_ADDRESS,
            "value": TOKEN_TRUSTLINE_LIMIT
        }
    }
    logging.info(f"Trustline transaction JSON: {json.dumps(transaction_json, indent=2)}")

    payload = {
        "txjson": transaction_json,
        "options": {
            "expire": 5,  # Expires in 5 minutes
            "return_url": {
                "web": "https://letseffinggo.com/"
            }
        }
    }

    try:
        # Make the API request
        response = await asyncio.to_thread(
            requests.post,
            XUMM_API_URL,
            json=payload,
            headers=headers
        )
        response_data = response.json()

        return {
            'qr_url': response_data['refs']['qr_png'],
            'xumm_url': response_data['next']['always'],
            'uuid': response_data['uuid']
        }

    except Exception as e:
        logging.error(f"Error generating trustline request: {e}")
        return None

async def safe_followup(interaction: discord.Interaction, *args, **kwargs):
    """followup.send that survives an expired/invalid interaction token.
    Discord webhook tokens last 15 minutes; long-running handlers (payment
    or trustline polling) can outlive them, and the resulting 401 (50027)
    must not crash the handler. Returns True if the message was delivered."""
    try:
        await interaction.followup.send(*args, **kwargs)
        return True
    except (discord.NotFound, discord.HTTPException) as e:
        logging.warning(f"Follow-up message not delivered "
                        f"(interaction token likely expired): {e}")
        return False


class MintView(View):
    def __init__(self):
        logging.info("=== Initializing MintView ===")
        super().__init__(timeout=VIEW_TIMEOUT)
        logging.info(f"View timeout set to: {VIEW_TIMEOUT}")
        
        self.buy_button = Button(
            label="💰 Buy Token", 
            style=discord.ButtonStyle.success, 
            url=EXTERNAL_WEBSITE_URL
        )
        self.add_item(self.buy_button)
        logging.info("Buy button added to view")

    @discord.ui.button(label="🎨 Mint NFT", style=discord.ButtonStyle.primary)
    async def mint_button(self, interaction: discord.Interaction, button: Button):
        logging.info("=== Mint button pressed ===")
        logging.info(f"User ID: {interaction.user.id}")
        logging.info(f"Username: {interaction.user.name}")
        
        await interaction.response.defer(ephemeral=True)
        logging.info("Interaction deferred")
        
        # Get user's wallet address
        user_data = get_user(interaction.user)
        logging.info(f"Retrieved user data: {json.dumps(user_data, indent=2) if user_data else 'None'}")
        
        if not user_data or not user_data.get("address"):
            logging.warning("No wallet address found for user")
            await interaction.followup.send(
                "Please register your wallet first using /register",
                ephemeral=True
            )
            return

        try:
            logging.info("=== Starting payment process ===")
            logging.info(f"Using TOKEN_ISSUER_ADDRESS: {TOKEN_ISSUER_ADDRESS}")
            
            # Create payment request
            payment_data = await create_payment_request(TOKEN_ISSUER_ADDRESS)
            
            if not payment_data:
                logging.error("Failed to create payment request")
                await interaction.followup.send(
                    "Failed to create payment request. Please try again.",
                    ephemeral=True
                )
                return

            logging.info(f"Payment request created: {payment_data['payment_link']}")
            
            # Upload QR code to CDN
            qr_filename = f"payment_qr_{int(time.time())}.png"
            qr_cdn_url = None
            try:
                async with aiohttp.ClientSession() as session:
                    qr_url = f"https://storage.bunnycdn.com/lfgo/minttest/{qr_filename}"
                    headers = {
                        "AccessKey": BUNNY_CDN_ACCESS_KEY,
                        "Content-Type": "image/png",
                    }
                    payment_data['qr_image_bytes'].seek(0)
                    await session.put(qr_url, headers=headers, data=payment_data['qr_image_bytes'].read())
                    qr_cdn_url = f"https://lfgo.b-cdn.net/minttest/{qr_filename}"
                    logging.info(f"QR code uploaded to: {qr_cdn_url}")
            except Exception as e:
                logging.error(f"Failed to upload QR code to CDN: {e}")
                # Fallback: use file attachment
                pass
            
            # Create embed for payment
            embed = Embed(
                title="💰 Token Payment Required",
                description=(
                    "Please pay 1 token to mint your NFT.\n\n"
                    "**Steps:**\n"
                    "1. Scan the QR code with your XRPL wallet (XUMM, Xaman, etc.)\n"
                    "2. Approve the payment\n"
                    "3. Wait for confirmation\n\n"
                    f"[Open Payment Link]({payment_data['payment_link']})"
                ),
                color=0x00ff00
            )
            
            if qr_cdn_url:
                embed.set_image(url=qr_cdn_url)
            else:
                # Fallback: attach QR code as file
                payment_data['qr_image_bytes'].seek(0)
                file = discord.File(payment_data['qr_image_bytes'], filename="payment_qr.png")
                embed.set_image(url="attachment://payment_qr.png")
            
            embed.set_footer(text="Payment request expires in 5 minutes")
            
            logging.info("Payment embed created, sending to user")
            if qr_cdn_url:
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                payment_data['qr_image_bytes'].seek(0)
                file = discord.File(payment_data['qr_image_bytes'], filename="payment_qr.png")
                await interaction.followup.send(embed=embed, file=file, ephemeral=True)

            # Wait for payment using subscription
            # CRITICAL: Pass the user's wallet address to verify payment came from them
            user_wallet_address = user_data["address"]
            logging.info(f"Starting payment subscription monitoring for user: {user_wallet_address}")
            payment_received = await check_payment_status(
                destination=TOKEN_ISSUER_ADDRESS,
                expected_sender=user_wallet_address,  # Verify payment came from this user
                expected_amount="1",
                timeout_seconds=300
            )
            
            if payment_received:
                logging.info("Payment confirmed! Proceeding with NFT mint")
                await interaction.followup.send(
                    "✅ Payment received! Starting NFT mint process...",
                    ephemeral=True
                )
                
                # Get the next NFT number from the LFG table
                nft_number = get_next_nft_number()
                logging.info(f"Generated NFT number: {nft_number}")
                
                # Generate and upload NFT image and metadata
                selected_traits = {}
                combined_image_path = f"output_nft_{nft_number}.png"
                input_images = []
                
                # Select and combine trait images
                trait_layer_folders = get_sorted_trait_layers(TRAIT_LAYERS_DIR)
                for layer in trait_layer_folders:
                    layer_dir = os.path.join(TRAIT_LAYERS_DIR, layer)
                    if os.path.isdir(layer_dir):
                        valid_files = [f for f in os.listdir(layer_dir) 
                                     if not f.startswith('.') and f.lower().endswith('.png')]
                        if valid_files:
                            selected_file = random.choice(valid_files)
                            selected_traits[layer] = selected_file
                            input_images.append(os.path.join(layer_dir, selected_file))
                
                # Generate composite image using ffmpeg
                if input_images:
                    try:
                        stream = ffmpeg.input(input_images[0])
                        for additional_image in input_images[1:]:
                            stream = ffmpeg.overlay(stream, ffmpeg.input(additional_image))
                        
                        # Modified ffmpeg output command with proper parameters for single image
                        stream = ffmpeg.output(
                            stream, 
                            combined_image_path,
                            vframes=1,
                            update=1,  # Allows overwriting single image
                            loglevel='error'  # Reduce logging output
                        )
                        ffmpeg.run(stream, overwrite_output=True, capture_stdout=True, capture_stderr=True)
                        
                    except ffmpeg.Error as e:
                        error_msg = e.stderr.decode() if e.stderr else str(e)
                        logging.error(f"FFmpeg error: {error_msg}")
                        raise Exception(f"Failed to generate composite image: {error_msg}")
                
                # Upload image to BunnyCDN
                image_filename = f"lfg_{nft_number}.png"
                async with aiohttp.ClientSession() as session:
                    image_url = f"https://storage.bunnycdn.com/lfgo/minttest/{image_filename}"
                    headers = {
                        "AccessKey": BUNNY_CDN_ACCESS_KEY,
                        "Content-Type": "image/png",
                    }
                    with open(combined_image_path, 'rb') as file:
                        await session.put(image_url, headers=headers, data=file.read())
                
                # Define the CDN URL for the uploaded image
                image_cdn_url = f"https://lfgo.b-cdn.net/minttest/{image_filename}"
                
                # Generate and upload metadata
                metadata = {
                    "name": f"Let's Effing Go! #{nft_number}",
                    "image": image_cdn_url,
                    "edition": nft_number,
                    "attributes": [
                        {"trait_type": layer, "value": format_trait_name(os.path.splitext(selected_file)[0])} 
                        for layer, selected_file in selected_traits.items()
                    ]
                }
                
                metadata_filename = f"metadata_{nft_number}.json"
                with open(metadata_filename, 'w') as f:
                    json.dump(metadata, f, indent=2)
                
                # Upload metadata to BunnyCDN
                metadata_upload_url = f"https://storage.bunnycdn.com/lfgo/minttest/{metadata_filename}"
                metadata_cdn_url = f"https://lfgo.b-cdn.net/minttest/{metadata_filename}"
                async with aiohttp.ClientSession() as session:
                    headers = {
                        "AccessKey": BUNNY_CDN_ACCESS_KEY,
                        "Content-Type": "application/json",
                    }
                    with open(metadata_filename, 'rb') as file:
                        await session.put(metadata_upload_url, headers=headers, data=file.read())
                
                # Clean up local files
                os.remove(combined_image_path)
                os.remove(metadata_filename)
                
                # Mint the NFT
                nft_id = await mint_nft_for_user(
                    metadata_cdn_url=metadata_cdn_url,
                    taxon=NFT_TAXON,
                    issuer=TOKEN_ISSUER_ADDRESS
                )
                
                if not nft_id:
                    await interaction.followup.send(
                        "Failed to mint NFT. Please try again later.",
                        ephemeral=True
                    )
                    return
                
                # Define the CDN URL for the image
                image_cdn_url = f"https://lfgo.b-cdn.net/minttest/{image_filename}"

                # Convert attributes list to dictionary format needed for database
                traits_dict = {}
                for trait in metadata["attributes"]:
                    trait_type = trait["trait_type"]
                    trait_value = trait["value"]
                    traits_dict[trait_type] = trait_value

                # Record the mint in the LFG table
                mint_record = record_nft_mint(
                    nft_number=nft_number,
                    nft_id=nft_id,
                    discord_id=str(interaction.user.id),
                    owner_address=user_data["address"],
                    metadata_url=metadata_cdn_url,
                    image_url=image_cdn_url,
                    traits=traits_dict
                )
                
                if not mint_record:
                    logging.error(f"Failed to record NFT #{nft_number} in database")
                
                # Get user's wallet address from the database
                user_data = get_user(interaction.user)
                if not user_data or not user_data.get("address"):
                    await interaction.followup.send(
                        "NFT minted but couldn't create offer: User wallet not found.",
                        ephemeral=True
                    )
                    return
                    
                # Create offer to the user
                logging.info(f"Creating NFT offer to wallet: {user_data['address']}")
                offer_id = await create_nft_offer(nft_id, user_data["address"])
                
                if not offer_id:
                    await interaction.followup.send(
                        f"NFT minted (ID: {nft_id}) but failed to create offer. Please contact an administrator.",
                        ephemeral=True
                    )
                    return

                # Generate XUMM QR code for NFT acceptance
                xumm_data = await generate_xumm_qr(offer_id)
                if not xumm_data:
                    await interaction.followup.send(
                        f"NFT minted and offer created (ID: {offer_id}) but failed to generate QR code. Please accept manually.",
                        ephemeral=True
                    )
                    return
                
                # Create embed for success message with QR code and NFT image
                embed = Embed(
                    title="🎨 NFT Minted Successfully!",
                    description=(
                        f"Your NFT has been minted and an offer has been created!\n\n"
                        f"**NFT Number:** #{nft_number}\n"
                        f"**To claim your NFT:**\n"
                        f"1. Scan the QR code with XUMM\n"
                        f"2. Review and accept the offer\n"
                        f"3. Your NFT will appear in your wallet!\n\n"
                        f"[Open in XUMM]({xumm_data['xumm_url']})"
                    ),
                    color=0x00ff00
                )
                
                # Add the NFT image as a thumbnail
                embed.set_thumbnail(url=image_cdn_url)
                
                # Add the XUMM QR code as the main image
                embed.set_image(url=xumm_data['qr_url'])
                
                embed.set_footer(text="Offer acceptance request expires in 24 hours")
                
                # Send the success message with both images
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            else:
                # Payment timed out
                logging.warning("Payment request timed out")
                await interaction.followup.send(
                    "Payment request timed out. Please try again.",
                    ephemeral=True
                )

        except Exception as e:
            logging.error("=== Error in mint_button handler ===")
            logging.error(f"Error type: {type(e).__name__}")
            logging.error(f"Error message: {str(e)}")
            logging.error(f"Full traceback: {traceback.format_exc()}")
            
            await interaction.followup.send(
                f"An error occurred during payment: {str(e)}",
                ephemeral=True
            )
    
    @discord.ui.button(label="🔗 Set LFGO Trustline", style=discord.ButtonStyle.secondary)
    async def trustline_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        
        # Get user's wallet address
        user_data = get_user(interaction.user)
        if not user_data or not user_data.get("address"):
            await safe_followup(
                interaction,
                "Please register your wallet first using /register",
                ephemeral=True
            )
            return

        try:
            # Create trustline request
            trustline_data = await create_trustline_request()
            if not trustline_data:
                await safe_followup(
                    interaction,
                    "Failed to create trustline request. Please try again.",
                    ephemeral=True
                )
                return

            # Create embed for trustline setup
            embed = Embed(
                title="🔗 Set Up LFGO Token Trustline",
                description=(
                    "Please set up a trustline for the LFGO token.\n\n"
                    "**Steps:**\n"
                    "1. Scan the QR code with your XUMM app\n"
                    "2. Review and approve the trustline\n"
                    "3. Wait for confirmation\n\n"
                    f"[Open in XUMM]({trustline_data['xumm_url']})"
                ),
                color=0x00ff00
            )
            
            embed.set_image(url=trustline_data['qr_url'])
            embed.set_footer(text="Trustline request expires in 5 minutes")
            
            # Send trustline request
            await safe_followup(interaction, embed=embed, ephemeral=True)

            # Trustline checking - keeping XUMM API for now since trustlines are one-time setup
            # Could be converted to static links if needed
            try:
                # Check trustline status using XUMM payload. The loop is
                # bounded by wall clock (not iteration count) and each XUMM
                # call gets its own timeout, so a slow/hanging API can never
                # stretch the handler past Discord's 15-minute webhook token.
                if 'uuid' in trustline_data:
                    # Poll the XUMM REST endpoint directly with a real network
                    # timeout: the SDK's payload.get exposes none, and a
                    # hanging call inside to_thread outlives any asyncio-level
                    # timeout and piles up worker threads.
                    status_headers = {
                        "accept": "application/json",
                        "X-API-Key": X_API_KEY,
                        "X-API-Secret": X_API_SECRET,
                    }
                    status_url = f"{XUMM_API_URL}/{trustline_data['uuid']}"
                    deadline = time.monotonic() + 300  # matches the 5-min payload expiry
                    while time.monotonic() < deadline:
                        try:
                            response = await asyncio.to_thread(
                                requests.get, status_url,
                                headers=status_headers, timeout=10)
                            meta = response.json().get('meta', {})
                            if meta.get('resolved'):
                                if meta.get('signed'):
                                    await safe_followup(
                                        interaction,
                                        "✅ Trustline set up successfully! You can now hold LFGO tokens.",
                                        ephemeral=True
                                    )
                                else:
                                    # resolved without signed = user declined:
                                    # terminal, so stop polling immediately
                                    await safe_followup(
                                        interaction,
                                        "Trustline request was declined or cancelled. "
                                        "Run it again whenever you're ready.",
                                        ephemeral=True
                                    )
                                return
                        except requests.Timeout:
                            logging.warning("XUMM payload status check timed out; retrying")
                        except Exception as e:
                            logging.error(f"Error checking trustline status: {e}")
                        await asyncio.sleep(5)

                # If we get here, request timed out
                await safe_followup(
                    interaction,
                    "Trustline request timed out. Please try again.",
                    ephemeral=True
                )
            except Exception as e:
                logging.error(f"Error in trustline checking: {e}")
                await safe_followup(
                    interaction,
                    "Error checking trustline status. Please try again.",
                    ephemeral=True
                )

        except Exception as e:
            error_msg = str(e)
            short_error = error_msg[:500] + "..." if len(error_msg) > 500 else error_msg
            logging.error(f"Error in trustline setup: {error_msg}")
            await safe_followup(
                interaction,
                f"An error occurred during trustline setup: {short_error}",
                ephemeral=True
            )

@tree.command(name="letsgo", description="Open the NFT minting interface")
async def mint(interaction: discord.Interaction):
    # Create the embed
    embed = Embed(
        title="🎮 LFG NFT Minting Interface",
        description=(
            "Welcome to the LFG NFT Minting Interface!\n\n"
            "**Requirements:**\n"
            "• XUMM Wallet\n"
            "• LFGO Tokens\n"
            "• XRPL Trustline\n\n"
            "Choose an action below:"
        ),
        color=0x00ff00  # Green color
    )
    
    # Add fields with information
    embed.add_field(
        name="🎨 Mint NFT",
        value="Create a unique NFT with random traits",
        inline=False
    )
    embed.add_field(
        name="🔗 Set LFGO Trustline",
        value="Set up your XRPL trustline for LFGO tokens",
        inline=False
    )
    embed.add_field(
        name="💰 Buy LFGO",
        value="Purchase LFGO tokens to mint NFTs",
        inline=False
    )
    
    # Add footer with additional info
    embed.set_footer(text="Buttons are active for 10 minutes • All actions are ephemeral")
    
    # Create the view with buttons
    view = MintView()
    
    # Send the embed with the view
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Add this after your mint command
@tree.command(name="register", description="Register your wallet")
async def register(interaction: discord.Interaction, wallet: str):
    """
    Registers a user's wallet address. Only requires the wallet address;
    Discord's ID and name are captured automatically.
    """
    discord_id = str(interaction.user.id)
    discord_name = str(interaction.user)
    
    success = register_user(discord_id, discord_name, wallet)
    if success:
        await interaction.response.send_message("Your wallet has been registered!", ephemeral=True)
    else:
        await interaction.response.send_message("There was an error registering your wallet.", ephemeral=True)

# Add cleanup handler
async def cleanup():
    """Cleanup tasks when bot is shutting down."""
    logging.info("Performing cleanup before shutdown...")
    try:
        if not bot.is_closed():
            await bot.close()
    except Exception as e:
        logging.error(f"Error during cleanup: {e}")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logging.info(f"Received signal {sig}, initiating shutdown...")
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup())
    loop.stop()

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

@bot.event
async def on_ready():
    create_users_table()  # Initialize the users table
    await tree.sync()     # Sync slash commands
    logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

async def burn_nft(nft_id: str) -> bool:
    """Burn an NFT using the issuer's seed"""
    try:
        logging.info(f"Attempting to burn NFT: {nft_id}")
        
        wallet = Wallet.from_seed(SEED)
        client = JsonRpcClient(JSON_RPC_URL)
        
        # Create NFTokenBurn transaction
        burn_tx = NFTokenBurn(
            account=wallet.classic_address,
            nftoken_id=nft_id
        )
        
        # Submit and wait for validation
        logging.info("Submitting burn transaction...")
        response = await asyncio.to_thread(
            submit_and_wait,
            burn_tx,
            client,
            wallet
        )
        
        if response.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
            logging.info(f"Successfully burned NFT: {nft_id}")
            return True
        else:
            logging.error(f"Failed to burn NFT. Response: {response.result}")
            return False
            
    except Exception as e:
        logging.error(f"Error burning NFT: {e}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        return False

class BurnNFTModal(Modal, title="Burn NFT"):
    nft_number = TextInput(
        label="Enter NFT Number to Burn",
        placeholder="e.g., 3535",
        required=True,
        min_length=1,
        max_length=10
    )
    
    reason = TextInput(
        label="Reason for Burning",
        placeholder="Enter reason for audit purposes",
        required=True,
        style=TextStyle.paragraph
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            nft_num = int(self.nft_number.value)
            
            # Get NFT details from database
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT nft_id, discord_id 
                FROM LFG 
                WHERE nft_number = ?
            ''', (nft_num,))
            
            result = cursor.fetchone()
            
            if not result or not result[0]:
                await interaction.followup.send(
                    f"❌ NFT #{nft_num} not found or hasn't been minted.",
                    ephemeral=True
                )
                return
                
            nft_id = result[0]
            discord_id = result[1]
            
            # Confirm burn with a button
            confirm_embed = Embed(
                title="🔥 Confirm NFT Burn",
                description=(
                    f"Are you sure you want to burn NFT #{nft_num}?\n\n"
                    f"**NFT ID:** {nft_id}\n"
                    f"**Owner:** <@{discord_id}>\n"
                    f"**Reason:** {self.reason.value}\n\n"
                    "⚠️ This action cannot be undone!"
                ),
                color=0xFF0000  # Red color for warning
            )
            
            # Create confirmation view
            view = BurnConfirmView(nft_num, nft_id, self.reason.value)
            await interaction.followup.send(
                embed=confirm_embed,
                view=view,
                ephemeral=True
            )
            
        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid NFT number.",
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Error in burn modal: {e}")
            await interaction.followup.send(
                "❌ Error processing burn request. Check logs for details.",
                ephemeral=True
            )
        finally:
            if 'conn' in locals():
                conn.close()

class BurnConfirmView(View):
    def __init__(self, nft_number: int, nft_id: str, reason: str):
        super().__init__(timeout=60)  # 1 minute timeout
        self.nft_number = nft_number
        self.nft_id = nft_id
        self.reason = reason
    
    @discord.ui.button(label="Confirm Burn", style=discord.ButtonStyle.danger)
    async def confirm_burn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Attempt to burn the NFT
            success = await burn_nft(self.nft_id)
            
            if success:
                conn = sqlite3.connect(DATABASE)
                cursor = conn.cursor()
                
                # Get all data from LFG table before deleting
                cursor.execute('''
                    SELECT nft_number, nft_id, discord_id, created_at
                    FROM LFG 
                    WHERE nft_number = ?
                ''', (self.nft_number,))
                nft_data = cursor.fetchone()
                
                # Insert into burned_nfts with original data
                cursor.execute('''
                    INSERT INTO burned_nfts (
                        nft_number, nft_id, discord_id, burned_by, 
                        reason, original_mint_time
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    nft_data[0],  # nft_number
                    nft_data[1],  # nft_id
                    nft_data[2],  # original discord_id
                    str(interaction.user.id),  # burned_by
                    self.reason,  # reason
                    nft_data[3]   # original_mint_time
                ))
                
                # Remove from LFG table
                cursor.execute('DELETE FROM LFG WHERE nft_number = ?', (self.nft_number,))
                
                conn.commit()
                
                await interaction.followup.send(
                    f"✅ Successfully burned NFT #{self.nft_number}",
                    ephemeral=True
                )
                
                # Log the burn in the admin channel
                try:
                    log_channel = interaction.guild.get_channel(ADMIN_LOG_CHANNEL_ID)
                    if log_channel:
                        log_embed = Embed(
                            title="🔥 NFT Burned",
                            description=(
                                f"**NFT #{self.nft_number}** was burned\n\n"
                                f"**Originally minted by:** <@{nft_data[2]}>\n"
                                f"**Burned by:** {interaction.user.mention}\n"
                                f"**Reason:** {self.reason}\n"
                                f"**NFT ID:** {self.nft_id}"
                            ),
                            color=0xFF0000
                        )
                        await log_channel.send(embed=log_embed)
                except Exception as e:
                    logging.error(f"Failed to send burn log: {e}")
                
            else:
                await interaction.followup.send(
                    f"❌ Failed to burn NFT #{self.nft_number}. Check logs for details.",
                    ephemeral=True
                )
                
        except Exception as e:
            logging.error(f"Error in burn confirmation: {e}")
            logging.error(f"Full traceback: {traceback.format_exc()}")
            await interaction.followup.send(
                "❌ Error processing burn confirmation. Check logs for details.",
                ephemeral=True
            )
        finally:
            if 'conn' in locals():
                conn.close()
            self.stop()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_burn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "❌ NFT burn cancelled.",
            ephemeral=True
        )
        self.stop()

# Add burn button to AdminView
class AdminView(View):
    def __init__(self):
        super().__init__(timeout=600)  # 10 minute timeout
        logging.info("Initializing AdminView")
    
    @discord.ui.button(label="📊 View Stats", style=discord.ButtonStyle.primary)
    async def stats_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        logging.info(f"Stats button pressed by {interaction.user}")
        
        try:
            conn = sqlite3.connect('lfg_nfts.db')
            cursor = conn.cursor()
            
            # Get total NFTs minted
            cursor.execute('SELECT COUNT(*) FROM LFG WHERE nft_id IS NOT NULL')
            total_minted = cursor.fetchone()[0]
            
            # Get total unique users
            cursor.execute('SELECT COUNT(DISTINCT discord_id) FROM LFG WHERE discord_id IS NOT NULL')
            unique_users = cursor.fetchone()[0]
            
            # Get recent mints
            cursor.execute('''
                SELECT nft_number, discord_id, created_at 
                FROM LFG 
                WHERE nft_id IS NOT NULL 
                ORDER BY created_at DESC 
                LIMIT 5
            ''')
            recent_mints = cursor.fetchall()
            
            # Get burned NFTs count
            cursor.execute('SELECT COUNT(*) FROM burned_nfts')
            burned_count = cursor.fetchone()[0]
            
            stats_embed = Embed(
                title="📊 Minting Statistics",
                color=0x9C84EF
            )
            
            stats_embed.add_field(
                name="Total NFTs Minted",
                value=str(total_minted),
                inline=True
            )
            
            stats_embed.add_field(
                name="Unique Users",
                value=str(unique_users),
                inline=True
            )
            
            stats_embed.add_field(
                name="Burned NFTs",
                value=str(burned_count),
                inline=True
            )
            
            if recent_mints:
                recent_mints_text = "\n".join(
                    f"#{num} by <@{uid}> on {date[:10]}"
                    for num, uid, date in recent_mints
                )
                stats_embed.add_field(
                    name="Recent Mints",
                    value=recent_mints_text,
                    inline=False
                )
            
            await interaction.followup.send(embed=stats_embed, ephemeral=True)
            
        except Exception as e:
            logging.error(f"Error in stats button: {e}")
            await interaction.followup.send(
                "❌ Error retrieving statistics. Check logs for details.",
                ephemeral=True
            )
        finally:
            if 'conn' in locals():
                conn.close()
    
    @discord.ui.button(label="🔍 Lookup NFT", style=discord.ButtonStyle.primary)
    async def lookup_button(self, interaction: discord.Interaction, button: Button):
        logging.info(f"Lookup button pressed by {interaction.user}")
        await interaction.response.send_modal(NFTLookupModal())
    
    @discord.ui.button(label="🔥 Burn NFT", style=discord.ButtonStyle.danger)
    async def burn_button(self, interaction: discord.Interaction, button: Button):
        logging.info(f"Burn button pressed by {interaction.user}")
        await interaction.response.send_modal(BurnNFTModal())

@tree.command(
    name="admin",
    description="Admin control panel for NFT management"
)
@app_commands.checks.has_permissions(administrator=True)  # Add explicit permission check
async def admin_command(interaction: discord.Interaction):
    """Admin control panel for NFT management"""
    
    logging.info(f"Admin command triggered by {interaction.user}")
    
    # Create the admin panel embed
    embed = Embed(
        title="🔧 Admin Control Panel",
        description=(
            "Welcome to the NFT Admin Panel!\n\n"
            "**Available Actions:**\n"
            "• 📊 View Stats - Check minting statistics\n"
            "• 🔍 Lookup NFT - View details of specific NFT\n"
            "• 🔥 Burn NFT - Burn a specific NFT"
        ),
        color=0x9C84EF
    )
    
    embed.set_footer(text="Admin panel will timeout after 10 minutes")
    
    # Create view with admin buttons
    view = AdminView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class NFTLookupModal(Modal, title="NFT Lookup"):
    nft_number = TextInput(
        label="Enter NFT Number",
        placeholder="e.g., 3535",
        required=True,
        min_length=1,
        max_length=10
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        logging.info(f"NFT lookup requested for number {self.nft_number.value} by {interaction.user}")
        
        try:
            nft_num = int(self.nft_number.value)
            conn = sqlite3.connect('lfg_nfts.db')
            cursor = conn.cursor()
            
            # Check main NFT table
            cursor.execute('''
                SELECT nft_number, nft_id, discord_id, created_at
                FROM LFG 
                WHERE nft_number = ?
            ''', (nft_num,))
            
            result = cursor.fetchone()
            
            # Check if NFT was burned
            cursor.execute('''
                SELECT burned_by, reason, burned_at
                FROM burned_nfts 
                WHERE nft_number = ?
            ''', (nft_num,))
            
            burn_info = cursor.fetchone()
            
            if result:
                nft_embed = Embed(
                    title=f"🔍 NFT #{result[0]} Details",
                    color=0x9C84EF
                )
                
                nft_embed.add_field(
                    name="NFT ID",
                    value=result[1] or "Not minted",
                    inline=True
                )
                
                if result[2]:  # If discord_id exists
                    nft_embed.add_field(
                        name="Minted By",
                        value=f"<@{result[2]}>",
                        inline=True
                    )
                
                nft_embed.add_field(
                    name="Minted On",
                    value=result[3][:10] if result[3] else "N/A",
                    inline=True
                )
                
                # Add burn information if it exists
                if burn_info:
                    nft_embed.add_field(
                        name="🔥 Burn Status",
                        value=(
                            f"Burned by: <@{burn_info[0]}>\n"
                            f"Reason: {burn_info[1]}\n"
                            f"Date: {burn_info[2][:10]}"
                        ),
                        inline=False
                    )
                
                await interaction.followup.send(embed=nft_embed, ephemeral=True)
            else:
                await interaction.followup.send(
                    f"❌ NFT #{nft_num} not found in database.",
                    ephemeral=True
                )
                
        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid NFT number.",
                ephemeral=True
            )
        except Exception as e:
            logging.error(f"Error in NFT lookup: {e}")
            await interaction.followup.send(
                "❌ Error looking up NFT. Check logs for details.",
                ephemeral=True
            )
        finally:
            if 'conn' in locals():
                conn.close()

# Main execution
if __name__ == "__main__":
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        logging.error(f"Failed to start bot: {e}")
