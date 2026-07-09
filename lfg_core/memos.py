# lfg_core/memos.py
# Provenance Memos (#54): human-readable who/what/where attribution stamped onto
# every XRPL transaction the app builds, alongside the Make Waves SourceTag
# (#61). SourceTag is a single assigned UInt32 — it identifies the contest
# entrant but cannot encode initiator/platform/action; Memos carry that
# provenance for the jury and our own analytics.
#
# This module is the single source of truth for the schema. Values are a CLOSED
# enum (constants below) — call sites pass the constants, never free strings, so
# a typo fails loudly here instead of writing an unattributable memo on-chain.
# The same schema is emitted in the two wire shapes the app needs:
#   * build_memos_json  -> XUMM txjson "Memos" array (for user-signed payloads)
#   * build_memo_models -> list[xrpl.models.transactions.Memo] (backend-signed)

import re
from typing import Any

from xrpl.models.transactions import Memo
from xrpl.utils import str_to_hex

# --- initiator: who signed / triggered the transaction ----------------------
INITIATOR_USER = "user"
INITIATOR_BACKEND = "backend"
_INITIATORS = frozenset({INITIATOR_USER, INITIATOR_BACKEND})

# --- platform: originating surface ------------------------------------------
PLATFORM_DISCORD_BOT = "discord-bot"
PLATFORM_DISCORD_ACTIVITY = "discord-activity"
PLATFORM_TELEGRAM = "telegram"
PLATFORM_TWITTER = "twitter"
PLATFORM_WEBAPP = "webapp"
# Backend-signed transactions with no user surface (listener/admin/service ops).
PLATFORM_BACKEND = "backend"
_PLATFORMS = frozenset(
    {
        PLATFORM_DISCORD_BOT,
        PLATFORM_DISCORD_ACTIVITY,
        PLATFORM_TELEGRAM,
        PLATFORM_TWITTER,
        PLATFORM_WEBAPP,
        PLATFORM_BACKEND,
    }
)

# The service resolves a surface token to one of these short surface names
# ("discord"/"telegram"/…); map them onto the memo platform enum so call sites
# can hand us whatever they already have without re-deriving it.
_SURFACE_TO_PLATFORM = {
    "discord": PLATFORM_DISCORD_ACTIVITY,
    "discord-bot": PLATFORM_DISCORD_BOT,
    "discord-activity": PLATFORM_DISCORD_ACTIVITY,
    "telegram": PLATFORM_TELEGRAM,
    "twitter": PLATFORM_TWITTER,
    "webapp": PLATFORM_WEBAPP,
    "backend": PLATFORM_BACKEND,
}

# --- action: the app-level operation ----------------------------------------
# Covers every transaction type the app actually builds/submits. The app has no
# Clawback path (issuer never claws back holder balances), so there is
# deliberately no `clawback` action — add one here if that ever changes.
ACTION_MINT = "mint"
ACTION_CREATE_OFFER = "create-offer"
ACTION_ACCEPT_OFFER = "accept-offer"
ACTION_CANCEL_OFFER = "cancel-offer"
ACTION_BURN = "burn"
ACTION_MODIFY = "modify"
ACTION_TRAIT_SWAP_FEE = "trait-swap-fee"
ACTION_BUY_AND_BURN = "buy-and-burn"
ACTION_TRUSTSET = "trustset"
ACTION_PAYMENT = "payment"
ACTION_LIST = "list"
ACTION_BUY = "buy"
ACTION_HARVEST = "harvest"
ACTION_ASSEMBLE = "assemble"
ACTION_EQUIP = "equip"
ACTION_EXTRACT = "extract"
ACTION_DEPOSIT = "deposit"
_ACTIONS = frozenset(
    {
        ACTION_MINT,
        ACTION_CREATE_OFFER,
        ACTION_ACCEPT_OFFER,
        ACTION_CANCEL_OFFER,
        ACTION_BURN,
        ACTION_MODIFY,
        ACTION_TRAIT_SWAP_FEE,
        ACTION_BUY_AND_BURN,
        ACTION_TRUSTSET,
        ACTION_PAYMENT,
        ACTION_LIST,
        ACTION_BUY,
        ACTION_HARVEST,
        ACTION_ASSEMBLE,
        ACTION_EQUIP,
        ACTION_EXTRACT,
        ACTION_DEPOSIT,
    }
)

