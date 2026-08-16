# lfg_core/xumm_ops.py
# XUMM/Xaman payload helpers: payment links, QR rendering and NFT-accept
# payloads (extracted from main.py).

import asyncio
import io
import json
import logging
import os
import re
import time
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


class XummRateLimited(Exception):
    """XUMM answered 429 — the app is over its per-minute API quota."""


# After any 429, stop calling out for a cooldown window: XUMM's limit is a
# sliding per-minute average, so hammering through it only extends the outage
# (and did, in the 2026-07-17 incident — 39 rejected creates in 25 minutes).
_RATE_LIMIT_COOLDOWN = float(os.getenv("XUMM_RATE_LIMIT_COOLDOWN", "30.0"))
_rate_limited_until = 0.0


def _note_rate_limited(context: str) -> None:
    global _rate_limited_until
    _rate_limited_until = time.monotonic() + _RATE_LIMIT_COOLDOWN
    logging.warning(
        f"XUMM rate limited (429) during {context}; backing off {_RATE_LIMIT_COOLDOWN}s"
    )


def rate_limited() -> bool:
    """True while the post-429 cooldown is active. The service uses this to
    return 503 + Retry-After (which surfaces do NOT retry) instead of a
    retryable 502 that amplifies the overload."""
    return time.monotonic() < _rate_limited_until


def _check_rate_headers(response: Any, context: str) -> None:
    """Log when XUMM says we're close to the limit, so quota pressure is
    visible in the logs BEFORE the 429s start."""
    remaining = getattr(response, "headers", {}).get("X-RateLimit-Remaining")
    try:
        if remaining is not None and int(remaining) <= 5:
            limit = response.headers.get("X-RateLimit-Limit")
            logging.warning(f"XUMM quota low during {context}: {remaining}/{limit} remaining")
    except (TypeError, ValueError):
        pass


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


# Default payload lifetime. XUMM's own default is 24 hours, so abandoned
# sign requests (closed Activity, regenerated QR, bot-probed signins) pile
# up as open payloads until the app hits the platform's open-payload cap
# and every create is rejected with an embedded 429 ("Max payloads of N
# exceeded", 2026-07-17 incident). Every builder must set an expire; 15
# minutes comfortably outlives any real signing session.
DEFAULT_EXPIRE_MINUTES = 15


def _with_return_url(
    options: dict[str, Any], return_url: dict[str, str] | None
) -> dict[str, Any] | None:
    options.setdefault("expire", DEFAULT_EXPIRE_MINUTES)
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


