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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import NFTokenAcceptOffer, Transaction, TrustSet, TrustSetFlag
from xrpl.wallet import Wallet

from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import config, economy_flow, market_ops, market_store, memos, system_wallets
from lfg_core import economy_store as es


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


def require_durable_exclusion(house: str) -> None:
    """A house wallet goes into service only once it is in the append-only
    durable roster: leaderboards and metrics exclude it by config too, but a
    later rotation would reclassify its archived activity as a user's (#414)."""
    if house not in system_wallets.DURABLE_SYSTEM_ACCOUNTS:
        raise MigrationRefused(
            f"add {house} to lfg_core/system_wallets.py HISTORICAL_HOUSE_WALLETS "
            "(append-only) before it goes into service, so its history stays "
            "excluded from leaderboards and metrics if the house ever changes"
        )


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
    require_durable_exclusion(house)
    stuck = [n for n, e in state["items"].items() if e["status"] == NEEDS_ATTENTION]
    if stuck:
        raise MigrationRefused(
            f"{len(stuck)} item(s) need attention ({', '.join(sorted(stuck))}); resolve "
            "them from their Deposit journals before the migration continues"
        )


_RETRYABLE = (PLANNED, FAILED)
# Deposit journal statuses (economy_flow.run_deposit) a resumed item is judged by.
_JOURNAL_COMPLETE = ("complete", "complete_pending_mirror")
_JOURNAL_UNCHANGED = ("failed_burn",)  # the burn definitively did not happen


@dataclass
class MigrationDeps:
    economy: economy_flow.EconomyDeps
    app_wallet: str
    house: str
    fee_bps: int
    plan_path: str


def _prior_journal(deps: MigrationDeps, entry: dict[str, Any]) -> dict[str, Any] | None:
    """The Deposit journal of this item's previous attempt, if it wrote one."""
    if not entry.get("journal_id"):
        return None
    path = os.path.join(deps.economy.records_dir, f"deposit-{entry['journal_id']}.json")
    try:
        with open(path) as fh:
            return dict(json.load(fh))
    except FileNotFoundError:
        return None


def _deposited(conn: sqlite3.Connection, entry: dict[str, Any]) -> None:
    """Bookkeeping once the unit is in the house Closet. The burn deleted the
    sell offer on-ledger, but the listener does not close listings on a burn;
    without this the row lingers until the nightly market sweep."""
    market_store.close_listing(conn, entry["offer_index"], "cancelled")
    entry.update(status=DEPOSITED if entry["action"] == LIST else HELD, error=None)


async def deposit_phase(state: dict[str, Any], deps: MigrationDeps) -> bool:
    """Burn each planned token into the house Closet. True once nothing is left
    to deposit; False when it stopped on a failure (the item's error says why).

    Each item's Deposit journal id is saved before its burn, so a run that died
    mid-item is judged from that journal on the next run: a completed Deposit is
    recorded, never burned again, and one that may have burned without crediting
    the house is `needs_attention`, never retried."""
    items = state["items"]
    todo = sorted(n for n, e in items.items() if e["status"] in _RETRYABLE)
    if not todo:
        return True
    conn = deps.economy.conn
    if house_busy(conn, deps.house):
        raise MigrationRefused(
            "the house has a live order or an unfinished fill, so the service may be "
            "rewriting its Closet; deposits only run before the house lists anything"
        )
    for nft_id in todo:
        entry = items[nft_id]
        prior = _prior_journal(deps, entry)
        prior_status = prior["status"] if prior else None
        if prior_status in _JOURNAL_COMPLETE:
            _deposited(conn, entry)
            save_state(deps.plan_path, state)
            continue
        info = await deps.economy.trait_info_fn(nft_id)  # type: ignore[misc]
        if info is None:
            entry.update(status=FAILED, error="could not look up the token on-ledger")
            save_state(deps.plan_path, state)
            return False
        held = not info.get("is_burned") and info.get("owner") == deps.app_wallet
        if prior is not None and prior_status not in _JOURNAL_UNCHANGED:
            # A previous attempt got past its checks. Retry only a plain failure
            # that provably burned nothing and left the token where it was.
            retry = (
                prior_status == "failed"
                and held
                and not prior.get("burn_hash")
                and not prior.get("pending_tx_hash")
            )
            if not retry:
                entry.update(
                    status=NEEDS_ATTENTION,
                    error="a previous attempt may have burned this token without crediting "
                    f"the house Closet (Deposit journal {entry['journal_id']}: {prior_status})",
                )
                save_state(deps.plan_path, state)
                return False
        if not held:
            entry.update(
                status=SKIPPED,
                error=f"no longer held by the app wallet (owner {info.get('owner')}, "
                f"burned {bool(info.get('is_burned'))})",
            )
            save_state(deps.plan_path, state)
            continue
        session = economy_flow.DepositSession(
            owner=deps.app_wallet, nft_id=nft_id, credit_to=deps.house
        )
        entry["journal_id"] = session.id
        save_state(deps.plan_path, state)  # before the burn: see the docstring
        await economy_flow.run_deposit(session, deps.economy)
        entry["burn_hash"] = session.burn_hash
        if session.state == economy_flow.DONE:
            _deposited(conn, entry)
            save_state(deps.plan_path, state)
            continue
        burned = bool(session.burn_hash or session.pending_tx_hash)
        entry.update(status=NEEDS_ATTENTION if burned else FAILED, error=session.error)
        save_state(deps.plan_path, state)
        return False
    return True


