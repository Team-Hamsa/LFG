#!/usr/bin/env python3
"""Testnet rehearsal of the marketplace fee cover, end to end (#515).

A real brokered fill of a bid placed THROUGH LFG. Testnet has no xrp.cafe, so
script-held wallets stand in for the missing parties: a broker, a seller whose
character is listed with that broker, and an intermediate that funds the
seller. The bidder is a real testnet wallet signed in to staging.
Runbook, including the owner steps between these commands:
docs/ops/fee-cover-rehearsal.md.

  .venv/bin/python scripts/fee_cover_rehearsal.py setup --state ~/fee-cover-rehearsal/state.json
  .venv/bin/python scripts/fee_cover_rehearsal.py mint-to-seller --state <state>
  .venv/bin/python scripts/fee_cover_rehearsal.py list --state <state> --price-xrp 5
  .venv/bin/python scripts/fee_cover_rehearsal.py print-allowlist --state <state>
  .venv/bin/python scripts/fee_cover_rehearsal.py broker-accept --state <state> --buy-offer <index>
  .venv/bin/python scripts/fee_cover_rehearsal.py verify --bid <session_id>

Testnet only: every subcommand exits 2 unless XRPL_NETWORK=testnet. Every
transaction carries SourceTag and provenance memos (campaign
`fee-cover-rehearsal`) and is signed once and submitted through
xrpl_ops.rpc_client(), via xrpl_ops._submit_and_confirm, which also serializes
the issuer's sequence with the running staging service. The wallet seeds this
creates are written ONLY to the --state file, owner-only (0600); a --state path
inside this repository is refused (exit 2).

`setup` and `mint-to-seller` record each step in the state file and skip it on
a re-run, so one that failed part-way can simply be run again. `list` and
`broker-accept` act every time they run.

Exit codes: 0 ok · 1 a step failed, a bid was refused, or verify ended without
a confirmed refund · 2 refused (network, state path, usage).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Coroutine
from typing import Any, Protocol, TypeVar

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from xrpl.asyncio.wallet import generate_faucet_wallet  # noqa: E402
from xrpl.models.requests import LedgerEntry  # noqa: E402
from xrpl.models.transactions import (  # noqa: E402
    Memo,
    NFTokenAcceptOffer,
    NFTokenCreateOffer,
    NFTokenMint,
    Payment,
)
from xrpl.models.transactions.nftoken_create_offer import NFTokenCreateOfferFlag  # noqa: E402
from xrpl.models.transactions.transaction import Transaction  # noqa: E402
from xrpl.utils import get_nftoken_id  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

from lfg_core import (  # noqa: E402
    brokers,
    config,
    db_path,
    fee_cover_store,
    market_ops,
    memos,
    xrpl_ops,
)
from scripts import fee_cover_report  # noqa: E402

TESTNET = "testnet"
ROLES = ("intermediate", "broker", "seller")
BROKER_NAME = "testbroker"
# xrp.cafe's measured rate (lfg_core/brokers.py), so the fill matches mainnet's.
BROKER_RATE = 0.01589
# The memo campaign on every rehearsal transaction: findable on-ledger.
CAMPAIGN = "fee-cover-rehearsal"
# What the intermediate sends the seller: the account, NFToken page and sell
# offer reserves plus fees, with room to spare, and well under a faucet grant.
SELLER_FUNDING_DROPS = 25_000_000
# Names the rehearsal and resolves to nothing: the listener indexes the
# character without metadata, which is all a bid needs.
REHEARSAL_URI = "lfg:fee-cover-rehearsal"
POLL_SECONDS = 15
DEFAULT_VERIFY_TIMEOUT_SECONDS = 1800
# fee-cover states that are still moving: anything else ends a verify poll.
_WAITING = frozenset({"quoted", "open", "owed", "submitted"})
_LSF_SELL_NFTOKEN = 0x00000001

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

T = TypeVar("T")


class RehearsalError(Exception):
    """A step that could not complete: printed, exit 1."""


class Chain(Protocol):
    async def fund_from_faucet(self, wallet: Wallet) -> None: ...

    async def submit(self, tx: Transaction, wallet: Wallet, label: str) -> dict[str, Any]: ...

    async def ledger_entry(self, index: str) -> dict[str, Any] | None: ...


class XrplChain:
    """The testnet ledger, over one xrpl_ops.rpc_client(): the failover
    client every backend JSON-RPC call uses."""

    def __init__(self) -> None:
        self.client = xrpl_ops.rpc_client()

    async def fund_from_faucet(self, wallet: Wallet) -> None:
        await generate_faucet_wallet(self.client, wallet)

    async def submit(self, tx: Transaction, wallet: Wallet, label: str) -> dict[str, Any]:
        """Sign once, submit, confirm (pre-submit simulate and the per-account
        submission lock included). A definitive failure raises RehearsalError;
        an unknown outcome raises xrpl_ops.IndeterminateResultError."""
        result = await xrpl_ops._submit_and_confirm(tx, wallet, self.client, label)
        if result is None:
            raise RehearsalError(f"{label}: failed on-ledger (its result is in the warning above)")
        return result

    async def ledger_entry(self, index: str) -> dict[str, Any] | None:
        response = await asyncio.to_thread(
            self.client.request, LedgerEntry(index=index, ledger_index="validated")
        )
        if response.is_successful():
            node = response.result.get("node")
            return node if isinstance(node, dict) else None
        if response.result.get("error") == "entryNotFound":
            return None
        raise RehearsalError(f"ledger_entry {index} failed: {response.result.get('error')}")


def broker_fee_drops(bid_drops: int, rate: float = BROKER_RATE) -> int:
    """The broker's cut of a bid, ceil(bid x rate): the same expression
    lfg_core.brokers.clearing_drops clears against, so a bid the app places at
    the clearing price always clears here."""
    return math.ceil(bid_drops * rate)


# --- state file -------------------------------------------------------------------


def _inside_repo(path: str) -> bool:
    real = os.path.realpath(path)
    root = os.path.realpath(REPO_ROOT)
    return real == root or real.startswith(root + os.sep)


def load_state(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"network": TESTNET, "wallets": {}}
    if (
        not isinstance(state, dict)
        or state.get("network") != TESTNET
        or not isinstance(state.get("wallets"), dict)
    ):
        raise RehearsalError(f"{path} is not a testnet rehearsal state file")
    return state


def save_state(path: str, state: dict[str, Any]) -> None:
    """Atomically replace the state file. It holds wallet seeds, so it is
    written owner-only (0600) and never left half-written."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fee-cover-rehearsal-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _ensure_wallet(path: str, state: dict[str, Any], role: str) -> dict[str, Any]:
    """The state's entry for `role`, creating the wallet if there is none."""
    entry = state["wallets"].get(role)
    if entry is None:
        wallet = Wallet.create()
        entry = {"address": wallet.address, "seed": wallet.seed, "funded": False}
        state["wallets"][role] = entry
        save_state(path, state)  # the seed is on disk before any XRP reaches it
    if not isinstance(entry, dict) or not entry.get("seed"):
        raise RehearsalError(f"the {role} wallet in the state file is malformed")
    return entry


