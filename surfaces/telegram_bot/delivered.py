# surfaces/telegram_bot/delivered.py
# Which mint sessions this surface's own chat handler owns the artwork for (#591).
#
# Two paths deliver a completed mint's artwork to a Telegram user: handle_mint
# (which posts "Your NFT #N" in chat, before the claim QR) and the firehose
# consumer in events.py (which DMs the minter the "NFT #N minted by ..."
# announcement with the same media). Both fire for a /mint run in the bot's DM,
# so the image landed twice.
#
# handle_mint claims its session id here as soon as start_mint returns — well
# before the terminal event can be published, so the firehose can never win the
# race — and releases it again if it bails out without showing the artwork.
# Mini App mints (#89) are platform="telegram" too but never claim a session,
# so their DM, which is that user's only delivery, is untouched.
#
# Process-local and bounded: a bot restart mid-mint just falls back to the old
# behaviour (one extra DM), which is the safe direction to fail in.
from collections import OrderedDict

# Sessions are short-lived and ids are never reused, so this only has to outlive
# the in-flight mints of one process.
MAX_TRACKED = 256

_owned: OrderedDict[str, None] = OrderedDict()


def mark(session_id: str) -> None:
    """Claim `session_id` for the chat handler — the firehose skips its DM."""
    if not session_id:
        return
    _owned[session_id] = None
    _owned.move_to_end(session_id)
    while len(_owned) > MAX_TRACKED:
        _owned.popitem(last=False)


def release(session_id: str) -> None:
    """Give the claim back: the chat handler is not going to show the artwork."""
    _owned.pop(session_id, None)


def owns(session_id: str) -> bool:
    """True while the chat handler owns this session's artwork delivery.

    Deliberately non-consuming: the service accepts a sub-tick double-publish
    of the terminal event (see _publish_mint_terminal), and both copies must
    stay suppressed.
    """
    return bool(session_id) and session_id in _owned