MEMO_FORMAT = "text/plain"
_MEMO_FORMAT_HEX = str_to_hex(MEMO_FORMAT)

# `campaign` is the one non-enum memo field, so unlike initiator/platform/action
# it can't be a closed set. But it is written PERMANENTLY and PUBLICLY on-ledger,
# so it must be a constrained admin/config tag (lowercase slug, bounded length) —
# never free-form or user-derived text (PII / compliance exposure). A value
# outside this shape fails loudly here rather than being memorialized on-chain.
_CAMPAIGN_RE = re.compile(r"^[a-z0-9-]{1,32}$")

# XRPL caps the total Memos payload at ~1 KB per transaction; our short closed
# enum is far under this, but the builders assert it so an accidentally long
# campaign string fails here rather than as an on-ledger temMALFORMED.
MAX_MEMOS_BYTES = 1024


def platform_for_surface(surface: str | None) -> str:
    """Map a service surface name (as returned by auth.surface_for_token, e.g.
    "discord"/"telegram") to the memo platform enum. Unknown/None → backend."""
    if not surface:
        return PLATFORM_BACKEND
    return _SURFACE_TO_PLATFORM.get(surface, PLATFORM_BACKEND)


def _entries(
    initiator: str, platform: str, action: str, campaign: str | None
) -> list[tuple[str, str]]:
    if initiator not in _INITIATORS:
        raise ValueError(f"unknown memo initiator: {initiator!r}")
    if platform not in _PLATFORMS:
        raise ValueError(f"unknown memo platform: {platform!r}")
    if action not in _ACTIONS:
        raise ValueError(f"unknown memo action: {action!r}")
    entries = [("initiator", initiator), ("platform", platform), ("action", action)]
    if campaign:
        if not _CAMPAIGN_RE.match(campaign):
            raise ValueError(
                f"unsafe campaign tag {campaign!r}: expected an admin-controlled "
                "slug matching [a-z0-9-]{1,32} (memos are permanent & public on-ledger)"
            )
        entries.append(("campaign", campaign))
    return entries


def _assert_within_budget(pairs: list[tuple[str, str]]) -> None:
    total = 0
    for key, value in pairs:
        # MemoType + MemoData + MemoFormat bytes, per entry.
        total += len(key.encode("utf-8")) + len(value.encode("utf-8")) + len(MEMO_FORMAT)
    if total >= MAX_MEMOS_BYTES:
        raise ValueError(f"memo payload {total}B exceeds {MAX_MEMOS_BYTES}B limit")


def build_memos_json(
    initiator: str, platform: str, action: str, campaign: str | None = None
) -> list[dict[str, Any]]:
    """The XUMM txjson `Memos` array (hex-encoded), for user-signed payloads."""
    pairs = _entries(initiator, platform, action, campaign)
    _assert_within_budget(pairs)
    return [
        {
            "Memo": {
                "MemoType": str_to_hex(key),
                "MemoData": str_to_hex(value),
                "MemoFormat": _MEMO_FORMAT_HEX,
            }
        }
        for key, value in pairs
    ]


def build_memo_models(
    initiator: str, platform: str, action: str, campaign: str | None = None
) -> list[Memo]:
    """xrpl-py `Memo` models (hex-encoded), for backend-signed tx builders."""
    pairs = _entries(initiator, platform, action, campaign)
    _assert_within_budget(pairs)
    return [
        Memo(
            memo_type=str_to_hex(key),
            memo_data=str_to_hex(value),
            memo_format=_MEMO_FORMAT_HEX,
        )
        for key, value in pairs
    ]