def _unrecorded_ask(
    conn: sqlite3.Connection, house: str, key: tuple[str, str], state: dict[str, Any]
) -> dict[str, Any] | None:
    """A house ask for this key that no plan entry records, posted since this
    plan started: a crash between create_ask and the plan save. Adopting it keeps
    a re-run from listing the same unit twice."""
    recorded = {e.get("order_id") for e in state["items"].values()}
    rows = conn.execute(
        "SELECT id FROM closet_orders WHERE owner = ? AND side = 'ask' AND slot = ? "
        "AND value = ? AND state != 'cancelled' AND created_ts >= ? "
        "ORDER BY created_ts, id",
        (house, key[0], key[1], state["started"]),
    ).fetchall()
    for (order_id,) in rows:
        if order_id not in recorded:
            return cms.get_order(conn, order_id)
    return None


def list_phase(state: dict[str, Any], deps: MigrationDeps) -> bool:
    """Post an ask for each deposited unit at its old price, crossing any open
    bid at or above it (the service's sweep settles the fill). Runs only once
    every deposit is done; it never touches the Closet token, so it cannot race
    the service. True when every deposited unit is listed."""
    items = state["items"]
    waiting = [n for n, e in items.items() if e["status"] in (*_RETRYABLE, NEEDS_ATTENTION)]
    if waiting:
        raise MigrationRefused(
            f"{len(waiting)} item(s) not deposited yet; listing waits for every deposit"
        )
    conn = deps.economy.conn
    for nft_id in sorted(n for n, e in items.items() if e["status"] == DEPOSITED):
        entry = items[nft_id]
        key = (entry["slot"], entry["value"])
        order = _unrecorded_ask(conn, deps.house, key, state)
        if order is None:
            try:
                order = cms.create_ask(
                    conn,
                    owner=deps.house,
                    slot=key[0],
                    value=key[1],
                    price_brix=entry["price_brix"],
                    platform=None,
                )
            except cms.OrderError as exc:
                entry["error"] = str(exc)
                save_state(deps.plan_path, state)
                return False
        fill = (
            cms.cross_incoming(conn, order["id"], fee_bps=deps.fee_bps)
            if order["state"] == cms.OPEN
            else None
        )
        entry.update(
            status=LISTED, order_id=order["id"], fill_id=fill["id"] if fill else None, error=None
        )
        save_state(deps.plan_path, state)
    return True