def _wallet(state: dict[str, Any], role: str) -> Wallet:
    entry = state["wallets"].get(role)
    if not isinstance(entry, dict) or not entry.get("funded"):
        raise RehearsalError(f"no funded {role} wallet in the state file: run setup first")
    return Wallet.from_seed(str(entry["seed"]))


def _need(state: dict[str, Any], key: str, step: str) -> Any:
    value = state.get(key)
    if not value:
        raise RehearsalError(f"no {key} in the state file: run {step} first")
    return value


def _memos(initiator: str, action: str) -> list[Memo]:
    return memos.build_memo_models(initiator, memos.PLATFORM_BACKEND, action, campaign=CAMPAIGN)


def _offer_id(result: dict[str, Any], label: str) -> str:
    meta = result.get("meta")
    offer_id = meta.get("offer_id") if isinstance(meta, dict) else None
    if not isinstance(offer_id, str) or not offer_id:
        raise RehearsalError(
            f"{label}: validated, but its offer index was absent (tx {result.get('hash')})"
        )
    return offer_id


def _nft_id(result: dict[str, Any]) -> str:
    meta = result.get("meta")
    nft_id = meta.get("nftoken_id") if isinstance(meta, dict) else None
    if not nft_id and isinstance(meta, dict):
        with contextlib.suppress(Exception):
            nft_id = get_nftoken_id(meta)
    if not isinstance(nft_id, str) or not nft_id:
        raise RehearsalError(
            f"the mint validated but its NFTokenID could not be read (tx {result.get('hash')}): "
            "set nft_id in the state file from that tx before re-running"
        )
    return nft_id


