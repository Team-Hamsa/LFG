# surfaces/telegram_bot/render.py
# Pure caption + photo builders for the Telegram mint flow. Telegram has no
# embeds; plain-text captions (no parse_mode, to avoid MarkdownV2 escaping
# pitfalls) carry the links, and photos are sent as InputFile bytes. Trivially
# unit-testable with no SDK/XRPL involvement.
import io
from typing import Any

from telegram import InputFile


def payment_caption(payment_link: str) -> str:
    return (
        "💰 Token Payment Required\n\n"
        "Pay 1 token to mint your NFT:\n"
        "1. Scan the QR with your XRPL wallet (XUMM/Xaman)\n"
        "2. Approve the payment\n"
        "3. Wait for confirmation\n\n"
        f"Open payment link: {payment_link}\n"
        "(expires in 5 minutes)"
    )


def offer_caption(final: dict[str, Any], *, with_qr: bool = True) -> str:
    number = final.get("nft_number", "?")
    accept_link = final.get("accept_deeplink", "")
    if with_qr:
        step1 = "1. Scan the QR with XUMM"
    else:
        step1 = "1. Open the link below in XUMM"
    return (
        "🎨 NFT Minted Successfully!\n\n"
        f"NFT Number: #{number}\n\n"
        "To claim it:\n"
        f"{step1}\n"
        "2. Review and accept the offer\n"
        "3. Your NFT appears in your wallet\n\n"
        f"Open in XUMM: {accept_link}\n"
        "(offer expires in 24 hours)"
    )


def artwork_caption(final: dict[str, Any]) -> str:
    return f"🖼️ Your NFT #{final.get('nft_number', '?')}"


def error_caption(message: str) -> str:
    return f"⚠️ {message}"


def signin_caption(signin_link: str) -> str:
    return (
        "🔐 Verify your wallet with Xaman\n\n"
        "Scan the QR with Xaman (or open the link) and approve the sign-in.\n"
        "Your wallet address is captured on approval — nothing to type.\n\n"
        f"Open in Xaman: {signin_link}\n"
        "(the request expires after a few minutes)"
    )


def photo_input(data: bytes, filename: str) -> InputFile:
    return InputFile(io.BytesIO(data), filename=filename)
