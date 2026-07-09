# lfg_core/xumm_ops.py
# XUMM/Xaman payload helpers: payment links, QR rendering and NFT-accept
# payloads (extracted from main.py).

import asyncio
import io
import json
import logging
import os
import re
from decimal import Decimal
from typing import Any

import qrcode
import requests
from PIL import Image
from xrpl.utils import xrp_to_drops

from lfg_core import config, memos

_XUMM_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "X-API-Key": config.XUMM_API_KEY,
    "X-API-Secret": config.XUMM_API_SECRET,
}


def _payment_amount(value: str, currency: str | None, issuer: str | None) -> str | dict[str, str]:
    """XRPL Amount field for a Payment: native XRP is a drops string, IOUs
    are a currency dict. currency/issuer default to the LFGO mint token."""
    if currency == "XRP":
        return xrp_to_drops(Decimal(value))
    return {
        "currency": currency or config.TOKEN_CURRENCY_HEX,
        "value": value,
        "issuer": issuer or config.TOKEN_ISSUER_ADDRESS,
    }


def generate_static_payment_link(
    destination: str, value: str = "1", currency: str | None = None, issuer: str | None = None
) -> str:
    """xaman.app/detect deep link for a payment; works in any XRPL wallet.
    currency/issuer default to the LFGO mint token; "XRP" means native XRP."""
    transaction_json = {
        "TransactionType": "Payment",
        "Destination": destination,
        "Amount": _payment_amount(value, currency, issuer),
    }
    tx_hex = json.dumps(transaction_json).encode("utf-8").hex()
    return f"https://xaman.app/detect/{tx_hex}"


# Mascot composited into the center of every QR (issue #19). High error
# correction tolerates the covered modules; a missing file means plain QRs.
QR_LOGO_PATH = os.getenv(
    "QR_LOGO_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "webapp",
        "client",
        "assets",
        "mascot.png",
    ),
)


# Decoded once per path (not per QR) to avoid disk I/O on every render;
# keyed by path so a QR_LOGO_PATH override picks up the new file.
_qr_logo_cache: dict[str, Image.Image] = {}


def _load_qr_logo() -> Image.Image:
    logo = _qr_logo_cache.get(QR_LOGO_PATH)
    if logo is None:
        logo = Image.open(QR_LOGO_PATH).convert("RGBA")
        _qr_logo_cache[QR_LOGO_PATH] = logo
    return logo.copy()  # thumbnail() mutates; never resize the cached original