# --- subcommands ------------------------------------------------------------------


async def setup(chain: Chain, path: str) -> None:
    state = load_state(path)
    for role in ("intermediate", "broker"):
        entry = _ensure_wallet(path, state, role)
        if not entry["funded"]:
            await chain.fund_from_faucet(Wallet.from_seed(entry["seed"]))
            entry["funded"] = True
            save_state(path, state)
    intermediate = _wallet(state, "intermediate")
    seller = _ensure_wallet(path, state, "seller")
    if not seller["funded"]:
        # Never from the faucet: a faucet-funded seller shares its activation
        # funder with a faucet-funded bidder, which the fee cover's linkage
        # rule reads as one person (`linked_counterparty`).
        await chain.submit(
            Payment(
                account=intermediate.address,
                destination=seller["address"],
                amount=str(SELLER_FUNDING_DROPS),
                source_tag=config.SOURCE_TAG,
                memos=_memos(memos.INITIATOR_BACKEND, memos.ACTION_PAYMENT),
            ),
            intermediate,
            "rehearsal: the intermediate funds the seller",
        )
        seller["funded"] = True
        save_state(path, state)
    for role in ROLES:
        print(f"{role}: {state['wallets'][role]['address']}")


async def mint_to_seller(chain: Chain, path: str) -> None:
    state = load_state(path)
    seller = _wallet(state, "seller")
    if not config.NFT_FLAGS & xrpl_ops.TF_TRANSFERABLE:
        raise RehearsalError(
            f"NFT_FLAGS={config.NFT_FLAGS} lacks tfTransferable: no TransferFee, and the seller "
            "could never sell the character"
        )
    issuer = Wallet.from_seed(config.SEED)
    if not state.get("nft_id"):
        result = await chain.submit(
            NFTokenMint(
                account=config.SIGNING_ACCOUNT,
                uri=xrpl_ops.convert_str_to_hex(REHEARSAL_URI),
                nftoken_taxon=config.NFT_TAXON,
                flags=config.NFT_FLAGS,
                transfer_fee=config.NFT_TRANSFER_FEE,
                source_tag=config.SOURCE_TAG,
                memos=_memos(memos.INITIATOR_BACKEND, memos.ACTION_MINT),
            ),
            issuer,
            "rehearsal: mint a character",
        )
        state["nft_id"] = _nft_id(result)
        save_state(path, state)
    if not state.get("transfer_offer"):
        result = await chain.submit(
            NFTokenCreateOffer(
                account=config.SIGNING_ACCOUNT,
                nftoken_id=state["nft_id"],
                amount="0",
                flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
                destination=seller.address,
                source_tag=config.SOURCE_TAG,
                memos=_memos(memos.INITIATOR_BACKEND, memos.ACTION_CREATE_OFFER),
            ),
            issuer,
            "rehearsal: offer the character to the seller",
        )
        state["transfer_offer"] = _offer_id(result, "rehearsal: offer to the seller")
        save_state(path, state)
    if not state.get("seller_holds_nft"):
        await chain.submit(
            NFTokenAcceptOffer(
                account=seller.address,
                nftoken_sell_offer=state["transfer_offer"],
                source_tag=config.SOURCE_TAG,
                memos=_memos(memos.INITIATOR_USER, memos.ACTION_ACCEPT_OFFER),
            ),
            seller,
            "rehearsal: the seller accepts the character",
        )
        state["seller_holds_nft"] = True
        save_state(path, state)
    print(f"nft_id: {state['nft_id']}")
    print(
        f"held by the seller {seller.address} (TransferFee {config.NFT_TRANSFER_FEE}, "
        f"taxon {config.NFT_TAXON})"
    )


