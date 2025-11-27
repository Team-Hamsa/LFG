"""
Discord bot for minting NFTs on XRPL Testnet.

This version consolidates environment variables, fixes several issues
identified in the original code, and ensures the NFT metadata uses the
public BunnyCDN pull zone.  It retains ffmpeg for image composition
because animated layers (e.g. GIF/MP4) are supported.

Configuration is read from environment variables.  Required variables:

* XUMM_API_KEY, XUMM_API_SECRET – credentials for the Xumm API.
* DISCORD_BOT_TOKEN – token for the Discord bot.
* ADMIN_LOG_CHANNEL_ID – Discord channel ID (integer) for admin logs.
* SEED – XRPL seed for signing transactions.
* TOKEN_ISSUER_ADDRESS – issuing account of the fungible token.
* TOKEN_CURRENCY_HEX – hex code of the fungible token currency.
* BUNNY_CDN_ACCESS_KEY – BunnyCDN storage API access key.
* BUNNY_CDN_STORAGE_ZONE – BunnyCDN storage zone name.
* BUNNY_PULL_ZONE – BunnyCDN pull zone domain (e.g. nft.letseffinggo.com).

Optional variables allow customising NFT collection, taxon, and timeouts.
"""

import os
import random
import json
import asyncio
import logging
import re
import signal
import sqlite3
import traceback
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands, TextStyle, Embed
from discord.ui import Button, View, Modal, TextInput
from discord.ext import commands

from dotenv import load_dotenv
import aiohttp
import requests

from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet
from xrpl.models.requests import Tx
from xrpl.models.transactions import (
    NFTokenMint,
    NFTokenBurn,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
)
from xrpl.transaction import submit_and_wait

from xumm import XummSdk

from db_helpers import get_next_nft_number, record_nft_mint
from user_db import register_user, create_users_table, get_user as get_user_from_db
from ts_helpers import mint_nft as helper_mint_nft  # imported for completeness

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
load_dotenv()

XUMM_API_KEY = os.getenv("XUMM_API_KEY")
XUMM_API_SECRET = os.getenv("XUMM_API_SECRET")
if not XUMM_API_KEY or not XUMM_API_SECRET:
    raise ValueError("XUMM_API_KEY and XUMM_API_SECRET must be set in the environment")

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN must be set in the environment")

ADMIN_LOG_CHANNEL_ID_RAW = os.getenv("ADMIN_LOG_CHANNEL_ID")
if not ADMIN_LOG_CHANNEL_ID_RAW:
    raise ValueError("ADMIN_LOG_CHANNEL_ID must be set in the environment")
try:
    ADMIN_LOG_CHANNEL_ID = int(ADMIN_LOG_CHANNEL_ID_RAW)
except ValueError as exc:
    raise ValueError("ADMIN_LOG_CHANNEL_ID must be a valid integer ID") from exc

SEED = os.getenv("SEED")
if not SEED:
    raise ValueError("SEED must be set in the environment")

TOKEN_ISSUER_ADDRESS = os.getenv("TOKEN_ISSUER_ADDRESS")
TOKEN_CURRENCY_HEX = os.getenv("TOKEN_CURRENCY_HEX")
if not TOKEN_ISSUER_ADDRESS or not TOKEN_CURRENCY_HEX:
    raise ValueError("TOKEN_ISSUER_ADDRESS and TOKEN_CURRENCY_HEX must be set in the environment")

NFT_TAXON = int(os.getenv("NFT_TAXON", "0"))
TOKEN_TRUSTLINE_LIMIT = os.getenv("TOKEN_TRUSTLINE_LIMIT", "1000")

BUNNY_CDN_ACCESS_KEY = os.getenv("BUNNY_CDN_ACCESS_KEY")
BUNNY_CDN_STORAGE_ZONE = os.getenv("BUNNY_CDN_STORAGE_ZONE")
BUNNY_CDN_FOLDER = os.getenv("BUNNY_CDN_FOLDER", "minttest")
BUNNY_PULL_ZONE = os.getenv("BUNNY_PULL_ZONE")  # e.g. nft.letseffinggo.com
if not all([BUNNY_CDN_ACCESS_KEY, BUNNY_CDN_STORAGE_ZONE, BUNNY_PULL_ZONE]):
    raise ValueError(
        "BUNNY_CDN_ACCESS_KEY, BUNNY_CDN_STORAGE_ZONE and BUNNY_PULL_ZONE must be set in the environment"
    )

NFT_COLLECTION_NAME = os.getenv("NFT_COLLECTION_NAME", "Let's Effing Go!")
NFT_COLLECTION_FAMILY = os.getenv("NFT_COLLECTION_FAMILY", "Test")
NFT_DESCRIPTION = os.getenv("NFT_DESCRIPTION", "Test")
NFT_TRANSFER_FEE = int(os.getenv("NFT_TRANSFER_FEE", "7000"))
NFT_FLAGS = int(os.getenv("NFT_FLAGS", "9"))
NFT_SCHEMA_URL = os.getenv(
    "NFT_SCHEMA_URL",
    "ipfs://QmNpi8rcXEkohca8iXu7zysKKSJYqCvBJn3xJwga8jXqWU",
)

