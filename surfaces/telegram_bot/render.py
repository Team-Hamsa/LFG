# surfaces/telegram_bot/render.py
# Pure caption + photo builders for the Telegram mint flow. Telegram has no
# embeds; plain-text captions (no parse_mode, to avoid MarkdownV2 escaping
# pitfalls) carry the links, and photos are sent as InputFile bytes. Trivially
# unit-testable with no SDK/XRPL involvement.
import io
from typing import Any

from telegram import InputFile


def _push_hint(push: Any) -> str:
    """#212: one-line delivery hint. 'sent' = the sign request was pushed to
    the user's Xaman app; 'failed' = a push was attempted but not delivered
    (the request still shows under Xaman's Events list); anything else = plain
    QR sign, no hint."""
    if push == "sent":
        return "📲 Also sent straight to your Xaman app — you can just approve it there.\n"
    if push == "failed":
        return "(You can also find this request under Events in Xaman.)\n"
    return ""


def payment_caption(payment_link: str, push: Any = None) -> str:
    return (
        "💰 Token Payment Required\n\n"
        "Pay 1 token to mint your NFT:\n"
        "1. Scan the QR with your XRPL wallet (XUMM/Xaman)\n"
        "2. Approve the payment\n"
        "3. Wait for confirmation\n\n"
        f"Open payment link: {payment_link}\n"
        f"{_push_hint(push)}"
        "(expires in 5 minutes)"
    )


def free_mint_caption() -> str:
    return (
        "🎉 Free mint\n\n"
        "You're a newcomer — this one's on us. No payment needed.\n"
        "Building your avatar now… hang tight here."
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
        f"{_push_hint(final.get('accept_push'))}"
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


def linked_caption(summary: str) -> str:
    """Confirmation for a completed cross-surface link (#90). ``summary`` comes
    from surfaces._shared.account_result.linked_summary."""
    return f"✅ {summary}"


def photo_input(data: bytes, filename: str) -> InputFile:
    return InputFile(io.BytesIO(data), filename=filename)


def is_video_url(url: Any) -> bool:
    """True when the media URL points at an MP4 (animated NFTs upload a
    `<basename>.mp4` next to the PNG poster frame)."""
    if not url:
        return False
    return str(url).split("?", 1)[0].lower().endswith(".mp4")


async def send_media(bot: Any, chat_id: Any, media_url: str, caption: str | None = None) -> None:
    """Send artwork by URL, as a playing video when it's an MP4 and as a
    photo otherwise — a static send_photo of an MP4 URL is what made animated
    mints show up frozen on Telegram."""
    if is_video_url(media_url):
        # supports_streaming: without it Telegram mobile buffers the whole
        # file (download spinner) instead of playing the MP4 inline.
        await bot.send_video(chat_id, video=media_url, caption=caption, supports_streaming=True)
    else:
        await bot.send_photo(chat_id, photo=media_url, caption=caption)