@dataclass
class SetupDeps:
    economy: economy_flow.EconomyDeps
    # account_info's account_data, or None when the account doesn't exist
    account_fn: Callable[[str], Awaitable[dict[str, Any] | None]]
    # the account's BRIX trust line (an account_lines entry), or None
    brix_line_fn: Callable[[str], Awaitable[dict[str, Any] | None]]
    # sign with the wallet, submit, wait; returns the engine result
    submit_fn: Callable[[Transaction, Wallet], Awaitable[str]]


def _tag(action: str) -> dict[str, Any]:
    return {
        "source_tag": config.SOURCE_TAG,
        "memos": memos.build_memo_models(memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, action),
    }


async def _no_payload(offer_id: str) -> None:
    """Setup accepts the Closet offer with the house seed: no Xaman payload."""
    return None


async def setup_house(wallet: Wallet, deps: SetupDeps, *, limit: str) -> dict[str, str]:
    """One-time and idempotent: the BRIX trust line (sale proceeds arrive in
    BRIX), then the house Closet, minted if needed and accepted with the house
    seed. Returns what each step did."""
    house = wallet.classic_address
    require_durable_exclusion(house)
    if await deps.account_fn(house) is None:
        raise MigrationRefused(
            f"{house} is not funded on this network; send it the XRP reserve first"
        )
    steps: dict[str, str] = {}
    line = await deps.brix_line_fn(house)
    if line is None or Decimal(str(line.get("limit", "0"))) < Decimal(limit):
        trust = TrustSet(
            account=house,
            flags=TrustSetFlag.TF_SET_NO_RIPPLE,
            limit_amount=IssuedCurrencyAmount(
                currency=config.BRIX_CURRENCY_HEX, issuer=config.BRIX_ISSUER, value=limit
            ),
            **_tag(memos.ACTION_TRUSTSET),
        )
        result = await deps.submit_fn(trust, wallet)
        if result != "tesSUCCESS":
            raise MigrationRefused(f"the BRIX TrustSet failed: {result}")
        steps["trust_line"] = "set"
    else:
        steps["trust_line"] = "present"

    econ, conn = deps.economy, deps.economy.conn
    rec = es.get_closet_record(conn, house)
    if rec is not None and rec[2] != ct.ACTIVE:
        # A previous run's accept may have landed before the listener caught
        # up: confirm on-ledger before paying for a second accept.
        if await ct.confirm_accept(conn, house, owner_fn=econ.closet_owner_fn) == ct.ACTIVE:  # type: ignore[arg-type]
            rec = es.get_closet_record(conn, house)
    if rec is not None and rec[2] == ct.ACTIVE:
        steps["closet"] = ct.ACTIVE
        return steps
    if rec is None or not rec[3]:
        # No Closet yet (mint + offer), or a pending one whose offer id was lost
        # (a fresh offer). With no payload fn, ensure_closet only records the offer.
        await ct.ensure_closet(
            conn,
            house,
            upload_fn=econ.closet_upload_fn,
            mint_fn=econ.closet_mint_fn,
            offer_fn=econ.closet_offer_fn,
            accept_payload_fn=_no_payload,
            exists_fn=econ.closet_exists_fn,
        )
        rec = es.get_closet_record(conn, house)
    if rec is None or not rec[3]:
        raise MigrationRefused("the house Closet was not offered; re-run --apply-setup")
    nft_id, uri_hex, _status, offer_id = rec
    accept = NFTokenAcceptOffer(
        account=house, nftoken_sell_offer=offer_id, **_tag(memos.ACTION_ACCEPT_OFFER)
    )
    result = await deps.submit_fn(accept, wallet)
    status = await ct.confirm_accept(conn, house, owner_fn=econ.closet_owner_fn)  # type: ignore[arg-type]
    if status != ct.ACTIVE and result != "tesSUCCESS":
        # The stored offer is gone or unusable: clear it so the next run makes
        # a fresh one for the same token.
        es.set_closet_token(conn, house, nft_id, uri_hex, status=status, offer_id=None)
        raise MigrationRefused(f"accepting the Closet offer failed: {result}; re-run --apply-setup")
    steps["closet"] = status
    return steps