async def list_character(chain: Chain, path: str, price_xrp: str) -> None:
    state = load_state(path)
    seller = _wallet(state, "seller")
    broker = _wallet(state, "broker")
    nft_id = _need(state, "nft_id", "mint-to-seller")
    _need(state, "seller_holds_nft", "mint-to-seller")
    try:
        ask = int(market_ops.xrp_to_drops_str(price_xrp))
    except (TypeError, ValueError) as exc:
        raise RehearsalError(f"bad --price-xrp {price_xrp!r}: {exc}") from exc
    result = await chain.submit(
        NFTokenCreateOffer(
            account=seller.address,
            nftoken_id=nft_id,
            amount=str(ask),
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
            destination=broker.address,
            source_tag=config.SOURCE_TAG,
            memos=_memos(memos.INITIATOR_USER, memos.ACTION_LIST),
        ),
        seller,
        "rehearsal: the seller lists with the broker",
    )
    state["sell_offer"] = _offer_id(result, "rehearsal: listing")
    state["ask_drops"] = ask
    save_state(path, state)
    clearing = brokers.clearing_drops(ask, BROKER_RATE)
    print(f"sell offer: {state['sell_offer']}")
    print(
        f"ask {market_ops.drops_to_xrp_str(str(ask))} XRP, Destination {broker.address}; "
        f"a Buy-now bid clears at {market_ops.drops_to_xrp_str(str(clearing))} XRP"
    )


def print_allowlist(path: str) -> None:
    broker = _wallet(load_state(path), "broker")
    entry = {"name": BROKER_NAME, "url_template": None, "broker_rate": BROKER_RATE}
    print(json.dumps({broker.address: entry}))


async def broker_accept(chain: Chain, path: str, buy_offer: str) -> None:
    state = load_state(path)
    broker = _wallet(state, "broker")
    nft_id = _need(state, "nft_id", "mint-to-seller")
    sell_offer = _need(state, "sell_offer", "list")
    ask = int(_need(state, "ask_drops", "list"))
    bid = await chain.ledger_entry(buy_offer)
    if bid is None or bid.get("LedgerEntryType") != "NFTokenOffer":
        raise RehearsalError(f"buy offer {buy_offer} is not on the validated ledger")
    if int(bid.get("Flags") or 0) & _LSF_SELL_NFTOKEN:
        raise RehearsalError(f"{buy_offer} is a sell offer, not a bid")
    if bid.get("NFTokenID") != nft_id:
        raise RehearsalError(
            f"{buy_offer} bids on {bid.get('NFTokenID')}, not the rehearsal character {nft_id}"
        )
    amount = bid.get("Amount")
    if not isinstance(amount, str) or not amount.isdigit():
        raise RehearsalError(f"{buy_offer} is not an XRP bid: {amount!r}")
    bid_drops = int(amount)
    fee = broker_fee_drops(bid_drops)
    if bid_drops - fee < ask:
        raise RehearsalError(
            f"bid {bid_drops} drops less the broker's {fee} is under the ask of {ask}: a real "
            f"broker never fills it (Buy-now clears at {brokers.clearing_drops(ask, BROKER_RATE)})"
        )
    result = await chain.submit(
        NFTokenAcceptOffer(
            account=broker.address,
            nftoken_sell_offer=sell_offer,
            nftoken_buy_offer=buy_offer,
            nftoken_broker_fee=str(fee),
            source_tag=config.SOURCE_TAG,
            memos=_memos(memos.INITIATOR_USER, memos.ACTION_ACCEPT_OFFER),
        ),
        broker,
        "rehearsal: the broker settles the bid",
    )
    print(f"accept tx: {result.get('hash')} (bid {bid_drops} drops, broker fee {fee} drops)")


def bid_fee_cover(app_db: str, session_id: str) -> dict[str, Any]:
    """The fee-cover state of bid session `session_id`, read as the service
    keeps it: the session's quote, then the promise/refund view of the offer
    index that quote closed with."""
    conn = fee_cover_store.connect(app_db)
    try:
        quote = fee_cover_store.get_quote(conn, session_id)
        if quote is None:
            return {"state": "no_quote"}
        offer_index = quote["offer_index"]
        view = fee_cover_store.view_for_offer(conn, str(offer_index)) if offer_index else None
    finally:
        conn.close()
    if view is not None:
        return {**view, "offer_index": offer_index}
    if quote["state"] == "pending":
        return {"state": "quoted"}
    return {"state": f"quote_{quote['outcome']}"}


