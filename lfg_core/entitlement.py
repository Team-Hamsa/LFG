# lfg_core/entitlement.py
# Entitlement seam for bulk minting (#215): the fulfillment loop reads how many
# mints a user is owed (`quantity`) without caring WHY. `payment` is built now;
# `burn` (#220, "infinite" minting past the cap) is a documented stub.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaymentEntitlement:
    quantity: int
    source: str = field(default="payment", init=False)
    cap_exempt: bool = field(default=False, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"source": "payment", "quantity": self.quantity}


@dataclass
class BurnEntitlement:
    quantity: int
    burn_nft_ids: list[str]
    # Burning M live NFTs to mint M fresh ones is supply-neutral, so it is
    # exempt from MAX_COLLECTION_SIZE.
    source: str = field(default="burn", init=False)
    cap_exempt: bool = field(default=True, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"source": "burn", "quantity": self.quantity, "burn_nft_ids": self.burn_nft_ids}


def from_dict(d: dict[str, Any]) -> PaymentEntitlement | BurnEntitlement:
    if d["source"] == "payment":
        return PaymentEntitlement(quantity=d["quantity"])
    if d["source"] == "burn":
        return BurnEntitlement(quantity=d["quantity"], burn_nft_ids=d["burn_nft_ids"])
    raise ValueError(f"unknown entitlement source: {d['source']!r}")


def build_burn_entitlement(burn_nft_ids: list[str]) -> BurnEntitlement:
    """Convert VALIDATED burns into a cap-exempt mint entitlement (#220).

    `burn_nft_ids` must contain only NFTs whose user-signed NFTokenBurn was
    confirmed validated + tesSUCCESS on-ledger (lfg_core/burn2mint_flow.py is
    the only caller and enforces that). quantity == the validated burn count,
    never the requested count: an unsigned/failed burn earns no mint. The
    resulting entitlement is `cap_exempt` — burning M live NFTs to mint M
    fresh ones is supply-neutral, so the headroom clamp/reservation
    (MAX_COLLECTION_SIZE, #226) is skipped entirely."""
    if not burn_nft_ids:
        raise ValueError("burn entitlement requires at least one validated burn")
    if len(set(burn_nft_ids)) != len(burn_nft_ids):
        raise ValueError("burn entitlement nft_ids must be unique")
    return BurnEntitlement(quantity=len(burn_nft_ids), burn_nft_ids=list(burn_nft_ids))
