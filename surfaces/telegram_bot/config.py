# surfaces/telegram_bot/config.py
# Environment-derived settings for the Telegram adapter. SERVICE_TOKEN_TELEGRAM
# auto-registers the surface server-side (lfg_service/auth.py) — no service code
# change needed for auth.
import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} not found in environment variables")
    return value


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
LFG_SERVICE_URL = _require("LFG_SERVICE_URL")
SERVICE_TOKEN_TELEGRAM = _require("SERVICE_TOKEN_TELEGRAM")

TELEGRAM_ANNOUNCE_CHAT_ID = int(os.getenv("TELEGRAM_ANNOUNCE_CHAT_ID", "0"))
if not TELEGRAM_ANNOUNCE_CHAT_ID:
    raise ValueError("TELEGRAM_ANNOUNCE_CHAT_ID not found in environment variables")

RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

# Telegram Mini App (#89). Public HTTPS URL the launch button / menu button
# point at. Optional: when empty, no launch button is shown and the menu button
# is not set (the feature stays dormant until the ops step provisions hosting).
TELEGRAM_MINI_APP_URL = os.getenv("TELEGRAM_MINI_APP_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