def verify(session_id: str, timeout_seconds: float) -> int:
    app_db = db_path.app_db_path(TESTNET)
    deadline = time.monotonic() + timeout_seconds
    last: str | None = None
    while True:
        cover = bid_fee_cover(app_db, session_id)
        state = str(cover["state"])
        if state != last:
            print(f"fee cover: {state}")
            last = state
        if state not in _WAITING:
            break
        if time.monotonic() >= deadline:
            print(f"timed out after {timeout_seconds:g}s; last state: {state}")
            break
        time.sleep(POLL_SECONDS)
    if state == "confirmed":
        print(f"refunded {cover['xrp']} XRP, refund tx {cover['payout_tx_hash']}")
    elif cover.get("reason"):
        print(f"{state}: {cover['reason']}")
    print("audit:")
    audit_code = fee_cover_report.main(["--network", TESTNET, "--audit"])
    return EXIT_OK if state == "confirmed" and audit_code == 0 else EXIT_FAILED


# --- entry point ------------------------------------------------------------------


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """A private event loop: asyncio.run() leaves the process with no current
    loop on Python 3.10, which breaks anything that imports and calls main()."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Testnet rehearsal of the marketplace fee cover "
        "(runbook: docs/ops/fee-cover-rehearsal.md)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    state_help = "state file holding the rehearsal wallets' seeds (keep it outside any checkout)"
    for name, help_text in (
        ("setup", "create and fund the broker, intermediate and seller wallets"),
        ("mint-to-seller", "mint a character and deliver it to the seller"),
        ("print-allowlist", "print the BROKER_ALLOWLIST_PATH overlay for the rehearsal broker"),
    ):
        sub.add_parser(name, help=help_text).add_argument("--state", required=True, help=state_help)
    listing = sub.add_parser("list", help="the seller lists the character with the broker")
    listing.add_argument("--state", required=True, help=state_help)
    listing.add_argument("--price-xrp", required=True)
    accept = sub.add_parser("broker-accept", help="the broker settles a bid against the listing")
    accept.add_argument("--state", required=True, help=state_help)
    accept.add_argument("--buy-offer", required=True, metavar="INDEX")
    check = sub.add_parser("verify", help="wait for a bid's fee cover to settle, then audit")
    check.add_argument("--bid", required=True, metavar="SESSION_ID")
    check.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_VERIFY_TIMEOUT_SECONDS,
        help="seconds to wait (default %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None, *, chain: Chain | None = None) -> int:
    args = _parser().parse_args(argv)
    if config.XRPL_NETWORK != TESTNET:
        print(
            f"refusing: the fee-cover rehearsal is testnet only (XRPL_NETWORK={config.XRPL_NETWORK})",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    state_path: str | None = getattr(args, "state", None)
    if state_path is not None and _inside_repo(state_path):
        print(
            f"refusing: --state {state_path} is inside the repository ({REPO_ROOT}). It holds "
            "wallet seeds: keep it outside any checkout.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        if args.command == "verify":
            return verify(args.bid, args.timeout)
        path = str(state_path)  # every other subcommand requires --state
        if args.command == "print-allowlist":
            print_allowlist(path)
            return EXIT_OK
        ledger = chain if chain is not None else XrplChain()
        if args.command == "setup":
            _run(setup(ledger, path))
        elif args.command == "mint-to-seller":
            _run(mint_to_seller(ledger, path))
        elif args.command == "list":
            _run(list_character(ledger, path, args.price_xrp))
        else:
            _run(broker_accept(ledger, path, args.buy_offer))
        return EXIT_OK
    except RehearsalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except xrpl_ops.IndeterminateResultError as exc:
        print(
            f"error: {exc}. The outcome is unknown: look up tx {exc.tx_hash} on-ledger before "
            "running this step again.",
            file=sys.stderr,
        )
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
