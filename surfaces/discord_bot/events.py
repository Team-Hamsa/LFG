# surfaces/discord_bot/events.py
# Background firehose consumer: announces mint.completed / mint.failed to the
# admin-log channel and DMs the minter on success. Additive to the interactive
# wait_for_mint path (spec D4) — this is the out-of-band notifier.
#
# The /events firehose is cross-surface (service-token scope), so it can carry
# mints from the webapp too. The discord-specific bits — the <@id> mention and
# the DM via bot.fetch_user — are gated on identity.platform == "discord".
import logging
from collections.abc import Awaitable, Callable

from lfg_service.events import Event
from surfaces._client import LFGServiceClient

# Every in-process NFT interaction the service publishes (#91). Burns are NOT
# here (out-of-process / covered by swap.*); the X surface is deferred to #41.
_ANNOUNCE_EVENT_TYPES = [
    "mint.completed",
    "mint.failed",
    "swap.completed",
    "swap.failed",
    "harvest.completed",
    "harvest.failed",
    "assemble.completed",
    "assemble.failed",
    "equip.completed",
    "equip.failed",
]


def _is_discord(ev: Event) -> bool:
    return (ev.identity or {}).get("platform") == "discord"


def _minter_display(ev: Event) -> str:
    """Who to credit in the channel. Prefer a real Discord ping: the event's own
    discord identity, else a linked discord identity (so a webapp/telegram mint
    by someone who ALSO linked Discord still pings them here). Otherwise fall
    back to the minter's display_handle, then the wallet, then a generic name."""
    ident = ev.identity or {}
    uid = ident.get("platform_user_id")
    if _is_discord(ev) and uid:
        return f"<@{uid}>"
    for link in ident.get("linked") or []:
        if link.get("platform") == "discord" and link.get("platform_user_id"):
            return f"<@{link['platform_user_id']}>"
    handle = ident.get("display_handle")
    if handle:
        return str(handle)
    if ev.wallet:
        return str(ev.wallet)
    return "a user"


def make_announcement(ev: Event) -> str:
    data = ev.data or {}
    number = data.get("nft_number", "?")
    who = _minter_display(ev)
    if ev.type == "mint.completed":
        return f"🎨 NFT #{number} minted by {who}."
    if ev.type == "mint.failed":
        return f"❌ Mint failed for {who} (#{number})."
    if ev.type == "swap.completed":
        return f"🔄 {who} swapped traits."
    if ev.type == "swap.failed":
        return f"❌ {who}'s trait swap failed."
    if ev.type == "assemble.completed":
        return f"🛠️ {who} assembled a new character."
    if ev.type == "assemble.failed":
        return f"❌ {who}'s assemble failed."
    if ev.type == "harvest.completed":
        return f"🌾 {who} harvested a character into their bucket."
    if ev.type == "harvest.failed":
        return f"❌ {who}'s harvest failed."
    if ev.type == "equip.completed":
        return f"👕 {who} equipped a trait."
    if ev.type == "equip.failed":
        return f"❌ {who}'s equip failed."
    logging.warning("make_announcement: unhandled event type %r", ev.type)
    return f"❌ Unknown event for {who}."


def announcement_image(ev: Event) -> str | None:
    """The artwork URL to attach to a completed announcement, or None for
    failures / events that carry no image. The service normalizes the image
    onto a uniform top-level data.image_url across interactions."""
    if ev.type.endswith(".completed"):
        return (ev.data or {}).get("image_url")
    return None


async def run_event_loop(
    svc: LFGServiceClient,
    announce: Callable[[str, str | None], Awaitable[None]],
    dm_user: Callable[[str, str, str | None], Awaitable[None]] | None = None,
) -> None:
    """Consume the service firehose forever. The SDK reconnects internally;
    cancel the enclosing task to stop (the finally block aclose()s the
    generator to release the WebSocket)."""
    agen = svc.events(types=_ANNOUNCE_EVENT_TYPES)
    try:
        async for ev in agen:
            try:
                message = make_announcement(ev)
                image = announcement_image(ev)
                await announce(message, image)
                if dm_user is not None and ev.type == "mint.completed" and _is_discord(ev):
                    uid = (ev.identity or {}).get("platform_user_id")
                    if uid:
                        await dm_user(uid, message, image)
            except Exception as e:  # never let one bad event kill the loop
                logging.error(f"event handler error: {e}")
    finally:
        await agen.aclose()
