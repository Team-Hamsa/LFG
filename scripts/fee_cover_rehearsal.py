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
  .venv/bin/python scripts/fee_cover_rehearsal.py verify --offer-index <index>   (or --bid <session_id>)

Testnet only: every subcommand exits 2 unless XRPL_NETWORK=testnet, and every
subcommand that touches the ledger first asks each configured JSON-RPC endpoint
what it is (server_info network_id 1, plus the ledger-32570 anchor once the
testnet archive has recorded one): any endpoint that isn't testnet exits 2
before a single request is signed. Every transaction carries SourceTag and
provenance memos (campaign `fee-cover-rehearsal`) and is signed once and
submitted through xrpl_ops.rpc_client(), via xrpl_ops._submit_and_confirm, which
also serializes the issuer's sequence with the running staging service. The
wallet seeds this creates are written ONLY to the --state file, owner-only
(0600); a --state path inside this repository is refused (exit 2).

`setup` and `mint-to-seller` record each step in the state file and skip it on
a re-run, so one that failed part-way can simply be run again. `list` and
`broker-accept` act every time they run.

Exit codes: 0 ok · 1 a step failed, a bid was refused, no endpoint could be
verified, or verify ended without a confirmed refund · 2 refused (network or
endpoint not testnet, state path, usage).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import sys
import tempfile
import time
from collections.abc import Coroutine
from typing import Any, Protocol, TypeVar, cast

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from xrpl.asyncio.wallet import generate_faucet_wallet  # noqa: E402
from xrpl.models import TransactionMetadata  # noqa: E402
from xrpl.models.requests import AccountNFTs, Ledger, LedgerEntry, ServerInfo  # noqa: E402
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
    brix_drip,
    brokers,
    config,
    db_path,
    fee_cover_store,
    history_store,
    market_ops,
    memos,
    xrpl_ops,
    xrpl_rpc,
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
# What a testnet server reports as server_info.info.network_id (mainnet omits it).
TESTNET_NETWORK_ID = 1
ENDPOINT_CHECK_TIMEOUT_SECONDS = 30.0
ACCOUNT_NFTS_MAX_PAGES = 25
# fee-cover states that are still moving: anything else ends a verify poll.
_WAITING = frozenset({"quoted", "open", "owed", "submitted"})
_LSF_SELL_NFTOKEN = 0x00000001

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2

T = TypeVar("T")


class RehearsalError(Exception):
    """A step that could not complete: printed, exit 1."""


class WrongNetwork(RehearsalError):
    """A JSON-RPC endpoint the harness could submit through is not testnet:
    refused, exit 2."""


class Chain(Protocol):
    async def ensure_testnet(self) -> None: ...

    async def fund_from_faucet(self, wallet: Wallet) -> None: ...

    async def submit(self, tx: Transaction, wallet: Wallet, label: str) -> dict[str, Any]: ...

    async def validated_ledger_index(self) -> int: ...

    async def ledger_entry(
        self, index: str, ledger_index: int | str = "validated"
    ) -> dict[str, Any] | None: ...

    async def holds_nft(
        self, address: str, nft_id: str, ledger_index: int | str = "validated"
    ) -> bool: ...


