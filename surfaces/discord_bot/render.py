# surfaces/discord_bot/render.py
# Embed/QR builders shared by the inverted mint handler. Pure functions: given
# the session dicts the service returns (lfg_core.mint_flow.MintSession.to_dict)
# they build discord.Embed / discord.File objects, so they are trivially
# unit-testable with no SDK or XRPL involvement.
import io
from typing import Any

import discord
from discord import Embed


def _push_hint(push: Any) -> str:
    """#212: one-line delivery hint. 'sent' = the sign request was pushed to
    the user's Xaman app; 'failed' = a push was attempted but not delivered
    (the request still shows under Xaman's Events list); anything else = plain
    QR sign, no hint."""
    if push == "sent":
        return "\n📲 Also sent straight to your Xaman app — you can just approve it there.\n"
    if push == "failed":
        return "\n(You can also find this request under Events in Xaman.)\n"
    return ""


def payment_embed(payment_link: str, push: Any = None) -> Embed:
    """Step-1 embed. The payment QR is always rendered locally from the
    deeplink and attached, so the image points at the attachment."""
    embed = Embed(
        title="💰 Token Payment Required",
        description=(
            "Please pay 1 token to mint your NFT.\n\n"
            "**Steps:**\n"
            "1. Scan the QR code with your XRPL wallet (XUMM, Xaman, etc.)\n"
            "2. Approve the payment\n"
            "3. Wait for confirmation\n\n"
            f"[Open Payment Link]({payment_link})"
            f"{_push_hint(push)}"
        ),
        color=0x00FF00,
    )
    embed.set_footer(text="Payment request expires in 5 minutes")
    embed.set_image(url="attachment://payment_qr.png")
    return embed


def free_mint_embed() -> Embed:
    """Step-1 embed for a newcomer free mint: no payment, no QR. Wallet control
    is already proven at connect, so we just tell the user the build is running."""
    embed = Embed(
        title="🎉 Free mint",
        description=(
            "You're a newcomer — this one's on us. No payment needed.\n\n"
            "Building your avatar now… hang tight here."
        ),
        color=0x00FF00,
    )
    return embed


def offer_embed(final: dict[str, Any], qr_image_url: str) -> Embed:
    """Terminal-success embed. ``qr_image_url`` is either the service-hosted
    ``accept_qr_url`` or ``attachment://offer_qr.png`` when the handler had to
    render the accept deeplink itself."""
    number = final.get("nft_number", "?")
    accept_link = final.get("accept_deeplink", "")
    embed = Embed(
        title="🎨 NFT Minted Successfully!",
        description=(
            "Your NFT has been minted and an offer has been created!\n\n"
            f"**NFT Number:** #{number}\n"
            "**To claim your NFT:**\n"
            "1. Scan the QR code with XUMM\n"
            "2. Review and accept the offer\n"
            "3. Your NFT will appear in your wallet!\n\n"
            f"[Open in XUMM]({accept_link})"
            f"{_push_hint(final.get('accept_push'))}"
        ),
        color=0x00FF00,
    )
    embed.set_image(url=qr_image_url)
    embed.set_footer(text="Offer acceptance request expires in 24 hours")
    return embed


def artwork_embed(final: dict[str, Any]) -> Embed | None:
    """Large standalone embed showing the minted artwork to the minter (#86).
    Returns None when the session carries no image_url."""
    image_url = final.get("image_url")
    if not image_url:
        return None
    embed = Embed(title=f"🖼️ Your NFT #{final.get('nft_number', '?')}", color=0x00FF00)
    embed.set_image(url=image_url)
    return embed


def signin_embed(signin_link: str) -> Embed:
    embed = Embed(
        title="🔐 Verify your wallet with Xaman",
        description=(
            "Scan the QR with Xaman and approve the sign-in — your wallet "
            "address is captured on approval, nothing to type.\n\n"
            f"[Open in Xaman]({signin_link})"
        ),
        color=0x00FF00,
    )
    embed.set_image(url="attachment://signin_qr.png")
    embed.set_footer(text="The sign-in request expires after a few minutes")
    return embed


def linked_embed(summary: str) -> Embed:
    """Confirmation for a completed cross-surface link (#90). ``summary`` comes
    from surfaces._shared.account_result.linked_summary."""
    return Embed(title="✅ Linked to your account", description=summary, color=0x00FF00)


def error_embed(message: str, *, title: str = "⚠️ Mint failed") -> Embed:
    return Embed(title=title, description=message, color=0xFF0000)


def file_from_png(data: bytes, filename: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=filename)
