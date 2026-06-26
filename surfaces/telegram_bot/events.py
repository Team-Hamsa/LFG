# surfaces/telegram_bot/events.py
# Background firehose consumer: announces mint.completed / mint.failed to the
# configured channel and DMs the minter on success. The /events firehose is
# cross-surface, so the DM is gated on identity.platform == "telegram".
import logging
from collections.abc import Awaitable, Callable

from lfg_service.events import Event
from surfaces._client import LFGServiceClient

_MINT_EVENT_TYPES = ["mint.completed", "mint.failed"]


def _is_telegram(ev: Event) -> bool:
    return (ev.identity or {}).get("platform") == "telegram"


def make_announcement(ev: Event) -> str:
    data = ev.data or {}
    number = data.get("nft_number", "?")
    if ev.type == "mint.completed":
        return f"🎨 NFT #{number} minted for a user."
    return f"❌ Mint failed for a user (#{number})."


def announcement_image(ev: Event) -> str | None:
    """The minted artwork URL to attach to a completed-mint announcement, or
    None for failures / events that carry no image."""
    if ev.type == "mint.completed":
        return (ev.data or {}).get("image_url")
    return None


async def run_event_loop(
    svc: LFGServiceClient,
    announce: Callable[[str, str | None], Awaitable[None]],
    dm_user: Callable[[str, str, str | None], Awaitable[None]] | None = None,
) -> None:
    """Consume the service firehose forever. The SDK reconnects internally;
    cancel the enclosing task to stop (finally aclose()s the generator)."""
    agen = svc.events(types=_MINT_EVENT_TYPES)
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