EXTERNAL_WEBSITE_URL = os.getenv("EXTERNAL_WEBSITE_URL", "https://letseffinggo.com")

RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))
VIEW_TIMEOUT = int(os.getenv("VIEW_TIMEOUT", "600"))

TRAIT_LAYERS_DIR = "trait_layers"
DATABASE = "lfg_nfts.db"

# XRPL Testnet endpoint
JSON_RPC_URL = "https://s.altnet.rippletest.net:51234/"

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

# ---------------------------------------------------------------------------
# Discord bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True


class RetryBot(commands.Bot):
    """Bot subclass that attempts reconnects with backoff."""

    async def start(self, *args, **kwargs):
        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                if attempt > 0:
                    jitter = random.uniform(0, 2)
                    delay = (RETRY_BASE_DELAY * (2 ** attempt)) + jitter
                    logging.info(
                        "Retry attempt %s/%s after %.2f s delay", attempt + 1, RETRY_MAX_ATTEMPTS, delay
                    )
                    await asyncio.sleep(delay)
                await super().start(*args, **kwargs)
                return
            except Exception as exc:
                logging.error("Connection attempt %s failed: %s", attempt + 1, exc)
                if attempt == RETRY_MAX_ATTEMPTS - 1:
                    raise


bot = RetryBot(command_prefix="!", intents=intents)
tree = bot.tree

# Initialise XUMM SDK
sdk = XummSdk(XUMM_API_KEY, XUMM_API_SECRET)

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def convert_str_to_hex(string: str) -> str:
    """Convert a UTF‑8 string to uppercase hex for XRPL URIs."""
    return string.encode("utf-8").hex().upper()


def format_trait_name(text: str) -> str:
    """Format trait names by removing numeric prefixes and capitalising words."""
    clean_text = re.sub(r"^\d+\s*", "", text).strip()
    return " ".join(word.capitalize() for word in clean_text.split())


def get_trait_files(trait_layer_dir: str) -> List[str]:
    """Return all valid image files in a directory."""
    SUPPORTED = {".png", ".jpg", ".jpeg", ".gif", ".mp4"}
    return [
        f
        for f in os.listdir(trait_layer_dir)
        if os.path.isfile(os.path.join(trait_layer_dir, f))
        and not f.startswith(".")
        and os.path.splitext(f)[1].lower() in SUPPORTED
    ]


def get_random_trait(trait_layer_dir: str) -> str:
    """Randomly select a valid trait image from a directory."""
    files = get_trait_files(trait_layer_dir)
    if not files:
        raise ValueError(f"No valid image files found in {trait_layer_dir}")
    return random.choice(files)


def get_sorted_trait_layers(trait_layers_dir: str) -> List[str]:
    """Return a list of trait layer directories in the order they should be applied."""
    directories = [
        d
        for d in os.listdir(trait_layers_dir)
        if os.path.isdir(os.path.join(trait_layers_dir, d))
    ]
    has_numeric = any(re.match(r"^\d+", d) for d in directories)
    if has_numeric:
        def sort_key(name: str) -> int:
            match = re.match(r"^(\d+)", name)
            return int(match.group(1)) if match else float('inf')
        return sorted(directories, key=sort_key)
    TRAIT_ORDER = [
        "background",
        "body",
        "clothing",
        "mouth",
        "eyebrows",
        "eyes",
        "hat:hair",
        "accessory",
    ]
    return sorted(
        directories,
        key=lambda d: (TRAIT_ORDER.index(d.lower()) if d.lower() in TRAIT_ORDER else float('inf'), d),
    )


async def mint_nft_for_user(metadata_url: str, taxon: int, issuer: str) -> Optional[str]:
    """Mint an NFT and return its ID, or None on failure."""
    try:
        wallet = Wallet.from_seed(SEED)
        client = JsonRpcClient(JSON_RPC_URL)
        # Prepare NFTokenMint transaction
        if issuer == wallet.classic_address:
            tx = NFTokenMint(
                account=wallet.classic_address,
                uri=convert_str_to_hex(metadata_url),
                nftoken_taxon=taxon,
                transfer_fee=NFT_TRANSFER_FEE,
                flags=NFT_FLAGS,
            )
        else:
            tx = NFTokenMint(
                account=wallet.classic_address,
                uri=convert_str_to_hex(metadata_url),
                nftoken_taxon=taxon,
                issuer=issuer,
                transfer_fee=NFT_TRANSFER_FEE,
                flags=NFT_FLAGS,
            )
        # Submit transaction with retries
        for attempt in range(1, 6):
            try:
                resp = await asyncio.to_thread(submit_and_wait, tx, client, wallet)
                hash_txn = resp.result.get("hash")
                break
            except Exception as exc:
                logging.error("Mint attempt %s failed: %s", attempt, exc)
                if attempt == 5:
                    return None
                await asyncio.sleep(5)
        # Check transaction status
        for check in range(1, 6):
            try:
                txn = await asyncio.to_thread(client.request, Tx(transaction=hash_txn))
                res = txn.result
                if res.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
                    nft_id = res.get("meta", {}).get("nftoken_id")
                    if nft_id:
                        return nft_id
                break
            except Exception as exc:
                logging.error("Status check %s failed: %s", check, exc)
                if check == 5:
                    return None
                await asyncio.sleep(5)
        return None
    except Exception as exc:
        logging.error("Error in mint_nft_for_user: %s", exc)
        return None


