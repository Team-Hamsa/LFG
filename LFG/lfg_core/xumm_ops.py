# lfg_core/xumm_ops.py
# XUMM/Xaman payload helpers: payment links, QR rendering and NFT-accept
# payloads (extracted from main.py).

import io
import json
import asyncio
import logging
from decimal import Decimal

import qrcode
import requests
from xrpl.utils import xrp_to_drops

from lfg_core import config

_XUMM_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "X-API-Key": config.XUMM_API_KEY,
    "X-API-Secret": config.XUMM_API_SECRET,
}


def _payment_amount(value: str, currency: str, issuer: str):
    """XRPL Amount field for a Payment: native XRP is a drops string, IOUs
    are a currency dict. currency/issuer default to the LFGO mint token."""
    if currency == "XRP":
        return xrp_to_drops(Decimal(value))
    return {
        "currency": currency or config.TOKEN_CURRENCY_HEX,
        "value": value,
        "issuer": issuer or config.TOKEN_ISSUER_ADDRESS,
    }


def generate_static_payment_link(destination: str, value: str = "1",
                                 currency: str = None, issuer: str = None) -> str:
    """xaman.app/detect deep link for a payment; works in any XRPL wallet.
    currency/issuer default to the LFGO mint token; "XRP" means native XRP."""
    transaction_json = {
        "TransactionType": "Payment",
        "Destination": destination,
        "Amount": _payment_amount(value, currency, issuer),
    }
    tx_hex = json.dumps(transaction_json).encode('utf-8').hex()
    return f"https://xaman.app/detect/{tx_hex}"


def generate_qr_png(data: str) -> bytes:
    """Render a QR code PNG for the given string."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def _with_return_url(options: dict, return_url: dict) -> dict:
    if return_url:
        options["return_url"] = return_url
    return options or None


def discord_return_url(guild_id, channel_id):
    """XUMM return_url dict that bounces the user back to the Discord channel
    hosting the Activity after signing in Xaman (issue #14). The IDs come
    from the untrusted client, so anything non-numeric is rejected."""
    if not (isinstance(guild_id, str) and guild_id.isdigit()
            and isinstance(channel_id, str) and channel_id.isdigit()):
        return None
    return {
        "app": f"discord://-/channels/{guild_id}/{channel_id}",
        "web": f"https://discord.com/channels/{guild_id}/{channel_id}",
    }


async def _create_xumm_payload(txjson: dict, options: dict = None):
    """POST a payload to the XUMM platform API; returns qr/deeplink dict or None."""
    payload = {"txjson": txjson}
    if options:
        payload["options"] = options
    try:
        response = await asyncio.to_thread(
            requests.post, config.XUMM_API_URL, json=payload,
            headers=_XUMM_HEADERS, timeout=10
        )
        data = response.json()
        return {
            'qr_url': data['refs']['qr_png'],
            'xumm_url': data['next']['always'],
            'uuid': data['uuid'],
        }
    except Exception as e:
        logging.error(f"Error creating XUMM payload: {e}")
        return None


async def create_payment_payload(destination: str, value: str = "1",
                                 currency: str = None, issuer: str = None,
                                 expire_minutes: int = None,
                                 return_url: dict = None):
    """XUMM sign-request payload for a token Payment. This is what payment
    QRs must encode: Xaman only understands its own payload links
    (xumm.app/sign/<uuid>) — it cannot parse the raw-transaction-JSON
    xaman.app/detect link from generate_static_payment_link, which is kept
    only as a last-resort fallback when the XUMM API is unreachable."""
    if expire_minutes is None:
        # Match the on-ledger payment wait so the sign request and the
        # subscription expire together.
        expire_minutes = max(1, -(-config.PAYMENT_TIMEOUT_SECONDS // 60))
    return await _create_xumm_payload(
        {
            "TransactionType": "Payment",
            "Destination": destination,
            "Amount": _payment_amount(value, currency, issuer),
        },
        options=_with_return_url({"expire": expire_minutes}, return_url),
    )


async def create_accept_offer_payload(offer_id: str, return_url: dict = None):
    """XUMM payload for NFTokenAcceptOffer."""
    return await _create_xumm_payload({
        "TransactionType": "NFTokenAcceptOffer",
        "NFTokenSellOffer": offer_id,
    }, options=_with_return_url({}, return_url))