class XrplChain:
    """The testnet ledger, over one xrpl_ops.rpc_client(): the failover
    client every backend JSON-RPC call uses, built by ensure_testnet() from
    the endpoints it verified. Nothing reaches the ledger before that."""

    def __init__(self) -> None:
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RehearsalError("the endpoints were not verified as testnet (ensure_testnet)")
        return self._client

    async def ensure_testnet(self) -> None:
        """Refuse unless every JSON-RPC endpoint a submit could reach is testnet.

        XRPL_NETWORK=testnet proves nothing about the endpoint: an explicit
        XRPL_JSON_RPC_URL (or fallback list) wins over the network's defaults,
        and an exported shell variable beats .env, so a mainnet endpoint under
        XRPL_NETWORK=testnet would sign real mainnet transactions with the
        environment's key. Failover can route any request to any configured
        endpoint, so each one is asked on its own:

        - its ledger-32570 anchor, against the testnet archive's recorded
          identity (brix_drip.verify_endpoint_chain; a no-op while the archive
          has recorded none);
        - its server_info network_id, which must be TESTNET_NETWORK_ID.
          Missing or different is WrongNetwork.

        An endpoint that can't answer is excluded for this run
        (xrpl_rpc.restrict_to), and the submit client is built from the
        verified ones only."""
        hconn = history_store.init_history_db(history_store.history_db_path(TESTNET))
        try:
            problem = await brix_drip.verify_endpoint_chain(hconn, TESTNET)
        except Exception as exc:
            raise RehearsalError(f"could not verify any endpoint's chain: {exc}") from exc
        finally:
            hconn.close()
        if problem is not None:
            raise WrongNetwork(problem)
        verified: list[str] = []
        for url in xrpl_rpc.default_urls(config.JSON_RPC_URLS):
            endpoint = xrpl_ops.rpc_client(urls=[url])
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(endpoint.request, ServerInfo()),
                    timeout=ENDPOINT_CHECK_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logging.warning("rehearsal: %s did not answer server_info (excluded): %s", url, exc)
                continue
            if not response.is_successful():
                logging.warning(
                    "rehearsal: %s server_info failed (excluded): %s", url, response.result
                )
                continue
            info = response.result.get("info")
            network_id = info.get("network_id") if isinstance(info, dict) else None
            if network_id != TESTNET_NETWORK_ID:
                raise WrongNetwork(
                    f"endpoint {url} reports network_id {network_id!r}, not testnet "
                    f"({TESTNET_NETWORK_ID}): check XRPL_JSON_RPC_URL / XRPL_JSON_RPC_FALLBACK_URLS"
                )
            verified.append(url)
        if not verified:
            raise RehearsalError("no JSON-RPC endpoint answered server_info: nothing to verify")
        xrpl_rpc.restrict_to(verified)
        self._client = xrpl_ops.rpc_client()

    async def fund_from_faucet(self, wallet: Wallet) -> None:
        await generate_faucet_wallet(self.client, wallet)

    async def submit(self, tx: Transaction, wallet: Wallet, label: str) -> dict[str, Any]:
        """Sign once, submit, confirm (pre-submit simulate and the per-account
        submission lock included). A definitive failure raises RehearsalError;
        an unknown outcome raises xrpl_ops.IndeterminateResultError."""
        client = self.client
        result = await xrpl_ops._submit_and_confirm(tx, wallet, client, label)
        if result is None:
            raise RehearsalError(f"{label}: failed on-ledger (its result is in the warning above)")
        return result

    async def validated_ledger_index(self) -> int:
        """The latest validated ledger, so two reads can be pinned to one view."""
        response = await asyncio.to_thread(self.client.request, Ledger(ledger_index="validated"))
        index = response.result.get("ledger_index") if response.is_successful() else None
        if not isinstance(index, int):
            raise RehearsalError(f"could not read the validated ledger index: {response.result}")
        return index

    async def ledger_entry(
        self, index: str, ledger_index: int | str = "validated"
    ) -> dict[str, Any] | None:
        response = await asyncio.to_thread(
            self.client.request, LedgerEntry(index=index, ledger_index=ledger_index)
        )
        if response.is_successful():
            node = response.result.get("node")
            return node if isinstance(node, dict) else None
        if response.result.get("error") == "entryNotFound":
            return None
        raise RehearsalError(f"ledger_entry {index} failed: {response.result.get('error')}")

    async def holds_nft(
        self, address: str, nft_id: str, ledger_index: int | str = "validated"
    ) -> bool:
        """Whether `address` holds `nft_id` at `ledger_index`.

        `account_nfts`, not `nft_info`: the latter is clio-only, and
        CLIO_WS_URL is a separate endpoint this harness never verified as
        testnet. An account with many tokens pages, so markers are followed."""
        marker: Any = None
        for _ in range(ACCOUNT_NFTS_MAX_PAGES):
            response = await asyncio.to_thread(
                self.client.request,
                AccountNFTs(account=address, ledger_index=ledger_index, limit=400, marker=marker),
            )
            if not response.is_successful():
                if response.result.get("error") == "actNotFound":
                    return False
                raise RehearsalError(
                    f"account_nfts for {address} failed: {response.result.get('error')}"
                )
            for token in response.result.get("account_nfts") or []:
                if isinstance(token, dict) and token.get("NFTokenID") == nft_id:
                    return True
            marker = response.result.get("marker")
            if not marker:
                return False
        raise RehearsalError(f"account_nfts for {address} did not finish paging")


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


def _meta(result: dict[str, Any]) -> dict[str, Any]:
    raw = result.get("meta")
    return raw if isinstance(raw, dict) else {}


def _created_offer_index(meta: dict[str, Any]) -> str | None:
    """The NFTokenOffer this transaction created, read from its metadata."""
    for node in meta.get("AffectedNodes") or []:
        created = node.get("CreatedNode") if isinstance(node, dict) else None
        if isinstance(created, dict) and created.get("LedgerEntryType") == "NFTokenOffer":
            index = created.get("LedgerIndex")
            if isinstance(index, str) and index:
                return index
    return None


def _offer_id(result: dict[str, Any], label: str) -> str:
    """The offer index of a validated NFTokenCreateOffer. `meta.offer_id` is a
    convenience rippled may omit, and by now the offer EXISTS — so it is
    derived from the metadata rather than failing a step that succeeded, which
    a re-run would repeat as a second on-ledger offer."""
    meta = _meta(result)
    offer_id = meta.get("offer_id") or _created_offer_index(meta)
    if not isinstance(offer_id, str) or not offer_id:
        raise RehearsalError(
            f"{label}: validated, but its offer index could not be read (tx "
            f"{result.get('hash')}): take it from that transaction and set it in the state file"
        )
    return offer_id