async def create_nft_offer(nft_id: str, destination: str) -> Optional[str]:
    """Create a sell offer for zero price targeted at `destination`."""
    try:
        client = JsonRpcClient(JSON_RPC_URL)
        wallet = Wallet.from_seed(SEED)
        tx = NFTokenCreateOffer(
            account=wallet.classic_address,
            destination=destination,
            amount="0",
            nftoken_id=nft_id,
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        )
        response = await asyncio.to_thread(submit_and_wait, tx, client, wallet)
        hash_txn = response.result.get("hash")
        # Retrieve offer id
        for attempt in range(3):
            try:
                txn = await asyncio.to_thread(client.request, Tx(transaction=hash_txn))
                res = txn.result
                if res.get("meta", {}).get("TransactionResult") == "tesSUCCESS":
                    offer_id = res.get("meta", {}).get("offer_id")
                    if offer_id:
                        return offer_id
                await asyncio.sleep(5)
            except Exception:
                await asyncio.sleep(5)
        return None
    except Exception as exc:
        logging.error("Error creating NFT offer: %s", exc)
        return None


async def generate_xumm_qr(offer_id: str) -> Optional[Dict[str, str]]:
    """Generate a XUMM payload for accepting an NFT offer."""
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": XUMM_API_KEY,
        "X-API-Secret": XUMM_API_SECRET,
    }
    payload = {
        "txjson": {
            "TransactionType": "NFTokenAcceptOffer",
            "NFTokenSellOffer": offer_id,
        }
    }
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                requests.post,
                "https://xumm.app/api/v1/platform/payload",
                json=payload,
                headers=headers,
                timeout=30,
            ),
            timeout=35.0,
        )
        data = resp.json()
        return {
            "qr_url": data["refs"]["qr_png"],
            "xumm_url": data["next"]["always"],
            "uuid": data["uuid"],
        }
    except asyncio.TimeoutError:
        logging.error("Timeout generating XUMM QR: Request took longer than 35 seconds")
        return None
    except Exception as exc:
        logging.error("Error generating XUMM QR: %s", exc)
        return None


