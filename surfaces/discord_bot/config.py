# surfaces/discord_bot/config.py
# All environment-derived settings for the Discord adapter. Relocated from the
# top of the legacy main.py (no value changes) plus the two new spine vars.
import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} not found in environment variables")
    return value


# --- Discord ---
DISCORD_BOT_TOKEN = _require("DISCORD_BOT_TOKEN")
ADMIN_LOG_CHANNEL_ID = int(os.getenv("ADMIN_LOG_CHANNEL_ID", "0"))
if not ADMIN_LOG_CHANNEL_ID:
    raise ValueError("ADMIN_LOG_CHANNEL_ID not found in environment variables")

# --- Shared service (spine) ---
LFG_SERVICE_URL = _require("LFG_SERVICE_URL")
SERVICE_TOKEN_DISCORD = _require("SERVICE_TOKEN_DISCORD")

# --- XUMM (trustline stays bot-local, D2=A) ---
XUMM_API_KEY = _require("XUMM_API_KEY")
XUMM_API_SECRET = _require("XUMM_API_SECRET")
XUMM_API_URL = os.getenv("XUMM_API_URL", "https://xumm.app/api/v1/platform/payload")

# --- XRPL / token (trustline payload) ---
TOKEN_ISSUER_ADDRESS = _require("TOKEN_ISSUER_ADDRESS")
TOKEN_CURRENCY_HEX = _require("TOKEN_CURRENCY_HEX")
TOKEN_TRUSTLINE_LIMIT = os.getenv("TOKEN_TRUSTLINE_LIMIT", "1000")

# --- UI / retry ---
EXTERNAL_WEBSITE_URL = os.getenv("EXTERNAL_WEBSITE_URL", "https://letseffinggo.com")
VIEW_TIMEOUT = int(os.getenv("VIEW_TIMEOUT", "600"))
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
