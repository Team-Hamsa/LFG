"""Protocol primitives for payment-first XRPL mint actions.

This module deliberately owns no service or UI state.  It is the fail-closed
boundary for amendment capabilities, issuer Ticket discovery, and the ledger
key derivation needed to reference an NFToken offer created earlier in the
same Batch.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from xrpl.clients import JsonRpcClient
from xrpl.core.addresscodec import decode_classic_address
from xrpl.models.requests import AccountObjects, AccountObjectType, Feature

BATCH_V1_1_ID = "9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377"
NFTOKEN_MINT_OFFER_ID = (
    "EE3CF852F0506782D05E65D49E5DCC3D16D50898CD1B646BAE274863401CC3CE"
)
OBSOLETE_BATCH_ID = (
    "894646DD5284E97DECFE6674A6D6152686791C4A95F8C132CCA9BAF9E5812FB6"
)
NFTOKEN_OFFER_NAMESPACE = 0x0071


@dataclass(frozen=True)
class BatchCapability:
    enabled: bool
    reason: str | None


def nft_offer_id(account: str, sequence_or_ticket: int) -> str:
    """Return the NFToken offer ledger index for an account and sequence.

    NFTokenMintOffer lets the mint transaction create this offer.  Leasing a
    Ticket makes its identifying sequence stable while the buyer reviews the
    interactive Batch, so the later accept leg can reference this key up front.
    """

    if (
        isinstance(sequence_or_ticket, bool)
        or not isinstance(sequence_or_ticket, int)
        or not 0 <= sequence_or_ticket <= 0xFFFFFFFF
    ):
        raise ValueError("sequence_or_ticket must be a uint32")
    account_id = decode_classic_address(account)
    payload = (
        NFTOKEN_OFFER_NAMESPACE.to_bytes(2, "big")
        + account_id
        + sequence_or_ticket.to_bytes(4, "big")
    )
    return hashlib.sha512(payload).digest()[:32].hex().upper()


def evaluate_capabilities(
    rows: Mapping[str, Mapping[str, Any]], *, configured: bool
) -> BatchCapability:
    """Evaluate the exact amendment matrix; configuration can only close it."""

    if not configured:
        return BatchCapability(False, "action_disabled")
    obsolete = rows.get(OBSOLETE_BATCH_ID, {})
    if obsolete.get("enabled"):
        return BatchCapability(False, "obsolete_batch_enabled")
    batch = rows.get(BATCH_V1_1_ID, {})
    mint_offer = rows.get(NFTOKEN_MINT_OFFER_ID, {})
    if not batch.get("supported") or not batch.get("enabled"):
        return BatchCapability(False, "batch_unavailable")
    if not mint_offer.get("supported") or not mint_offer.get("enabled"):
        return BatchCapability(False, "mint_offer_unavailable")
    return BatchCapability(True, None)


async def fetch_batch_capability(
    client: JsonRpcClient, *, configured: bool
) -> BatchCapability:
    """Read `feature` rows in rippled's map-by-amendment response shape."""

    if not configured:
        return BatchCapability(False, "action_disabled")
    rows: dict[str, Mapping[str, Any]] = {}
    for amendment_id in (
        BATCH_V1_1_ID,
        NFTOKEN_MINT_OFFER_ID,
        OBSOLETE_BATCH_ID,
    ):
        unavailable = (
            "mint_offer_unavailable"
            if amendment_id == NFTOKEN_MINT_OFFER_ID
            else "batch_unavailable"
        )
        try:
            response = await asyncio.to_thread(
                client.request, Feature(feature=amendment_id)
            )
        except Exception:
            return BatchCapability(False, unavailable)
        result = response.result if isinstance(response.result, dict) else {}
        row = result.get(amendment_id)
        if not isinstance(row, dict):
            return BatchCapability(False, unavailable)
        rows[amendment_id] = row
    return evaluate_capabilities(rows, configured=True)


async def list_ticket_sequences(client: JsonRpcClient, account: str) -> list[int]:
    """Return validated TicketSequence values currently owned by `account`."""

    response = await asyncio.to_thread(
        client.request,
        AccountObjects(
            account=account,
            type=AccountObjectType.TICKET,
            ledger_index="validated",
        ),
    )
    result = response.result if isinstance(response.result, dict) else {}
    objects = result.get("account_objects", [])
    if not isinstance(objects, list):
        return []
    return sorted(
        obj["TicketSequence"]
        for obj in objects
        if isinstance(obj, dict)
        and obj.get("LedgerEntryType") == "Ticket"
        and isinstance(obj.get("TicketSequence"), int)
        and not isinstance(obj.get("TicketSequence"), bool)
    )