async def create_payment_request(destination: str) -> Optional[Dict[str, str]]:
    """Create a XUMM payment request for 1 unit of the token to `destination`."""
    logging.info("=== Starting create_payment_request ===")
    logging.info(f"Destination address: {destination}")
    logging.info(f"TOKEN_ISSUER_ADDRESS: {TOKEN_ISSUER_ADDRESS}")
    logging.info(f"TOKEN_CURRENCY_HEX: {TOKEN_CURRENCY_HEX}")
    
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": XUMM_API_KEY,
        "X-API-Secret": XUMM_API_SECRET,
    }
    logging.info("Headers prepared (keys hidden)")
    
    tx_json = {
        "TransactionType": "Payment",
        "Destination": destination,
        "Amount": {
            "currency": TOKEN_CURRENCY_HEX,
            "value": "1",
            "issuer": TOKEN_ISSUER_ADDRESS,
        },
    }
    logging.info(f"Payment transaction JSON prepared: {json.dumps(tx_json, indent=2)}")
    
    payload = {
        "txjson": tx_json,
        "options": {
            "expire": 5,
            "return_url": {"web": EXTERNAL_WEBSITE_URL},
        },
    }
    logging.info(f"Full payload prepared: {json.dumps(payload, indent=2)}")
    
    try:
        logging.info(f"Sending request to XUMM API: https://xumm.app/api/v1/platform/payload")
        # Use asyncio.wait_for to enforce timeout since asyncio.to_thread doesn't respect requests timeout
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                requests.post,
                "https://xumm.app/api/v1/platform/payload",
                json=payload,
                headers=headers,
                timeout=30,
            ),
            timeout=35.0,  # Slightly longer than requests timeout to allow it to fail gracefully
        )
        logging.info(f"XUMM API response status: {resp.status_code}")
        
        data = resp.json()
        logging.info(f"XUMM API response data: {json.dumps(data, indent=2)}")
        
        result = {
            "qr_url": data["refs"]["qr_png"],
            "xumm_url": data["next"]["always"],
            "uuid": data["uuid"],
        }
        logging.info(f"Successfully created payment request: {json.dumps(result, indent=2)}")
        return result
    except asyncio.TimeoutError:
        logging.error("=== Timeout in create_payment_request ===")
        logging.error("XUMM API request took longer than 35 seconds")
        return None
    except Exception as exc:
        logging.error("=== Error in create_payment_request ===")
        logging.error(f"Error type: {type(exc).__name__}")
        logging.error(f"Error message: {str(exc)}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        return None


async def check_payment_status(uuid: str) -> bool:
    """Return True if the XUMM payload has been signed and resolved."""
    try:
        logging.info(f"Checking payment status for UUID: {uuid}")
        resp = await asyncio.to_thread(sdk.payload.get, uuid)
        data = resp.to_dict().get("meta", {})
        signed = data.get("signed", False)
        resolved = data.get("resolved", False)
        logging.info(f"Payment status - signed: {signed}, resolved: {resolved}")
        return bool(signed and resolved)
    except Exception as exc:
        logging.error(f"Error checking payment status: {exc}")
        logging.error(f"Full traceback: {traceback.format_exc()}")
        return False


async def create_trustline_request() -> Optional[Dict[str, str]]:
    """Create a XUMM trustline request for the token."""
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": XUMM_API_KEY,
        "X-API-Secret": XUMM_API_SECRET,
    }
    tx_json = {
        "TransactionType": "TrustSet",
        "Flags": 131072,  # tfSetNoRipple
        "LimitAmount": {
            "currency": TOKEN_CURRENCY_HEX,
            "issuer": TOKEN_ISSUER_ADDRESS,
            "value": TOKEN_TRUSTLINE_LIMIT,
        },
    }
    payload = {
        "txjson": tx_json,
        "options": {
            "expire": 5,
            "return_url": {"web": EXTERNAL_WEBSITE_URL},
        },
    }
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                requests.post,
                "https://xumm.app/api/v1/platform/payload",
                json=payload,
                headers=headers,
                timeout=30,
            ),
            timeout=35.0,
        )
        data = resp.json()
        return {
            "qr_url": data["refs"]["qr_png"],
            "xumm_url": data["next"]["always"],
            "uuid": data["uuid"],
        }
    except asyncio.TimeoutError:
        logging.error("Timeout generating trustline request: Request took longer than 35 seconds")
        return None
    except Exception as exc:
        logging.error("Error generating trustline request: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Views and slash commands
# ---------------------------------------------------------------------------
class MintView(View):
    def __init__(self) -> None:
        super().__init__(timeout=VIEW_TIMEOUT)
        # Add a link button for buying tokens
        buy_button = Button(
            label="💰 Buy Token",
            style=discord.ButtonStyle.success,
            url=EXTERNAL_WEBSITE_URL,
        )
        self.add_item(buy_button)

    @discord.ui.button(label="🎨 Mint NFT", style=discord.ButtonStyle.primary)
    async def mint_button(self, interaction: discord.Interaction, button: Button) -> None:
        logging.info("=== Mint button pressed ===")
        logging.info(f"User ID: {interaction.user.id}")
        logging.info(f"Username: {interaction.user.name}")
        
        try:
            await interaction.response.defer(ephemeral=True)
            logging.info("Interaction deferred")
            
            user_data = get_user_from_db(str(interaction.user.id))
            logging.info(f"Retrieved user data: {json.dumps(user_data, indent=2) if user_data else 'None'}")
            
            if not user_data or not user_data.get("address"):
                logging.warning("No wallet address found for user")
                await interaction.followup.send("Please register your wallet first using /register", ephemeral=True)
                return
            
            logging.info("=== Starting payment process ===")
            logging.info(f"Using TOKEN_ISSUER_ADDRESS: {TOKEN_ISSUER_ADDRESS}")
            
            # Create payment request
            payment_data = await create_payment_request(TOKEN_ISSUER_ADDRESS)
            if not payment_data:
                logging.error("Failed to create payment request")
                await interaction.followup.send("Failed to create payment request. Please try again.", ephemeral=True)
                return
            
            logging.info(f"Payment request created: {json.dumps(payment_data, indent=2)}")
            
            # Present payment embed
            pay_embed = Embed(
                title="💰 Token Payment Required",
                description=(
                    "Please pay 1 token to mint your NFT.\n\n"
                    "**Steps:**\n"
                    "1. Scan the QR code with your XUMM app\n"
                    "2. Approve the payment\n"
                    "3. Wait for confirmation\n\n"
                    f"[Open in XUMM]({payment_data['xumm_url']})"
                ),
                color=0x00FF00,
            )
            pay_embed.set_image(url=payment_data["qr_url"])
            pay_embed.set_footer(text="Payment request expires in 5 minutes")
            
            logging.info("Payment embed created, sending to user")
            await interaction.followup.send(embed=pay_embed, ephemeral=True)
            
            # Wait up to 5 minutes for payment
            logging.info("Starting payment status check loop")
            for attempt in range(60):
                logging.info(f"Payment status check attempt {attempt + 1}/60")
                if await check_payment_status(payment_data["uuid"]):
                    logging.info("Payment confirmed! Proceeding with NFT mint")
                    await interaction.followup.send("✅ Payment received! Starting NFT mint process...", ephemeral=True)
                    break
                await asyncio.sleep(5)
            else:
                logging.warning("Payment request timed out after 60 checks (5 minutes)")
                await interaction.followup.send("Payment request timed out. Please try again.", ephemeral=True)
                return
            
            # Determine next NFT number
            nft_number = get_next_nft_number()
            logging.info(f"Generated NFT number: {nft_number}")
            
            # Select traits and compose image with ffmpeg
            selected_traits: Dict[str, str] = {}
            input_images: List[str] = []
            for layer in get_sorted_trait_layers(TRAIT_LAYERS_DIR):
                layer_dir = os.path.join(TRAIT_LAYERS_DIR, layer)
                if os.path.isdir(layer_dir):
                    files = get_trait_files(layer_dir)
                    if files:
                        chosen = random.choice(files)
                        selected_traits[layer] = chosen
                        input_images.append(os.path.join(layer_dir, chosen))
            combined_image_path = f"output_nft_{nft_number}.png"
            if input_images:
                try:
                    import ffmpeg  # import locally to ensure optional dependency
                    stream = ffmpeg.input(input_images[0])
                    for extra in input_images[1:]:
                        stream = ffmpeg.overlay(stream, ffmpeg.input(extra))
                    stream = ffmpeg.output(stream, combined_image_path, vframes=1, loglevel="error")
                    ffmpeg.run(stream, overwrite_output=True)
                except ffmpeg.Error as exc:
                    err_msg = exc.stderr.decode() if exc.stderr else str(exc)
                    logging.error("FFmpeg error: %s", err_msg)
                    await interaction.followup.send("Failed to generate NFT image. Please contact an administrator.", ephemeral=True)
                    return
            
            # Upload image to BunnyCDN
            image_filename = f"lfg_{nft_number}.png"
            storage_url = f"https://storage.bunnycdn.com/{BUNNY_CDN_STORAGE_ZONE}/{BUNNY_CDN_FOLDER}/{image_filename}"
            public_url = f"https://{BUNNY_PULL_ZONE}/{BUNNY_CDN_FOLDER}/{image_filename}"
            async with aiohttp.ClientSession() as session:
                headers = {"AccessKey": BUNNY_CDN_ACCESS_KEY, "Content-Type": "image/png"}
                with open(combined_image_path, "rb") as f:
                    await session.put(storage_url, headers=headers, data=f.read())
            
            # Create metadata and upload
            attributes = [
                {"trait_type": layer, "value": format_trait_name(os.path.splitext(file)[0])}
                for layer, file in selected_traits.items()
            ]
            metadata = {
                "schema": NFT_SCHEMA_URL,
                "name": f"{NFT_COLLECTION_NAME} #{nft_number}",
                "description": NFT_DESCRIPTION,
                "image": public_url,
                "video": "",
                "external_link": EXTERNAL_WEBSITE_URL,
                "collection": {
                    "name": NFT_COLLECTION_NAME,
                    "family": NFT_COLLECTION_FAMILY,
                },
                "edition": nft_number,
                "attributes": attributes,
            }
            metadata_filename = f"metadata_{nft_number}.json"
            with open(metadata_filename, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
            storage_meta_url = f"https://storage.bunnycdn.com/{BUNNY_CDN_STORAGE_ZONE}/{BUNNY_CDN_FOLDER}/{metadata_filename}"
            public_meta_url = f"https://{BUNNY_PULL_ZONE}/{BUNNY_CDN_FOLDER}/{metadata_filename}"
            async with aiohttp.ClientSession() as session:
                headers = {"AccessKey": BUNNY_CDN_ACCESS_KEY, "Content-Type": "application/json"}
                with open(metadata_filename, "rb") as f:
                    await session.put(storage_meta_url, headers=headers, data=f.read())
            
            # Remove local files
            try:
                os.remove(combined_image_path)
                os.remove(metadata_filename)
            except OSError:
                pass
            
            # Mint NFT using public metadata URL
            nft_id = await mint_nft_for_user(public_meta_url, NFT_TAXON, TOKEN_ISSUER_ADDRESS)
            if not nft_id:
                await interaction.followup.send("Failed to mint NFT. Please try again later.", ephemeral=True)
                return
            
            # Record mint
            traits_dict = {t["trait_type"]: t["value"] for t in attributes}
            record_nft_mint(
                nft_number=nft_number,
                nft_id=nft_id,
                discord_id=str(interaction.user.id),
                owner_address=user_data["address"],
                metadata_url=public_meta_url,
                image_url=public_url,
                traits=traits_dict,
            )
            
            # Create offer
            logging.info(f"Creating NFT offer to wallet: {user_data['address']}")
            offer_id = await create_nft_offer(nft_id, user_data["address"])
            if not offer_id:
                await interaction.followup.send(
                    f"NFT minted (ID: {nft_id}) but failed to create offer. Please contact an administrator.",
                    ephemeral=True,
                )
                return
            
            # Generate XUMM QR
            xumm_data = await generate_xumm_qr(offer_id)
            if not xumm_data:
                await interaction.followup.send(
                    f"NFT minted and offer created (ID: {offer_id}) but failed to generate QR code. Please accept manually.",
                    ephemeral=True,
                )
                return
            
            # Success embed
            success_embed = Embed(
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
                color=0x00FF00,
            )
            success_embed.set_thumbnail(url=public_url)
            success_embed.set_image(url=xumm_data["qr_url"])
            success_embed.set_footer(text="Offer acceptance request expires in 24 hours")
            await interaction.followup.send(embed=success_embed, ephemeral=True)
        except Exception as exc:
            logging.error("=== Error in mint_button handler ===")
            logging.error(f"Error type: {type(exc).__name__}")
            logging.error(f"Error message: {str(exc)}")
            logging.error(f"Full traceback: {traceback.format_exc()}")
            try:
                await interaction.followup.send(
                    f"An error occurred during minting: {str(exc)}",
                    ephemeral=True
                )
            except Exception:
                pass

    @discord.ui.button(label="🔗 Set LFGO Trustline", style=discord.ButtonStyle.secondary)
    async def trustline_button(self, interaction: discord.Interaction, button: Button) -> None:
        logging.info("=== Trustline button pressed ===")
        logging.info(f"User ID: {interaction.user.id}")
        logging.info(f"Username: {interaction.user.name}")
        
        try:
            await interaction.response.defer(ephemeral=True)
            logging.info("Interaction deferred")
            
            user_data = get_user_from_db(str(interaction.user.id))
            logging.info(f"Retrieved user data: {json.dumps(user_data, indent=2) if user_data else 'None'}")
            
            if not user_data or not user_data.get("address"):
                logging.warning("No wallet address found for user")
                await interaction.followup.send("Please register your wallet first using /register", ephemeral=True)
                return
            
            logging.info("Creating trustline request")
            trustline_data = await create_trustline_request()
            if not trustline_data:
                logging.error("Failed to create trustline request")
                await interaction.followup.send("Failed to create trustline request. Please try again.", ephemeral=True)
                return
            
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
                color=0x00FF00,
            )
            embed.set_image(url=trustline_data["qr_url"])
            embed.set_footer(text="Trustline request expires in 5 minutes")
            
            logging.info("Sending trustline embed to user")
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            logging.info("Starting trustline status check loop")
            for _ in range(60):
                if await check_payment_status(trustline_data["uuid"]):
                    logging.info("Trustline confirmed!")
                    await interaction.followup.send(
                        "✅ Trustline set up successfully! You can now hold LFGO tokens.",
                        ephemeral=True,
                    )
                    return
                await asyncio.sleep(5)
            
            logging.warning("Trustline request timed out")
            await interaction.followup.send(
                "Trustline request timed out. Please try again.",
                ephemeral=True,
            )
        except Exception as exc:
            logging.error("=== Error in trustline_button handler ===")
            logging.error(f"Error type: {type(exc).__name__}")
            logging.error(f"Error message: {str(exc)}")
            logging.error(f"Full traceback: {traceback.format_exc()}")
            try:
                error_msg = str(exc)
                short_error = error_msg[:500] + "..." if len(error_msg) > 500 else error_msg
                await interaction.followup.send(
                    f"An error occurred during trustline setup: {short_error}",
                    ephemeral=True,
                )
            except Exception:
                pass


@tree.command(name="letsgo", description="Open the NFT minting interface")
async def mint(interaction: discord.Interaction) -> None:
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
        color=0x00FF00,
    )
    embed.add_field(name="🎨 Mint NFT", value="Create a unique NFT with random traits", inline=False)
    embed.add_field(name="🔗 Set LFGO Trustline", value="Set up your XRPL trustline for LFGO tokens", inline=False)
    embed.add_field(name="💰 Buy LFGO", value="Purchase LFGO tokens to mint NFTs", inline=False)
    embed.set_footer(text="Buttons are active for 10 minutes • All actions are ephemeral")
    await interaction.response.send_message(embed=embed, view=MintView(),
                                            ephemeral=True)


