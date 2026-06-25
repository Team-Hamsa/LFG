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

_MINT_EVENT_TYPES = ["mint.completed", "mint.failed"]


def _is_discord(ev: Event) -> bool:
    return (ev.identity or {}).get("platform") == "discord"


def make_announcement(ev: Event) -> str:
    data = ev.data or {}
    number = data.get("nft_number", "?")
    uid = (ev.identity or {}).get("platform_user_id")
    who = f"<@{uid}>" if (_is_discord(ev) and uid) else "a user"
    if ev.type == "mint.completed":
        return f"🎨 NFT #{number} minted for {who}."
    return f"❌ Mint failed for {who} (#{number})."


async def run_event_loop(
    svc: LFGServiceClient,
    announce: Callable[[str], Awaitable[None]],
    dm_user: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Consume the service firehose forever. The SDK reconnects internally;
    cancel the enclosing task to stop (the finally block aclose()s the
    generator to release the WebSocket)."""
    agen = svc.events(types=_MINT_EVENT_TYPES)
    try:
        async for ev in agen:
            try:
                message = make_announcement(ev)
                await announce(message)
                if dm_user is not None and ev.type == "mint.completed" and _is_discord(ev):
                    uid = (ev.identity or {}).get("platform_user_id")
                    if uid:
                        await dm_user(uid, message)
            except Exception as e:  # never let one bad event kill the loop
                logging.error(f"event handler error: {e}")
    finally:
        await agen.aclose()
