# lfg_core/config.py
# Centralized environment configuration for the webapp/core modules.
# main.py keeps its own loading for backwards compatibility.

import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} not found in environment variables")
    return value


# XUMM
XUMM_API_KEY = _require("XUMM_API_KEY")
XUMM_API_SECRET = _require("XUMM_API_SECRET")
XUMM_API_URL = os.getenv("XUMM_API_URL", "https://xumm.app/api/v1/platform/payload")

# XRPL
SEED = _require("SEED")
TOKEN_ISSUER_ADDRESS = _require("TOKEN_ISSUER_ADDRESS")
TOKEN_CURRENCY_HEX = _require("TOKEN_CURRENCY_HEX")
TOKEN_TRUSTLINE_LIMIT = os.getenv("TOKEN_TRUSTLINE_LIMIT", "1000")
JSON_RPC_URL = os.getenv("XRPL_JSON_RPC_URL", "https://s.altnet.rippletest.net:51234/")
WS_URL = os.getenv("XRPL_WS_URL", "wss://s.altnet.rippletest.net:51233")

# NFT settings
NFT_TAXON = int(os.getenv("NFT_TAXON", "0"))
NFT_TRANSFER_FEE = int(os.getenv("NFT_TRANSFER_FEE", "7000"))
NFT_FLAGS = int(os.getenv("NFT_FLAGS", "9"))
NFT_COLLECTION_NAME = os.getenv("NFT_COLLECTION_NAME", "Let's Effing Go!")

# BunnyCDN
BUNNY_CDN_ACCESS_KEY = _require("BUNNY_CDN_ACCESS_KEY")
BUNNY_CDN_STORAGE_ZONE = _require("BUNNY_CDN_STORAGE_ZONE")
BUNNY_CDN_BASE_URL = os.getenv("BUNNY_CDN_BASE_URL", "https://storage.bunnycdn.com")
BUNNY_CDN_FOLDER = os.getenv("BUNNY_CDN_FOLDER", "minttest")
BUNNY_CDN_PUBLIC_BASE = os.getenv("BUNNY_CDN_PUBLIC_BASE", "https://lfgo.b-cdn.net")

# Discord Activity (webapp only — not required by the bot)
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
WEBAPP_SESSION_SECRET = os.getenv("WEBAPP_SESSION_SECRET", "")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))

# Misc
TRAIT_LAYERS_DIR = os.getenv("TRAIT_LAYERS_DIR", "trait_layers")
PAYMENT_TIMEOUT_SECONDS = int(os.getenv("PAYMENT_TIMEOUT_SECONDS", "300"))