@tree.command(name="register", description="Register your wallet")
async def register(interaction: discord.Interaction, wallet: str) -> None:
    discord_id = str(interaction.user.id)
    discord_name = str(interaction.user)
    wallet = wallet.strip()
    if not (wallet.startswith("r") and 25 < len(wallet) < 45):
        await interaction.response.send_message(
            "Invalid XRPL wallet address. Please enter a valid address.",
            ephemeral=True,
        )
        return
    success = register_user(discord_id, discord_name, wallet)
    if success:
        await interaction.response.send_message("Your wallet has been registered!", ephemeral=True)
    else:
        await interaction.response.send_message(
            "There was an error registering your wallet.",
            ephemeral=True,
        )


class BurnNFTModal(Modal, title="Burn NFT"):
    nft_number = TextInput(
        label="Enter NFT Number to Burn",
        placeholder="e.g., 3535",
        required=True,
        min_length=1,
        max_length=10,
    )
    reason = TextInput(
        label="Reason for Burning",
        placeholder="Enter reason for audit purposes",
        required=True,
        style=TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            nft_num = int(self.nft_number.value)
        except ValueError:
            await interaction.followup.send(
                "❌ Please enter a valid NFT number.",
                ephemeral=True,
            )
            return
        try:
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()
            cursor.execute("SELECT nft_id, discord_id FROM LFG WHERE nft_number = ?", (nft_num,))
            result = cursor.fetchone()
            if not result or not result[0]:
                # The specified NFT number does not exist or was not minted
                await interaction.followup.send(
                    f"❌ NFT #{nft_num} not found or hasn't been minted.",
                    ephemeral=True,
                )
                return
            nft_id, owner_discord_id = result
            confirm_embed = Embed(
                title="🔥 Confirm NFT Burn",
                description=(
                    f"Are you sure you want to burn NFT #{nft_num}?\n\n"
                    f"**NFT ID:** {nft_id}\n"
                    f"**Owner:** <@{owner_discord_id}>\n"
                    f"**Reason:** {self.reason.value}\n\n"
                    "⚠️ This action cannot be undone!"
                ),
                color=0xFF0000,
            )
            view = BurnConfirmView(nft_num, nft_id, self.reason.value)
            # Present confirmation to burn the NFT; use an embedded view
            await interaction.followup.send(
                embed=confirm_embed,
                view=view,
                ephemeral=True,
            )
        except Exception as exc:
            logging.error("Error in burn modal: %s", exc)
            await interaction.followup.send(
                "❌ Error processing burn request. Check logs for details.",
                ephemeral=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass


class BurnConfirmView(View):
    def __init__(self, nft_number: int, nft_id: str, reason: str) -> None:
        super().__init__(timeout=60)
        self.nft_number = nft_number
        self.nft_id = nft_id
        self.reason = reason

    @discord.ui.button(label="Confirm Burn", style=discord.ButtonStyle.danger)
    async def confirm_burn(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.defer(ephemeral=True)
        success = await burn_nft(self.nft_id)
        if not success:
            await interaction.followup.send(
                f"❌ Failed to burn NFT #{self.nft_number}. Check logs for details.",
                ephemeral=True,
            )
            self.stop()
            return
        try:
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS burned_nfts ("
                "nft_number INTEGER PRIMARY KEY,"
                "nft_id TEXT,"
                "discord_id TEXT,"
                "burned_by TEXT,"
                "reason TEXT,"
                "burned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                "original_mint_time TIMESTAMP"
                ")"
            )
            cursor.execute(
                "SELECT nft_number, nft_id, discord_id, created_at FROM LFG WHERE nft_number = ?",
                (self.nft_number,),
            )
            nft_data = cursor.fetchone()
            cursor.execute(
                "INSERT INTO burned_nfts (nft_number, nft_id, discord_id, burned_by, reason, original_mint_time)"
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    nft_data[0],
                    nft_data[1],
                    nft_data[2],
                    str(interaction.user.id),
                    self.reason,
                    nft_data[3],
                ),
            )
            cursor.execute("DELETE FROM LFG WHERE nft_number = ?", (self.nft_number,))
            conn.commit()
        except Exception as exc:
            logging.error("Error finalising NFT burn: %s", exc)
            # Notify the user of a database error during the burn operation
            await interaction.followup.send(
                "❌ Database error while burning NFT.",
                ephemeral=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
        # Notify the user of successful burn
        await interaction.followup.send(
            f"✅ Successfully burned NFT #{self.nft_number}",
            ephemeral=True,
        )
        try:
            guild = interaction.guild
            if guild:
                log_channel = guild.get_channel(ADMIN_LOG_CHANNEL_ID)
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
                        color=0xFF0000,
                    )
                    await log_channel.send(embed=log_embed)
        except Exception as exc:
            logging.error("Failed to send burn log: %s", exc)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_burn(self, interaction: discord.Interaction, button: Button) -> None:
        # Inform the user that the burn has been cancelled; send as an
        # ephemeral message so only they see it.
        await interaction.response.send_message("❌ NFT burn cancelled.", ephemeral=True)
        self.stop()


@tree.command(name="admin", description="Admin control panel for NFT management")
@app_commands.checks.has_permissions(administrator=True)
async def admin_command(interaction: discord.Interaction) -> None:
    embed = Embed(
        title="🔧 Admin Control Panel",
        description=(
            "Welcome to the NFT Admin Panel!\n\n"
            "**Available Actions:**\n"
            "• 📊 View Stats - Check minting statistics\n"
            "• 🔍 Lookup NFT - View details of specific NFT\n"
            "• 🔥 Burn NFT - Burn a specific NFT"
        ),
        color=0x9C84EF,
    )
    embed.set_footer(text="Admin panel will timeout after 10 minutes")
    # Send the admin panel as an ephemeral message so only the admin sees it
    await interaction.response.send_message(embed=embed, view=AdminView(),
                                            ephemeral=True)


class NFTLookupModal(Modal, title="NFT Lookup"):
    nft_number = TextInput(
        label="Enter NFT Number",
        placeholder="e.g., 3535",
        required=True,
        min_length=1,
        max_length=10,
    )
    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            nft_num = int(self.nft_number.value)
        except ValueError:
            # Invalid NFT number provided
            await interaction.followup.send("❌ Please enter a valid NFT number.",
                                           ephemeral=True)
            return
        try:
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT nft_number, nft_id, discord_id, created_at FROM LFG WHERE nft_number = ?",
                (nft_num,),
            )
            result = cursor.fetchone()
            cursor.execute(
                "SELECT burned_by, reason, burned_at FROM burned_nfts WHERE nft_number = ?",
                (nft_num,),
            )
            burn_info = cursor.fetchone()
            if result:
                # Construct an embed with NFT details
                embed = Embed(title=f"🔍 NFT #{result[0]} Details", color=0x9C84EF)
                embed.add_field(name="NFT ID", value=result[1] or "Not minted", inline=True)
                if result[2]:
                    embed.add_field(name="Minted By", value=f"<@{result[2]}>", inline=True)
                embed.add_field(
                    name="Minted On",
                    value=result[3][:10] if result[3] else "N/A",
                    inline=True,
                )
                if burn_info:
                    embed.add_field(
                        name="🔥 Burn Status",
                        value=(
                            f"Burned by: <@{burn_info[0]}>\n"
                            f"Reason: {burn_info[1]}\n"
                            f"Date: {burn_info[2][:10]}"
                        ),
                        inline=False,
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                # NFT not found in either table
                await interaction.followup.send(
                    f"❌ NFT #{nft_num} not found in database.",
                    ephemeral=True,
                )
        except Exception as exc:
            # Unexpected error during lookup
            logging.error("Error looking up NFT: %s", exc)
            await interaction.followup.send(
                "❌ Error looking up NFT. Check logs for details.",
                ephemeral=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass


class AdminView(View):
    def __init__(self) -> None:
        super().__init__(timeout=600)
    @discord.ui.button(label="📊 View Stats", style=discord.ButtonStyle.primary)
    async def stats_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            conn = sqlite3.connect(DATABASE)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM LFG WHERE nft_id IS NOT NULL")
            total_minted = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(DISTINCT discord_id) FROM LFG WHERE discord_id IS NOT NULL")
            unique_users = cursor.fetchone()[0]
            cursor.execute(
                "SELECT nft_number, discord_id, created_at FROM LFG WHERE nft_id IS NOT NULL ORDER BY created_at DESC LIMIT 5"
            )
            recent_mints = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) FROM burned_nfts")
            burned_count = cursor.fetchone()[0]
            embed = Embed(title="📊 Minting Statistics", color=0x9C84EF)
            embed.add_field(name="Total NFTs Minted", value=str(total_minted), inline=True)
            embed.add_field(name="Unique Users", value=str(unique_users), inline=True)
            embed.add_field(name="Burned NFTs", value=str(burned_count), inline=True)
            if recent_mints:
                text = "\n".join(
                    f"#{num} by <@{uid}> on {date[:10]}" for num, uid, date in recent_mints
                )
                embed.add_field(name="Recent Mints", value=text, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            logging.error("Error retrieving stats: %s", exc)
            await interaction.followup.send(
                "❌ Error retrieving statistics. Check logs for details.",
                ephemeral=True,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    @discord.ui.button(label="🔍 Lookup NFT", style=discord.ButtonStyle.primary)
    async def lookup_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(NFTLookupModal())
    @discord.ui.button(label="🔥 Burn NFT", style=discord.ButtonStyle.danger)
    async def burn_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(BurnNFTModal())


# get_user is now imported from user_db as get_user_from_db
# This function is kept for backward compatibility but should not be used
def get_user(user: discord.User) -> Optional[Dict[str, str]]:
    """DEPRECATED: Use get_user_from_db instead. Retrieve a user's data from database."""
    return get_user_from_db(str(user.id))


async def burn_nft(nft_id: str) -> bool:
    """Burn an NFT on XRPL and return True if successful."""
    try:
        wallet = Wallet.from_seed(SEED)
        client = JsonRpcClient(JSON_RPC_URL)
        tx = NFTokenBurn(account=wallet.classic_address, nftoken_id=nft_id)
        resp = await asyncio.to_thread(submit_and_wait, tx, client, wallet)
        return resp.result.get("meta", {}).get("TransactionResult") == "tesSUCCESS"
    except Exception as exc:
        logging.error("Error burning NFT: %s", exc)
        return False


async def cleanup() -> None:
    logging.info("Performing cleanup before shutdown...")
    try:
        if not bot.is_closed():
            await bot.close()
    except Exception as exc:
        logging.error("Error during cleanup: %s", exc)


def signal_handler(sig, frame) -> None:
    logging.info("Received signal %s, initiating shutdown...", sig)
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup())
    loop.stop()


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


@bot.event
async def on_ready() -> None:
    create_users_table()
    await tree.sync()
    logging.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)


if __name__ == "__main__":
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as exc:
        logging.error("Failed to start bot: %s", exc)
