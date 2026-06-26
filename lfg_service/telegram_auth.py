# lfg_service/telegram_auth.py
# Pure, unit-testable validator for Telegram Mini App `initData` (#89).
#
# When Telegram launches a Mini App it injects a signed launch payload as
# `window.Telegram.WebApp.initData` (a URL-encoded query string), HMAC-signed
# with the bot token. The server validates it to trust the embedded `user.id`
# without any OAuth round-trip. NEVER trust `initDataUnsafe` (the unsigned
# client-readable mirror); only the raw `initData` string, validated here, may
# be trusted.
#
# This module is intentionally pure: no I/O, no globals, `now` injectable, so
# the algorithm is pinned by deterministic unit tests.
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age: int,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Validate Telegram Mini App `initData` and return its parsed fields.

    Algorithm (per Telegram docs):
      1. parse the query string into key/value pairs (URL-decoded);
      2. pull out `hash` (and drop `signature`, the newer Ed25519 scheme — it is
         not part of the HMAC data-check-string);
      3. data-check-string = remaining `key=value` pairs sorted by key, joined
         by `\\n`;
      4. secret_key = HMAC_SHA256(key=b"WebAppData", msg=bot_token)
         — NOTE the inversion: the literal string "WebAppData" is the HMAC KEY
         and the bot token is the MESSAGE (the #1 gotcha);
      5. computed = hex(HMAC_SHA256(key=secret_key, msg=data_check_string));
      6. accept iff hmac.compare_digest(computed, received_hash);
      7. reject if `auth_date` is older than `max_age` seconds (the only replay
         guard — initData carries no nonce).

    Returns the parsed fields dict (with `user` decoded to a dict) on success,
    else None. Never raises on malformed input. Never logs `init_data` or the
    bot token.
    """
    if not init_data or not bot_token:
        return None

    # parse_qsl drops empty values; keep_blank_values keeps shape but we only
    # need the signed fields, all of which are non-empty in practice.
    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields: dict[str, str] = dict(pairs)

    received_hash = fields.pop("hash", None)
    if not received_hash:
        return None
    # `signature` (Ed25519 scheme) is not part of the HMAC data-check-string.
    fields.pop("signature", None)

    data_check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None

    # Staleness: reject if auth_date is older than max_age.
    auth_date_raw = fields.get("auth_date")
    if not auth_date_raw:
        return None
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        return None
    current = int(time.time()) if now is None else now
    if current - auth_date > max_age:
        return None

    # The user field is a JSON object; an identity payload is unusable without
    # it, so a missing/malformed user is treated as a validation failure.
    user_raw = fields.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None

    result: dict[str, Any] = dict(fields)
    result["user"] = user
    return result
