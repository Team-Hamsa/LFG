# surfaces/discord_bot/trustline.py
# Trustline flow: create XUMM TrustSet payload + bounded poll loop.
# Stays bot-local (D2=A).  SourceTag added per Make Waves invariant (#75).
import asyncio
import logging
from typing import Any

import discord
import requests

from lfg_core import memos
from lfg_core.config import SOURCE_TAG
from surfaces.discord_bot import config


async def safe_followup(interaction: discord.Interaction, *args, **kwargs):
    """followup.send that survives an expired/invalid interaction token.
    Discord webhook tokens last 15 minutes; long-running handlers (payment
    or trustline polling) can outlive them, and the resulting 401 (50027)
    must not crash the handler. Returns True if the message was delivered."""
    try:
        await interaction.followup.send(*args, **kwargs)
        return True
    except (discord.NotFound, discord.HTTPException) as e:
        logging.warning(f"Follow-up message not delivered (interaction token likely expired): {e}")
        return False


async def create_trustline_request() -> dict[str, Any] | None:
    """Create a XUMM request to set up token trustline"""
    logging.info("Creating trustline request")
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "X-API-Key": config.XUMM_API_KEY,
        "X-API-Secret": config.XUMM_API_SECRET,
    }

    # Create the transaction JSON for setting up token trustline
    transaction_json = {
        "TransactionType": "TrustSet",
        "Flags": 131072,  # tfSetNoRipple flag
        "SourceTag": SOURCE_TAG,  # Make Waves invariant (#75)
        # Provenance memo (#54): user-signed TrustSet from the Discord bot.
        "Memos": memos.build_memos_json(
            memos.INITIATOR_USER, memos.PLATFORM_DISCORD_BOT, memos.ACTION_TRUSTSET
        ),
        "LimitAmount": {
            "currency": config.TOKEN_CURRENCY_HEX,
            "issuer": config.TOKEN_ISSUER_ADDRESS,
            "value": config.TOKEN_TRUSTLINE_LIMIT,
        },
    }
    logging.info(f"Trustline transaction JSON: {transaction_json!r}")

    payload = {
        "txjson": transaction_json,
        "options": {
            "expire": 5,  # Expires in 5 minutes
            "return_url": {"web": "https://letseffinggo.com/"},
        },
    }

    try:
        # Make the API request
        response = await asyncio.to_thread(
            requests.post, config.XUMM_API_URL, json=payload, headers=headers
        )
        response_data = response.json()

        return {
            "qr_url": response_data["refs"]["qr_png"],
            "xumm_url": response_data["next"]["always"],
            "uuid": response_data["uuid"],
        }

    except Exception as e:
        logging.error(f"Error generating trustline request: {e}")
        return None


async def poll_trustline_status(
    interaction: discord.Interaction, trustline_data: dict[str, Any]
) -> None:
    """Bounded XUMM payload poll loop extracted from trustline_button.
    Runs for up to 300 s (matching the 5-min payload expiry) and sends
    a follow-up message with the final state."""
    try:
        # Check trustline status using XUMM payload. The loop is
        # bounded by wall clock (not iteration count) and each XUMM
        # call gets its own timeout, so a slow/hanging API can never
        # stretch the handler past Discord's 15-minute webhook token.
        if "uuid" in trustline_data:
            # Poll the XUMM REST endpoint directly with a real network
            # timeout: the SDK's payload.get exposes none, and a
            # hanging call inside to_thread outlives any asyncio-level
            # timeout and piles up worker threads.
            status_headers = {
                "accept": "application/json",
                "X-API-Key": config.XUMM_API_KEY,
                "X-API-Secret": config.XUMM_API_SECRET,
            }
            status_url = f"{config.XUMM_API_URL}/{trustline_data['uuid']}"
            loop = asyncio.get_running_loop()  # get_event_loop() is deprecated in a coroutine
            deadline = loop.time() + 300  # matches the 5-min payload expiry
            while loop.time() < deadline:
                try:
                    response = await asyncio.to_thread(
                        requests.get, status_url, headers=status_headers, timeout=10
                    )
                    meta = response.json().get("meta", {})
                    if meta.get("resolved"):
                        if meta.get("signed"):
                            await safe_followup(
                                interaction,
                                "✅ Trustline set up successfully! You can now hold LFGO tokens.",
                                ephemeral=True,
                            )
                        else:
                            # resolved without signed = user declined:
                            # terminal, so stop polling immediately
                            await safe_followup(
                                interaction,
                                "Trustline request was declined or cancelled. "
                                "Run it again whenever you're ready.",
                                ephemeral=True,
                            )
                        return
                    if meta.get("cancelled") or meta.get("expired"):
                        # Also terminal: cancelled/expired payloads can
                        # never be signed even though resolved is false
                        await safe_followup(
                            interaction,
                            "Trustline request expired or was cancelled. Please try again.",
                            ephemeral=True,
                        )
                        return
                except requests.Timeout:
                    logging.warning("XUMM payload status check timed out; retrying")
                except Exception as e:
                    logging.error(f"Error checking trustline status: {e}")
                await asyncio.sleep(5)

        # If we get here, request timed out
        await safe_followup(
            interaction, "Trustline request timed out. Please try again.", ephemeral=True
        )
    except Exception as e:
        logging.error(f"Error in trustline checking: {e}")
        await safe_followup(
            interaction,
            "Error checking trustline status. Please try again.",
            ephemeral=True,
        )
