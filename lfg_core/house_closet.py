"""House Closet (#548): the project's trait stock sold as Closet asks.

The app wallet is the issuer and cannot own a Closet (#383), so the trait tokens
it has listed are burned into a separate house wallet's Closet and relisted there
as asks. scripts/house_closet.py drives it; the design is
docs/superpowers/specs/2026-09-18-house-closet-migration-design.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.wallet import Wallet

from lfg_core import closet_market_store as cms
from lfg_core import config, market_ops


class HouseConfigError(RuntimeError):
    """The house wallet config is missing or unsafe. Nothing was changed."""


def _system_wallets() -> set[str]:
    """Project wallets that must never double as the house: a Closet can't
    belong to the issuer (#383), and the others keep books of their own."""
    return {
        a
        for a in (
            config.SIGNING_ACCOUNT,
            config.SWAP_ISSUER_ADDRESS,
            config.BRIX_ISSUER,
            config.TOKEN_ISSUER_ADDRESS,
            config.BRIX_DISTRIBUTOR_ADDRESS,
            config.BRIX_AMM_ACCOUNT,
        )
        if a
    }


def house_address() -> str:
    """The configured house wallet, validated. Needs no seed."""
    address = config.CLOSET_HOUSE_WALLET
    if not address:
        raise HouseConfigError("CLOSET_HOUSE_WALLET is not set")
    if not is_valid_classic_address(address):
        raise HouseConfigError(f"CLOSET_HOUSE_WALLET is not a classic address: {address}")
    if address in _system_wallets():
        raise HouseConfigError(
            f"CLOSET_HOUSE_WALLET {address} is a system wallet (issuer, app, distributor "
            "or AMM); the house must be a wallet of its own"
        )
    return address


def house_wallet() -> Wallet:
    """The house wallet's signing key. The seed must derive CLOSET_HOUSE_WALLET:
    a regular key would sign for another account, so it is refused."""
    address = house_address()
    if not config.CLOSET_HOUSE_SEED:
        raise HouseConfigError("CLOSET_HOUSE_SEED is not set")
    try:
        wallet = Wallet.from_seed(config.CLOSET_HOUSE_SEED)
    except Exception:
        # Never echo the seed, not even inside the library's own message.
        raise HouseConfigError("CLOSET_HOUSE_SEED is not a valid seed") from None
    if wallet.classic_address != address:
        raise HouseConfigError(
            "CLOSET_HOUSE_SEED signs for a different account than CLOSET_HOUSE_WALLET "
            f"({wallet.classic_address} != {address}); a regular key is not supported"
        )
    return wallet


# Plan actions
LIST = "list"
HOLD = "hold"  # value "None": deposited, never listed (#516 D5)

# Item statuses (the spec's status table)
PLANNED = "planned"
FAILED = "failed"  # failed before the burn; nothing changed; retried
NEEDS_ATTENTION = "needs_attention"  # burned or unknown; never retried
SKIPPED = "skipped"
HELD = "held"
DEPOSITED = "deposited"
LISTED = "listed"


class MigrationRefused(RuntimeError):
    """A precondition failed. Nothing was changed."""


@dataclass(frozen=True)
class PlanItem:
    nft_id: str
    slot: str
    value: str
    offer_index: str
    price_brix: str
    action: str


def build_plan(conn: sqlite3.Connection, app_wallet: str) -> list[PlanItem]:
    """Every live in-app BRIX trait listing whose token the app wallet still
    holds. One item per token: its cheapest listing if it somehow has two."""
    rows = conn.execute(
        "SELECT m.nft_id, m.offer_index, t.slot, t.value, m.amount_brix "
        "FROM market_listings m JOIN trait_tokens t ON t.nft_id = m.nft_id "
        "WHERE m.kind = 'trait' AND m.is_live = 1 AND m.seller = ? AND t.owner = ? "
        "AND (m.destination IS NULL OR m.destination = '') AND m.amount_brix IS NOT NULL",
        (app_wallet, app_wallet),
    ).fetchall()
    best: dict[str, PlanItem] = {}
    for nft_id, offer_index, slot, value, amount_brix in rows:
        price = market_ops.validate_brix_value(str(amount_brix))
        item = PlanItem(nft_id, slot, value, offer_index, price, HOLD if value == "None" else LIST)
        kept = best.get(nft_id)
        if kept is None or Decimal(price) < Decimal(kept.price_brix):
            best[nft_id] = item
    return sorted(best.values(), key=lambda i: (i.slot, i.value, i.nft_id))


def load_state(path: str) -> dict[str, Any]:
    """The plan file, or a fresh one. `started` bounds which house asks a
    re-run may adopt as its own (see list_phase)."""
    try:
        with open(path) as fh:
            return dict(json.load(fh))
    except FileNotFoundError:
        return {"version": 1, "house": None, "started": int(time.time()), "items": {}}


def save_state(path: str, state: dict[str, Any]) -> None:
    """Atomic: a crash mid-write leaves the previous plan file intact."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".house_migration.")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def merge_plan(state: dict[str, Any], items: list[PlanItem], house: str) -> int:
    """Add items not in the plan yet as `planned`; existing entries keep their
    status, so a re-run resumes. Returns how many were added. A plan made for
    another house is refused: its deposits sit in that house's Closet."""
    if state["house"] not in (None, house):
        raise MigrationRefused(
            f"this plan file is for the house {state['house']}, not {house}; "
            "move it aside to start a new plan"
        )
    state["house"] = house
    added = 0
    for item in items:
        if item.nft_id in state["items"]:
            continue
        state["items"][item.nft_id] = {
            "slot": item.slot,
            "value": item.value,
            "offer_index": item.offer_index,
            "price_brix": item.price_brix,
            "action": item.action,
            "status": PLANNED,
            "burn_hash": None,
            "journal_id": None,
            "order_id": None,
            "fill_id": None,
            "error": None,
        }
        added += 1
    return added


def house_busy(conn: sqlite3.Connection, house: str) -> bool:
    """Whether the service may be rewriting the house Closet: a live order of
    the house's, or a fill with the house on either side that hasn't finished."""
    order = conn.execute(
        "SELECT 1 FROM closet_orders WHERE owner = ? "
        "AND state IN ('pending_escrow', 'open', 'matched', 'cancelling') LIMIT 1",
        (house,),
    ).fetchone()
    fill = conn.execute(
        "SELECT 1 FROM closet_fills WHERE (seller = ? OR buyer = ?) "
        "AND state NOT IN ('mirrored', 'refunded', 'failed') LIMIT 1",
        (house, house),
    ).fetchone()
    return order is not None or fill is not None


def crossing_bids(
    conn: sqlite3.Connection, items: list[PlanItem], house: str
) -> list[dict[str, str]]:
    """Open bids that a planned ask would fill the moment it is posted (bid >=
    the key's cheapest planned ask). Each fills at the BID's price: the resting
    order sets it (cms.cross_incoming)."""
    floors: dict[tuple[str, str], Decimal] = {}
    for item in items:
        if item.action == LIST:
            key = (item.slot, item.value)
            price = Decimal(item.price_brix)
            floors[key] = min(price, floors.get(key, price))
    out = []
    for (slot, value), floor in sorted(floors.items()):
        for bid in cms.book_levels(conn, slot, value)["bids"]:
            if bid["owner"] != house and Decimal(bid["price_brix"]) >= floor:
                out.append(
                    {
                        "slot": slot,
                        "value": value,
                        "ask_brix": market_ops.validate_brix_value(str(floor)),
                        "bid_brix": bid["price_brix"],
                        "bidder": bid["owner"],
                    }
                )
    return out


def check_ready(
    conn: sqlite3.Connection,
    house: str,
    state: dict[str, Any],
    *,
    brix_line: dict[str, Any] | None,
    limit: str,
) -> None:
    """Every precondition for --migrate --apply, checked before anything is
    written. Raises MigrationRefused naming the first one that fails."""
    if not config.closet_market_enabled():
        raise MigrationRefused(
            "the Closet market is off (CLOSET_MARKET_ENABLED + CLOSET_MARKET_ENC_KEY)"
        )
    if not cms.closet_active(conn, house):
        raise MigrationRefused(f"{house} has no active Closet; run --apply-setup first")
    if brix_line is None or Decimal(str(brix_line.get("limit", "0"))) < Decimal(limit):
        raise MigrationRefused(
            f"{house} needs a BRIX trust line with a limit of at least {limit}; "
            "run --apply-setup first"
        )
    stuck = [n for n, e in state["items"].items() if e["status"] == NEEDS_ATTENTION]
    if stuck:
        raise MigrationRefused(
            f"{len(stuck)} item(s) need attention ({', '.join(sorted(stuck))}); resolve "
            "them from their Deposit journals before the migration continues"
        )