def _apply_qr_logo(img: Image.Image) -> Image.Image:
    logo = _load_qr_logo()
    # ~1/4 of the QR width keeps well under ERROR_CORRECT_H's 30% budget
    side = img.size[0] // 4
    logo.thumbnail((side, side), Image.Resampling.LANCZOS)
    lw, lh = logo.size
    cx, cy = (img.size[0] - lw) // 2, (img.size[1] - lh) // 2
    # white backing pad so the mascot never sits directly on dark modules
    pad = max(4, side // 10)
    backing = Image.new("RGB", (lw + 2 * pad, lh + 2 * pad), "white")
    img.paste(backing, (cx - pad, cy - pad))
    img.paste(logo, (cx, cy), logo)
    return img


def generate_qr_png(data: str) -> bytes:
    """Render a QR code PNG with the brand mascot in the center."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    try:
        img = _apply_qr_logo(img)
    except Exception as e:  # missing/corrupt logo: serve the plain QR
        logging.warning(f"QR branding skipped: {e}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _with_return_url(
    options: dict[str, Any], return_url: dict[str, str] | None
) -> dict[str, Any] | None:
    if return_url:
        options["return_url"] = return_url
    return options or None


def discord_return_url(guild_id: Any, channel_id: Any) -> dict[str, str] | None:
    """XUMM return_url dict that bounces the user back to the Discord channel
    hosting the Activity after signing in Xaman (issue #14). The IDs come
    from the untrusted client, so anything non-numeric is rejected."""
    if not (
        isinstance(guild_id, str)
        and guild_id.isdigit()
        and isinstance(channel_id, str)
        and channel_id.isdigit()
    ):
        return None
    return {
        "app": f"discord://-/channels/{guild_id}/{channel_id}",
        "web": f"https://discord.com/channels/{guild_id}/{channel_id}",
    }


async def _create_xumm_payload(
    txjson: dict[str, Any],
    options: dict[str, Any] | None = None,
    user_token: str | None = None,
    memos_json: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """POST a payload to the XUMM platform API; returns qr/deeplink dict or None.

    When ``user_token`` is a stored per-user push token (issue #135), XUMM
    delivers the sign request straight to that user's Xaman app as a push
    notification. The returned dict's ``pushed`` flag reports whether the push
    actually went out — a stale/expired token yields ``pushed: False`` and the
    caller falls back to the QR / deep link that are always returned too. A
    missing token simply omits the field, never blocking the sign."""
    # Make Waves hackathon: every signed transaction must carry the source tag,
    # and provenance memos (#54). SignIn is a pseudo-transaction (no ledger
    # effect), so it is exempt from both.
    if txjson.get("TransactionType") != "SignIn":
        txjson.setdefault("SourceTag", config.SOURCE_TAG)
        if memos_json:
            txjson.setdefault("Memos", memos_json)
    payload: dict[str, Any] = {"txjson": txjson}
    if options:
        payload["options"] = options
    # user_token is a top-level payload field in the XUMM platform API (not an
    # option). Only send it when we actually have one so an empty string can't
    # be misread as a token.
    if user_token:
        payload["user_token"] = user_token
    try:
        response = await asyncio.to_thread(
            requests.post, config.XUMM_API_URL, json=payload, headers=_XUMM_HEADERS, timeout=10
        )
        data = response.json()
        return {
            "qr_url": data["refs"]["qr_png"],
            "xumm_url": data["next"]["always"],
            "uuid": data["uuid"],
            # Whether XUMM push-delivered this payload to the user's Xaman app.
            # False (or absent → False) means fall back to the QR/deep link.
            "pushed": bool(data.get("pushed")),
        }
    except Exception as e:
        logging.error(f"Error creating XUMM payload: {e}")
        return None


async def create_payment_payload(
    destination: str,
    value: str = "1",
    currency: str | None = None,
    issuer: str | None = None,
    expire_minutes: int | None = None,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    action: str = memos.ACTION_PAYMENT,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """XUMM sign-request payload for a token Payment. This is what payment
    QRs must encode: Xaman only understands its own payload links
    (xumm.app/sign/<uuid>) — it cannot parse the raw-transaction-JSON
    xaman.app/detect link from generate_static_payment_link, which is kept
    only as a last-resort fallback when the XUMM API is unreachable.

    ``user_token`` (issue #135) push-delivers the request to a known user.
    ``platform``/``action`` populate the provenance memo (#54); this payload is
    user-signed, so the initiator is always ``user``. Pass a specific ``action``
    (e.g. the mint fee vs. ``trait-swap-fee``) to distinguish payment flows."""
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
        user_token=user_token,
        memos_json=memos.build_memos_json(memos.INITIATOR_USER, platform, action, campaign),
    )


async def create_accept_offer_payload(
    offer_id: str,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
    action: str = memos.ACTION_ACCEPT_OFFER,
) -> dict[str, Any] | None:
    """XUMM payload for NFTokenAcceptOffer. ``user_token`` push-delivers it (#135).
    ``platform`` populates the user-signed provenance memo (#54). ``action``
    distinguishes a marketplace buy from a plain offer accept — same tx type,
    different app action on the permanent memo."""
    return await _create_xumm_payload(
        {
            "TransactionType": "NFTokenAcceptOffer",
            "NFTokenSellOffer": offer_id,
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(memos.INITIATOR_USER, platform, action, campaign),
    )


async def create_sell_offer_payload(
    account: str,
    nft_id: str,
    drops: str,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """XUMM payload for NFTokenCreateOffer listing an NFT for sale on the
    in-app marketplace. `drops` must already be an integer-drops string (see
    `market_ops.xrp_to_drops_str`) — no float/Decimal handling here. Flags=1
    marks a sell offer; Owner is omitted (only meaningful when someone other
    than the token owner creates the offer) and Destination is omitted so the
    listing is open to any buyer rather than locked to one counterparty.

    ``user_token`` (issue #135) push-delivers the request to a known user."""
    return await _create_xumm_payload(
        {
            "TransactionType": "NFTokenCreateOffer",
            "Account": account,
            "NFTokenID": nft_id,
            "Amount": drops,
            "Flags": 1,
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(
            memos.INITIATOR_USER, platform, memos.ACTION_LIST, campaign
        ),
    )


async def create_cancel_offer_payload(
    account: str,
    offer_index: str,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """XUMM payload for NFTokenCancelOffer, delisting one existing sell
    offer by its ledger index. ``user_token`` push-delivers it (#135).
    ``platform`` populates the user-signed provenance memo (#54)."""
    return await _create_xumm_payload(
        {
            "TransactionType": "NFTokenCancelOffer",
            "Account": account,
            "NFTokenOffers": [offer_index],
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(
            memos.INITIATOR_USER, platform, memos.ACTION_CANCEL_OFFER, campaign
        ),
    )


async def create_signin_payload(return_url: dict[str, str] | None = None) -> dict[str, Any] | None:
    """XUMM SignIn payload: the user scans/approves in Xaman and the signed
    payload reveals their wallet address (registration flow, issue #24)."""
    return await _create_xumm_payload(
        {
            "TransactionType": "SignIn",
        },
        options=_with_return_url({}, return_url),
    )


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


async def get_payload_status(uuid: str) -> dict[str, Any] | None:
    """Poll a XUMM payload: whether it was opened (QR scanned) / signed /
    expired, and the signing account once signed. None on API errors or a
    malformed uuid (which is interpolated into the API URL)."""
    if not (isinstance(uuid, str) and _UUID_RE.match(uuid)):
        logging.error(f"Invalid XUMM payload uuid: {uuid!r}")
        return None
    try:
        response = await asyncio.to_thread(
            requests.get, f"{config.XUMM_API_URL}/{uuid}", headers=_XUMM_HEADERS, timeout=10
        )
        data = response.json()
        meta = data.get("meta") or {}
        response_block = data.get("response") or {}
        application = data.get("application") or {}
        return {
            "opened": bool(meta.get("opened")),
            "signed": bool(meta.get("signed")),
            "expired": bool(meta.get("expired")),
            "account": response_block.get("account"),
            # The signed transaction's hash (XUMM's payload status carries no
            # meta of its own — the marketplace list/buy finalize flow fetches
            # the tx by this hash to learn the on-ledger outcome). None until
            # signed.
            "txid": response_block.get("txid"),
            # The per-user push token XUMM issues when a user with Xaman signs
            # and grants push permission (issue #135). Persist it against the
            # signer's identity so future payloads can be push-delivered. None
            # when the user declined push or signed on a channel that issues no
            # token.
            "user_token": application.get("issued_user_token"),
        }
    except Exception as e:
        logging.error(f"Error fetching XUMM payload status: {e}")
        return None
