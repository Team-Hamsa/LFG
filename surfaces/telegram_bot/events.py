# surfaces/telegram_bot/events.py
# Background firehose consumer: announces mint.completed / mint.failed to the
# configured channel and DMs the minter on success. The /events firehose is
# cross-surface, so the DM is gated on identity.platform == "telegram".
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


def _is_telegram(ev: Event) -> bool:
    return (ev.identity or {}).get("platform") == "telegram"


def _minter_display(ev: Event) -> str:
    """A human name for the minter. Telegram cannot @-mention by numeric id, so
    we never build a mention — we prefer the minter's own display_handle, then
    any linked surface's handle, then the wallet, then a generic fallback."""
    ident = ev.identity or {}
    handle = ident.get("display_handle")
    if handle:
        return str(handle)
    for link in ident.get("linked") or []:
        if link.get("display_handle"):
            return str(link["display_handle"])
    if ev.wallet:
        return str(ev.wallet)
    return "a user"


def make_announcement(ev: Event) -> str:
    data = ev.data or {}
    number = data.get("nft_number", "?")
    name = _minter_display(ev)
    if ev.type == "mint.completed":
        return f"🎨 NFT #{number} minted by {name}."
    if ev.type == "mint.failed":
        return f"❌ Mint failed for {name} (#{number})."
    if ev.type == "swap.completed":
        return f"🔄 {name} swapped traits."
    if ev.type == "swap.failed":
        return f"❌ {name}'s trait swap failed."
    if ev.type == "assemble.completed":
        return f"🛠️ {name} dressed a blank into #{data.get('edition', '?')}."
    if ev.type == "assemble.failed":
        return f"❌ {name}'s assemble failed."
    if ev.type == "harvest.completed":
        return f"🌾 {name} stripped a character down to a blank."
    if ev.type == "harvest.failed":
        return f"❌ {name}'s harvest failed."
    if ev.type == "equip.completed":
        return f"👕 {name} equipped a trait."
    if ev.type == "equip.failed":
        return f"❌ {name}'s equip failed."
    logging.warning("make_announcement: unhandled event type %r", ev.type)
    return f"❌ Unknown event for {name}."


def announcement_image(ev: Event) -> str | None:
    """The artwork URL to attach to a completed announcement, or None for
    failures / events that carry no image. The service normalizes the artwork
    onto uniform top-level data.image_url / data.video_url across
    interactions; prefer the MP4 so animated NFTs play instead of showing
    the static poster frame."""
    if ev.type.endswith(".completed"):
        data = ev.data or {}
        return data.get("video_url") or data.get("image_url")
    return None


async def run_event_loop(
    svc: LFGServiceClient,
    announce: Callable[[str, str | None], Awaitable[None]],
    dm_user: Callable[[str, str, str | None], Awaitable[None]] | None = None,
) -> None:
    """Consume the service firehose forever. The SDK reconnects internally;
    cancel the enclosing task to stop (finally aclose()s the generator)."""
    agen = svc.events(types=_ANNOUNCE_EVENT_TYPES)
    try:
        async for ev in agen:
            try:
                message = make_announcement(ev)
                image = announcement_image(ev)
                await announce(message, image)
                if dm_user is not None and ev.type == "mint.completed" and _is_telegram(ev):
                    uid = (ev.identity or {}).get("platform_user_id")
                    if uid:
                        await dm_user(uid, message, image)
            except Exception as e:  # never let one bad event kill the loop
                logging.error(f"event handler error: {e}")
    finally:
        await agen.aclose()