async def _post_xumm_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """POST one payload to the XUMM platform API and normalize the response.
    Raises on transport errors or an unexpected response shape (a rejected
    payload comes back without refs/next) — callers decide the fallback."""
    response = await asyncio.to_thread(
        requests.post, config.XUMM_API_URL, json=payload, headers=_XUMM_HEADERS, timeout=10
    )
    # Fakes in tests carry no status_code; treat absence as success.
    status = getattr(response, "status_code", 200)
    if status == 429:
        _note_rate_limited("payload create")
        raise XummRateLimited("XUMM payload create rejected (429)")
    _check_rate_headers(response, "payload create")
    data = response.json()
    # XUMM also signals rate limiting inside an HTTP 400 body — the open-
    # payload cap comes back as {"error": {"code": 429, "message": "Max
    # payloads of N exceeded"}} (2026-07-17 incident). Treat it exactly like
    # a transport-level 429: cool off, and never token-less-retry into it.
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("code") == 429:
        _note_rate_limited(f"payload create ({error.get('message', 'embedded 429')})")
        raise XummRateLimited(f"XUMM payload create rejected: {error.get('message')}")
    if status >= 400 or "refs" not in data:
        # A rejected payload comes back without refs/next — surface the real
        # HTTP status instead of the KeyError('refs') this used to raise.
        raise RuntimeError(f"XUMM payload create failed: HTTP {status} {str(data)[:200]}")
    return {
        "qr_url": data["refs"]["qr_png"],
        "xumm_url": data["next"]["always"],
        "uuid": data["uuid"],
        # Whether XUMM push-delivered this payload to the user's Xaman app.
        # False (or absent → False) means fall back to the QR/deep link.
        "pushed": bool(data.get("pushed")),
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
    actually went out; ``push`` refines it for the UI (#212): "sent" (push
    delivered), "failed" (a token was sent but XUMM could not push — the
    payload still appears in Xaman's Events tab), or None (no token; plain
    QR/deep-link sign). If payload creation itself fails WITH a token (e.g. a
    token XUMM rejects outright after an app-key rotation), it is retried once
    without the token so a bad stored token can never block signing."""
    # Make Waves hackathon: every signed transaction must carry the source tag,
    # and provenance memos (#54). SignIn is a pseudo-transaction (no ledger
    # effect), so it is exempt from both.
    txtype = txjson.get("TransactionType")
    if txtype != "SignIn":
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
    sent_token = bool(user_token)
    if rate_limited():
        # Post-429 cooldown: don't spend another call we know will be
        # rejected. Callers already handle None (the service maps it to
        # 503 + Retry-After when rate_limited() is set).
        logging.warning(f"XUMM payload create skipped ({txtype}): rate-limit cooldown active")
        return None
    try:
        result = await _post_xumm_payload(payload)
    except XummRateLimited:
        # NOT the token-less-retry path: a 429 rejects the call, not the
        # token — retrying immediately (with or without the token) just
        # burns more quota. 2026-07-17 incident: each 429'd create was
        # retried without the token, doubling the pressure.
        return None
    except requests.Timeout as e:
        # Ambiguous transport failure: XUMM may have ALREADY created (and
        # pushed) the payload before the response was lost — retrying would
        # mint a duplicate the user could sign while the flow polls the other
        # uuid. Fail instead; the caller's own retry/regenerate paths handle it.
        logging.error(f"XUMM payload create timed out (no retry — outcome unknown): {e}")
        return None
    except Exception as e:
        if not sent_token:
            logging.error(f"Error creating XUMM payload: {e}")
            return None
        # A definitive create failure with a token attached (an HTTP error
        # response or a rejected/garbled body — e.g. XUMM refusing a token
        # from a rotated app key): never let a bad stored token block the
        # sign; retry once as a plain QR/deep-link payload (#212).
        logging.warning(f"XUMM payload create failed with user_token, retrying without: {e}")
        payload.pop("user_token", None)
        sent_token = False
        try:
            result = await _post_xumm_payload(payload)
        except Exception as e2:
            logging.error(f"Error creating XUMM payload: {e2}")
            return None
    # UI-facing push state; the raw `pushed` bool is kept alongside it.
    if result["pushed"]:
        result["push"] = "sent"
    elif sent_token:
        result["push"] = "failed"
    else:
        result["push"] = None
    # #212 observability: one line per payload so the push failure rate is
    # measurable from the service logs. A token that fails to push is the
    # anomaly worth flagging; the other two states log at info.
    if result["push"] == "failed":
        logging.warning(f"XUMM payload {result['uuid']} ({txtype}): push FAILED (token sent)")
    else:
        logging.info(
            f"XUMM payload {result['uuid']} ({txtype}): push={result['push'] or 'no-token'}"
        )
    # Event-driven status (no REST polling): keep this payload's cached
    # status fresh from XUMM's websocket instead.
    watch_payload(result["uuid"])
    return result


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
    account: str | None = None,
) -> dict[str, Any] | None:
    """XUMM sign-request payload for a token Payment. This is what payment
    QRs must encode: Xaman only understands its own payload links
    (xumm.app/sign/<uuid>) — it cannot parse the raw-transaction-JSON
    xaman.app/detect link from generate_static_payment_link, which is kept
    only as a last-resort fallback when the XUMM API is unreachable.

    ``user_token`` (issue #135) push-delivers the request to a known user.
    ``platform``/``action`` populate the provenance memo (#54); this payload is
    user-signed, so the initiator is always ``user``. Pass a specific ``action``
    (e.g. the mint fee vs. ``trait-swap-fee``) to distinguish payment flows.

    ``account`` pins the payer. Every flow that builds a payment then waits for
    it on-ledger verifies the SENDER (`xrpl_ops.wait_for_payment`), so a payload
    Xaman lets any selected account sign is a money trap: the wrong wallet's
    funds move, no wait ever matches them, and the user gets nothing (mainnet
    2026-07-21 — 2 LFGO paid from a second Xaman account). With Account set,
    Xaman refuses to sign from anything else. Optional only for callers that
    genuinely have no expected payer."""
    if expire_minutes is None:
        # Match the on-ledger payment wait so the sign request and the
        # subscription expire together.
        expire_minutes = max(1, -(-config.PAYMENT_TIMEOUT_SECONDS // 60))
    txjson: dict[str, Any] = {
        "TransactionType": "Payment",
        "Destination": destination,
        "Amount": _payment_amount(value, currency, issuer),
    }
    if account:
        txjson["Account"] = account
    return await _create_xumm_payload(
        txjson,
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
    account: str | None = None,
) -> dict[str, Any] | None:
    """XUMM payload for NFTokenAcceptOffer. ``user_token`` push-delivers it (#135).
    ``platform`` populates the user-signed provenance memo (#54). ``action``
    distinguishes a marketplace buy from a plain offer accept — same tx type,
    different app action on the permanent memo.

    ``account`` pins the signer, same rationale as create_payment_payload:
    a delivery offer is Destination-locked so a wrong-wallet signature merely
    wastes a fee, but a marketplace sell offer has no Destination — that accept
    would SUCCEED and buy the NFT into whichever account Xaman had selected.
    Optional only for callers with no expected signer (the CLI economy
    scripts, which have no identity context)."""
    txjson: dict[str, Any] = {
        "TransactionType": "NFTokenAcceptOffer",
        "NFTokenSellOffer": offer_id,
    }
    if account:
        txjson["Account"] = account
    return await _create_xumm_payload(
        txjson,
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(memos.INITIATOR_USER, platform, action, campaign),
    )


async def create_sell_offer_payload(
    account: str,
    nft_id: str,
    amount: str | dict[str, str],
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """XUMM payload for NFTokenCreateOffer listing an NFT for sale on the
    in-app marketplace. `amount` must already be wire-shaped: an integer-drops
    string for a character listing (see `market_ops.xrp_to_drops_str`) or a
    validated BRIX IssuedCurrencyAmount dict for a trait listing (#239, see
    `market_ops.brix_amount_dict`) — no float/Decimal handling here. Flags=1
    marks a sell offer; Owner is omitted (only meaningful when someone other
    than the token owner creates the offer) and Destination is omitted so the
    listing is open to any buyer rather than locked to one counterparty.

    ``user_token`` (issue #135) push-delivers the request to a known user."""
    return await _create_xumm_payload(
        {
            "TransactionType": "NFTokenCreateOffer",
            "Account": account,
            "NFTokenID": nft_id,
            "Amount": amount,
            "Flags": 1,
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(
            memos.INITIATOR_USER, platform, memos.ACTION_LIST, campaign
        ),
    )


async def create_buy_offer_payload(
    account: str,
    nft_id: str,
    owner: str,
    amount_drops: str,
    expiration: int,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """#283: XUMM payload for a native BUY offer (bid) on a character NFT.
    Flags=0 (no lsfSellNFToken) marks the buy side; `owner` is REQUIRED by
    the ledger for buy offers (the current NFT holder); `amount_drops` is a
    wire-shaped integer-drops string; `expiration` is a Ripple-epoch seconds
    timestamp and is ALWAYS set — bids escrow nothing (funds are checked only
    at accept), so an unexpiring stale bid would linger as ledger junk and a
    misleading UI row. No Destination: the bid is acceptable by whoever owns
    the token when it is accepted."""
    return await _create_xumm_payload(
        {
            "TransactionType": "NFTokenCreateOffer",
            "Account": account,
            "NFTokenID": nft_id,
            "Owner": owner,
            "Amount": amount_drops,
            "Flags": 0,
            "Expiration": expiration,
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(
            memos.INITIATOR_USER, platform, memos.ACTION_BID, campaign
        ),
    )


async def create_accept_buy_offer_payload(
    offer_id: str,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """#283: XUMM payload for the OWNER accepting a buy offer (bid) —
    NFTokenAcceptOffer keyed by NFTokenBuyOffer instead of NFTokenSellOffer
    (create_accept_offer_payload's sell-side twin)."""
    return await _create_xumm_payload(
        {
            "TransactionType": "NFTokenAcceptOffer",
            "NFTokenBuyOffer": offer_id,
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(
            memos.INITIATOR_USER, platform, memos.ACTION_BID_ACCEPT, campaign
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


async def create_onramp_payment_payload(
    account: str,
    brix_amount: dict[str, str],
    send_max_drops: str,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
) -> dict[str, Any] | None:
    """XUMM payload for the trait-buy XRP→BRIX on-ramp (#239): a
    cross-currency Payment the buyer signs to THEMSELVES — Account and
    Destination are both `account`, `Amount` is the listing's BRIX
    IssuedCurrencyAmount (`market_ops.brix_amount_dict` shape), and `SendMax`
    is the buffered AMM XRP quote in drops — which buys the BRIX out of the
    AMM into their own wallet. No custody: if the buyer abandons after
    signing, they simply keep the BRIX.

    SourceTag + provenance memos (`action=payment`, user-initiated) are
    stamped by `_create_xumm_payload` like every other payload; ``user_token``
    (issue #135) push-delivers the request to a known user."""
    return await _create_xumm_payload(
        {
            "TransactionType": "Payment",
            "Account": account,
            "Destination": account,
            "Amount": brix_amount,
            "SendMax": send_max_drops,
        },
        options=_with_return_url({}, return_url),
        user_token=user_token,
        memos_json=memos.build_memos_json(
            memos.INITIATOR_USER, platform, memos.ACTION_PAYMENT, campaign
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


async def cancel_xumm_payload(uuid: str) -> bool:
    """Cancel one open XUMM payload (DELETE /payload/{uuid}). Returns True
    only when XUMM confirms it cancelled; False for already-resolved/expired/
    opened payloads and for transport errors (safe to call blindly during a
    backlog cleanup — see scripts/cancel_xumm_payloads.py)."""
    if rate_limited():
        logging.warning(f"XUMM payload cancel {uuid} skipped: rate-limit cooldown active")
        return False
    try:
        response = await asyncio.to_thread(
            requests.delete,
            f"{config.XUMM_API_URL}/{uuid}",
            headers=_XUMM_HEADERS,
            timeout=10,
        )
        if getattr(response, "status_code", 200) == 429:
            _note_rate_limited("payload cancel")
            return False
        _check_rate_headers(response, "payload cancel")
        data = response.json()
    except Exception as e:
        logging.warning(f"XUMM payload cancel {uuid} failed: {e}")
        return False
    result = data.get("result", {}) if isinstance(data, dict) else {}
    cancelled = bool(result.get("cancelled"))
    logging.info(f"XUMM payload cancel {uuid}: cancelled={cancelled} reason={result.get('reason')}")
    return cancelled


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


# --- payload status: cached + event-driven (no polling of the XUMM REST API) --
#
# Every surface polls OUR status endpoints every ~3s; those used to translate
# 1:1 into XUMM REST GETs (the main quota drain behind the 2026-07-17 429s,
# and explicitly what XUMM says disqualifies an app from raised limits). Now:
#   - results are cached per uuid; terminal states (signed/expired) forever,
#     non-terminal ones for XUMM_STATUS_CACHE_SECONDS;
#   - every created payload gets a websocket watcher (wss://xumm.app/sign/<uuid>,
#     XUMM's sanctioned push channel) that refreshes the cache when the payload
#     is opened/signed/expired — while a watcher is live, cached non-terminal
#     status is served regardless of age, so XUMM sees ~2 REST calls per payload
#     instead of one per client tick;
#   - a 429 sets the shared cooldown and serves stale cache instead of retrying.
_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STATUS_CACHE_SECONDS = float(os.getenv("XUMM_STATUS_CACHE_SECONDS", "4.0"))
_STATUS_CACHE_MAX = 512
_WS_BASE = os.getenv("XUMM_WS_BASE", "wss://xumm.app/sign")
_WS_WATCH_ENABLED = os.getenv("XUMM_WS_WATCH", "1") == "1"
_WATCH_LIFETIME = 900.0  # give up after the standard payload TTL
_watch_tasks: dict[str, "asyncio.Task[None]"] = {}
_watched: set[str] = set()  # uuids with a LIVE ws feed (cache is event-fresh)


def _cache_status(uuid: str, s: dict[str, Any]) -> None:
    if len(_STATUS_CACHE) >= _STATUS_CACHE_MAX:
        # Evict oldest entries; payloads expire in 900s so anything old is dead.
        for old, _ in sorted(_STATUS_CACHE.items(), key=lambda kv: kv[1][0])[:64]:
            del _STATUS_CACHE[old]
    _STATUS_CACHE[uuid] = (time.monotonic(), s)


def _terminal(s: dict[str, Any]) -> bool:
    return bool(s.get("signed") or s.get("expired"))


def cached_status(uuid: str) -> dict[str, Any] | None:
    """The cached status for a payload, if any — no XUMM call ever. Lets the
    service cheaply skip e.g. already-signed payloads when deciding whether a
    pending sign-in can be re-served."""
    cached = _STATUS_CACHE.get(uuid)
    return cached[1] if cached else None


def watch_payload(uuid: str) -> None:
    """Start (once) a websocket watcher that keeps this payload's cached
    status fresh from XUMM's push channel. Best-effort: no running loop, a
    failed connect, or the env kill-switch just leaves the throttled REST
    fallback in charge."""
    if not _WS_WATCH_ENABLED or uuid in _watch_tasks:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_watch_payload(uuid))
    _watch_tasks[uuid] = task

    def _done(_t: "asyncio.Task[None]") -> None:
        _watch_tasks.pop(uuid, None)
        _watched.discard(uuid)

    task.add_done_callback(_done)


async def _watch_payload(uuid: str) -> None:
    try:
        # wait_for, not asyncio.timeout: the deployed venvs run Python 3.10.
        await asyncio.wait_for(_watch_payload_inner(uuid), timeout=_WATCH_LIFETIME)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        logging.debug(f"XUMM ws watcher for {uuid} ended: {e}")


async def _watch_payload_inner(uuid: str) -> None:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{_WS_BASE}/{uuid}", heartbeat=30) as ws:
            _watched.add(uuid)
            # Seed the cache so pollers are served without a fetch each.
            s = await get_payload_status(uuid, force=True)
            if s and _terminal(s):
                return
            async for msg in ws:
                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue  # ignore stray binary/ping frames, don't kill the feed
                try:
                    event = json.loads(msg.data)
                except ValueError:
                    continue
                if not any(k in event for k in ("opened", "signed", "expired")):
                    continue
                s = await get_payload_status(uuid, force=True)
                if s and _terminal(s):
                    return


async def get_payload_status(uuid: str, *, force: bool = False) -> dict[str, Any] | None:
    """Status of a XUMM payload: whether it was opened (QR scanned) / signed /
    expired, and the signing account once signed. Served from the event-fed
    cache when possible (see above); force=True bypasses the cache and the
    freshness throttle (the ws watcher uses it on events). None on API errors
    or a malformed uuid (which is interpolated into the API URL)."""
    if not (isinstance(uuid, str) and _UUID_RE.match(uuid)):
        logging.error(f"Invalid XUMM payload uuid: {uuid!r}")
        return None
    cached = _STATUS_CACHE.get(uuid)
    if cached and not force:
        age = time.monotonic() - cached[0]
        if _terminal(cached[1]) or uuid in _watched or age < _STATUS_CACHE_SECONDS:
            return cached[1]
    if rate_limited() and not force:
        # Cooldown: a stale answer beats another 429.
        return cached[1] if cached else None
    try:
        response = await asyncio.to_thread(
            requests.get, f"{config.XUMM_API_URL}/{uuid}", headers=_XUMM_HEADERS, timeout=10
        )
        if getattr(response, "status_code", 200) == 429:
            _note_rate_limited("payload status")
            return cached[1] if cached else None
        _check_rate_headers(response, "payload status")
        data = response.json()
        meta = data.get("meta") or {}
        response_block = data.get("response") or {}
        application = data.get("application") or {}
        s = {
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
        _cache_status(uuid, s)
        return s
    except Exception as e:
        logging.error(f"Error fetching XUMM payload status: {e}")
        return None
