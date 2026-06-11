# lfg_core/xumm_ops.py
# XUMM/Xaman payload helpers: payment links, QR rendering, trustline and
# NFT-accept payloads (extracted from main.py).

import io
import json
import asyncio
import logging

import qrcode
import requests

from lfg_core import config

_XUMM_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "X-API-Key": config.XUMM_API_KEY,
    "X-API-Secret": config.XUMM_API_SECRET,
}


def generate_static_payment_link(destination: str, value: str = "1") -> str:
    """xaman.app/detect deep link for a 1-token payment; works in any XRPL wallet."""
    transaction_json = {
        "TransactionType": "Payment",
        "Destination": destination,
        "Amount": {
            "currency": config.TOKEN_CURRENCY_HEX,
            "value": value,
            "issuer": config.TOKEN_ISSUER_ADDRESS,
        },
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


async def _create_xumm_payload(txjson: dict, options: dict = None):
    """POST a payload to the XUMM platform API; returns qr/deeplink dict or None."""
    payload = {"txjson": txjson}
    if options:
        payload["options"] = options
    try:
        response = await asyncio.to_thread(
            requests.post, config.XUMM_API_URL, json=payload, headers=_XUMM_HEADERS
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


async def create_accept_offer_payload(offer_id: str):
    """XUMM payload for NFTokenAcceptOffer."""
    return await _create_xumm_payload({
        "TransactionType": "NFTokenAcceptOffer",
        "NFTokenSellOffer": offer_id,
    })


async def create_trustline_payload():
    """XUMM payload for the LFGO TrustSet."""
    return await _create_xumm_payload(
        {
            "TransactionType": "TrustSet",
            "Flags": 131072,  # tfSetNoRipple
            "LimitAmount": {
                "currency": config.TOKEN_CURRENCY_HEX,
                "issuer": config.TOKEN_ISSUER_ADDRESS,
                "value": config.TOKEN_TRUSTLINE_LIMIT,
            },
        },
        options={"expire": 5},
    )