def _nft_id(result: dict[str, Any]) -> str:
    """The NFTokenID of a validated mint — from `meta.nftoken_id` when rippled
    includes it, else derived from the metadata like xrpl_ops.mint_nft does.
    The character exists either way, and a step that failed here would be
    re-run into a second mint."""
    meta = _meta(result)
    nft_id = meta.get("nftoken_id")
    if not nft_id:
        with contextlib.suppress(Exception):
            nft_id = get_nftoken_id(cast(TransactionMetadata, meta))
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
    if config.NFT_TRANSFER_FEE <= 0:
        # No royalty reaches the issuer, and the refund is bounded by the
        # royalty observed in the accept: settlement would decline it
        # `royalty_unobserved`. Refuse before minting anything.
        raise RehearsalError(
            "NFT_TRANSFER_FEE is 0: the sale would pay no royalty, so the refund would be "
            "declined royalty_unobserved. Set a non-zero TransferFee before rehearsing."
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
    if state.get("transfer_offer") and not state.get("seller_holds_nft"):
        await _resume_delivery(chain, path, state, seller)
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


async def _resume_delivery(chain: Chain, path: str, state: dict[str, Any], seller: Wallet) -> None:
    """Re-running after a crash in delivery: ask the ledger what became of the
    recorded transfer offer, because a crash between a validated accept and
    the state write used to leave the re-run submitting a consumed offer,
    which can only fail.

    Still there → nothing to settle, the accept below runs. Gone AND the
    seller holds the character → the accept landed. Gone and it does not →
    the offer was cancelled (the issuer can cancel its own), so forget it and
    make a new one rather than record a delivery that never happened.

    Both reads are pinned to ONE validated ledger. They are separate requests
    over a failover client, so unpinned they could describe different
    instants: an endpoint lagging the accept would report the seller as not
    holding a character it does have, and this would clear the offer and
    leave the issuer re-offering a token it no longer owns. Pinned, such an
    endpoint answers lgrNotFound instead, which raises and leaves the state
    untouched for a later re-run."""
    ledger = await chain.validated_ledger_index()
    offer = str(state["transfer_offer"])
    if await chain.ledger_entry(offer, ledger_index=ledger) is not None:
        return
    nft_id = str(state["nft_id"])
    if await chain.holds_nft(seller.address, nft_id, ledger_index=ledger):
        print(f"transfer offer {offer} was accepted before it could be recorded")
        state["seller_holds_nft"] = True
    else:
        print(f"transfer offer {offer} is gone and the seller holds nothing: offering again")
        state["transfer_offer"] = None
    save_state(path, state)


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


def offer_fee_cover(app_db: str, offer_index: str) -> dict[str, Any]:
    """The fee-cover state of the bid with ledger index `offer_index`: its
    promise/refund view. Unlike a session's quote, this exists for every bid
    whose promise was written — including one declined when it started
    (system_wallet, below_clearing, below_min_bid), which gets no quote."""
    conn = fee_cover_store.connect(app_db)
    try:
        view = fee_cover_store.view_for_offer(conn, offer_index)
    finally:
        conn.close()
    return {"state": "no_promise"} if view is None else {**view, "offer_index": offer_index}


def verify(timeout_seconds: float, *, session_id: str | None, offer_index: str | None) -> int:
    if not math.isfinite(timeout_seconds):
        # nan never passes its deadline and inf never arrives: either would
        # poll a still-moving fee cover for ever.
        raise RehearsalError("--timeout must be a finite number of seconds")
    app_db = db_path.app_db_path(TESTNET)
    deadline = time.monotonic() + timeout_seconds
    last: str | None = None
    while True:
        if offer_index is not None:
            cover = offer_fee_cover(app_db, offer_index)
        else:
            cover = bid_fee_cover(app_db, str(session_id))
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
    elif state == "no_quote":
        print(
            "that session has no covered quote: a bid declined when it started (system_wallet, "
            "below_clearing, below_min_bid) gets none. Pass --offer-index <index> from "
            "fee_cover_promises instead."
        )
    elif state == "no_promise":
        print("no fee-cover promise for that offer index (yet)")
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


async def _on_testnet(ledger: Chain, args: argparse.Namespace, path: str) -> None:
    """Run a ledger subcommand once the endpoints have proved they're testnet:
    no funding, lookup or transaction happens before that."""
    await ledger.ensure_testnet()
    if args.command == "setup":
        await setup(ledger, path)
    elif args.command == "mint-to-seller":
        await mint_to_seller(ledger, path)
    elif args.command == "list":
        await list_character(ledger, path, args.price_xrp)
    else:
        await broker_accept(ledger, path, args.buy_offer)


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
    target = check.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--offer-index",
        metavar="INDEX",
        help="the bid's offer index (fee_cover_promises): finds every bid, "
        "including one declined when it started",
    )
    target.add_argument("--bid", metavar="SESSION_ID", help="a bid session id with a covered quote")
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
            return verify(args.timeout, session_id=args.bid, offer_index=args.offer_index)
        path = str(state_path)  # every other subcommand requires --state
        if args.command == "print-allowlist":
            print_allowlist(path)
            return EXIT_OK
        _run(_on_testnet(chain if chain is not None else XrplChain(), args, path))
        return EXIT_OK
    except WrongNetwork as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return EXIT_REFUSED
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
