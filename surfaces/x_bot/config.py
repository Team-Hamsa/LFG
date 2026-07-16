# surfaces/x_bot/config.py
# Environment-derived settings for the X (Twitter) poster surface (#41).
# SERVICE_TOKEN_X auto-registers the surface server-side (lfg_service/auth.py) —
# no service code change needed for auth (the generic SERVICE_TOKEN_<SURFACE>
# scan in auth.py picks it up).
#
# Deliberately different from surfaces/telegram_bot/config.py in one way: every
# var here is optional with a safe default (no `_require`). The house
# convention (see lfg_core/config.py's X_* block, and the global constraint
# for this feature) is "unset X_* => feature off", and bot.py's own startup
# gate (`X_ENABLED` check, then a loud SERVICE_TOKEN_X check) is what turns a
# missing credential into a clear log line + sys.exit, not an import-time
# crash — a bare `_require` here would crash on import before that gate ever
# runs, even when the surface is meant to stay off.
import logging
import os

from dotenv import load_dotenv

load_dotenv()

LFG_SERVICE_URL = os.getenv("LFG_SERVICE_URL", "http://localhost:8000")
SERVICE_TOKEN_X = os.getenv("SERVICE_TOKEN_X", "")

# Base delay for the /events WebSocket reconnect backoff (surfaces/_client/events.py
# doubles this on each failed reconnect, capped at 30s) — same env var name and
# default as the other surfaces (surfaces/telegram_bot/config.py).
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
