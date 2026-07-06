# lfg_core/xrpl_ops.py
# XRPL operations: mint, offer creation, payment watching (extracted from main.py).

import asyncio
import logging
import time
import traceback
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.clients import JsonRpcClient
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.currencies import XRP, IssuedCurrency
from xrpl.models.requests import (
    AccountLines,
    AccountNFTs,
    AccountTx,
    AMMInfo,
    NFTSellOffers,
    Subscribe,
    Tx,
)
from xrpl.models.transactions import (
    NFTokenBurn,
    NFTokenCreateOffer,
    NFTokenMint,
    NFTokenModify,
    Payment,
)
from xrpl.models.transactions.nftoken_create_offer import NFTokenCreateOfferFlag
from xrpl.transaction import submit_and_wait
from xrpl.utils import xrp_to_drops
from xrpl.wallet import Wallet

from lfg_core import config

# On-ledger NFToken flag bits (mirror the tf* mint flags)
NFT_FLAG_BURNABLE = 0x0001
TF_TRANSFERABLE = 0x0008
NFT_FLAG_MUTABLE = 0x0010


def convert_str_to_hex(string: str) -> str:
    """Convert string to hex for XRPL URI"""
    return string.encode("utf-8").hex().upper()


async def mint_nft(
    metadata_cdn_url: str, taxon: int, issuer: str, flags: int | None = None
) -> str | None:
    """Mint an NFT on XRPL; returns the NFToken ID or None. `flags` overrides
    config.NFT_FLAGS (e.g. burnable economy characters / soulbound buckets)."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)

        eff_flags = config.NFT_FLAGS if flags is None else flags
        kwargs: dict[str, Any] = {
            "account": config.SIGNING_ACCOUNT,
            "uri": convert_str_to_hex(metadata_cdn_url),
            "nftoken_taxon": taxon,
            "flags": eff_flags,
            "source_tag": config.SOURCE_TAG,
        }
        # TransferFee is only valid on transferable tokens; XRPL rejects it as
        # temMALFORMED otherwise (e.g. the soulbound Bucket, flags=16).
        if eff_flags & TF_TRANSFERABLE:
            kwargs["transfer_fee"] = config.NFT_TRANSFER_FEE
        if issuer != config.SIGNING_ACCOUNT:
            kwargs["issuer"] = issuer
        payment = NFTokenMint(**kwargs)

        retries = 5
        hash_txn = None
        for attempt in range(1, retries + 1):
            try:
                logging.info(f"Submitting NFTokenMint (attempt {attempt}/{retries})")
                response = await asyncio.to_thread(submit_and_wait, payment, client, wallet)
                hash_txn = response.result["hash"]
                break
            except Exception as e:
                logging.error(f"Mint attempt {attempt} failed: {e}")
                if attempt == retries:
                    return None
                await asyncio.sleep(5)

        for check_attempt in range(1, retries + 1):
            try:
                txn = await asyncio.to_thread(client.request, Tx(transaction=hash_txn))
                res = txn.result
                if res["meta"]["TransactionResult"] == "tesSUCCESS":
                    nft_id = res["meta"].get("nftoken_id")
                    if nft_id:
                        logging.info(f"NFT minted: {nft_id}")
                        return nft_id  # type: ignore[no-any-return]
                    logging.warning("Mint succeeded but no NFT ID in meta")
                else:
                    logging.warning(f"Mint result: {res['meta']['TransactionResult']}")
                break
            except Exception as e:
                logging.error(f"Status check attempt {check_attempt} failed: {e}")
                if check_attempt == retries:
                    return None
                await asyncio.sleep(5)
        return None

    except Exception:
        logging.error(f"mint_nft error: {traceback.format_exc()}")
        return None


async def create_nft_offer(nft_id: str, destination: str, amount: Any = "0") -> str | None:
    """Create a sell offer transferring the NFT to destination; returns offer ID
    or None. amount may be an XRP-drops string or an IssuedCurrencyAmount."""
    try:
        client = JsonRpcClient(config.JSON_RPC_URL)
        wallet = Wallet.from_seed(config.SEED)

        offer = NFTokenCreateOffer(
            account=config.SIGNING_ACCOUNT,
            destination=destination,
            amount=amount,
            nftoken_id=nft_id,
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
            source_tag=config.SOURCE_TAG,
        )

        response = await asyncio.to_thread(submit_and_wait, offer, client, wallet)
        hash_txn = response.result["hash"]

        for _ in range(3):
            try:
                txn = await asyncio.to_thread(client.request, Tx(transaction=hash_txn))
                res = txn.result
                if res["meta"]["TransactionResult"] == "tesSUCCESS":
                    offer_id = res["meta"]["offer_id"]
                    logging.info(f"Offer created: {offer_id}")
                    return offer_id  # type: ignore[no-any-return]
                await asyncio.sleep(5)
            except Exception as e:
                logging.error(f"Error checking offer status: {e}")
                await asyncio.sleep(5)
        return None

    except Exception as e:
        logging.error(f"create_nft_offer error: {e}")
        return None


def swap_offer_amount() -> IssuedCurrencyAmount:
    """The token amount (e.g. 10 BRIX) charged for re-crafted swap NFTs."""
    return IssuedCurrencyAmount(
        currency=config.SWAP_OFFER_CURRENCY_HEX,
        issuer=config.SWAP_OFFER_ISSUER,
        value=config.SWAP_OFFER_AMOUNT,
    )


async def get_account_nfts(address: str, issuer: str) -> list[dict[str, Any]]:
    """List NFTs held by `address` that were issued by `issuer`.
    Returns a list of {"nft_id", "uri_hex", "flags"} dicts."""
    nfts = []
    marker = None
    async with AsyncWebsocketClient(config.WS_URL) as websocket:
        while True:
            response = await websocket.request(
                AccountNFTs(account=address, marker=marker, limit=400)
            )
            result = response.result
            for nft in result.get("account_nfts", []):
                if nft.get("Issuer") != issuer:
                    continue
                nfts.append(
                    {
                        "nft_id": nft["NFTokenID"],
                        "uri_hex": nft.get("URI", ""),
                        "flags": nft.get("Flags", 0),
                    }
                )
            marker = result.get("marker")
            if not marker:
                break
    return nfts


def _parse_nft_info(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize a clio `nft_info` result into the token shape the index uses."""
    return {
        "nft_id": result.get("nft_id"),
        "owner": result.get("owner"),
        "flags": int(result.get("flags") or 0),
        "uri_hex": result.get("uri", "") or "",
        "is_burned": bool(result.get("is_burned")),
        "issuer": result.get("issuer"),
        "taxon": result.get("nft_taxon"),
    }


def _clio_endpoint(clio: str | None) -> str:
    """Resolve the clio endpoint for clio-only methods (nft_info / nft_exists):
    the explicit arg when given, else config.CLIO_WS_URL. Never WS_URL — the
    plain rippled WS cannot answer nft_info (returns `unknownCmd`)."""
    return clio or config.CLIO_WS_URL


async def nft_info(nft_id: str, clio: str | None = None) -> dict[str, Any] | None:
    """Current owner/flags/uri/burn state for a single NFToken via clio's
    `nft_info` (needed to resolve the owner after a transfer — the XLS-46 path).
    Returns None on error."""
    from xrpl.models.requests import Request

    endpoint = _clio_endpoint(clio)
    try:
        async with AsyncWebsocketClient(endpoint) as websocket:
            response = await websocket.request(
                Request.from_dict({"method": "nft_info", "nft_id": nft_id})
            )
        result = response.result
        if not isinstance(result, dict) or result.get("error"):
            return None
        return _parse_nft_info(result)
    except Exception as e:
        logging.warning(f"nft_info failed for {nft_id}: {e}")
        return None


async def nft_exists(nft_id: str, clio: str | None = None, attempts: int = 3) -> bool | None:
    """On-ledger existence of an NFToken, distinguishing a DEFINITIVE absence
    from a transient lookup failure — unlike `nft_info`, which returns None for
    both. Returns True (present), False (clio definitively reports it absent), or
    None (could not determine after retries — network/ws error).

    Callers that re-mint on absence MUST treat None as "assume present", so a
    transient blip never re-mints and orphans a live token."""
    from xrpl.models.requests import Request

    endpoint = _clio_endpoint(clio)
    for attempt in range(attempts):
        try:
            async with AsyncWebsocketClient(endpoint) as websocket:
                response = await websocket.request(
                    Request.from_dict({"method": "nft_info", "nft_id": nft_id})
                )
            result = response.result
            if isinstance(result, dict) and not result.get("error"):
                return True
            err = str(result.get("error", "")).lower() if isinstance(result, dict) else ""
            # clio reports a missing/never-minted token as objectNotFound; that is
            # the only result we treat as a definitive absence. Any other error
            # code is indeterminate — retry, then fall through to None.
            if "notfound" in err:
                return False
        except Exception as e:
            logging.warning(f"nft_exists failed for {nft_id} (attempt {attempt + 1}): {e}")
        if attempt + 1 < attempts:
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def get_nft_sell_offers(nft_id: str, raise_on_error: bool = False) -> list[dict[str, Any]]:
    """List sell offers for `nft_id` via the standard (non-clio) rippled
    `nft_sell_offers` method. Unlike nft_info/nft_exists this is a plain
    method, so it goes through JSON_RPC_URL like mint/burn/offer, not
    CLIO_WS_URL.

    Each returned dict is normalized to
    `{offer_index, amount, destination, flags, owner}`. `offer_index` accepts
    either the `nft_offer_index` or `index` field — different server versions
    key the offer's ledger index differently (drift guard, mirrors Baysed
    market.py:386-390).

    Returns an empty list when there are no offers or the NFT is unknown to
    the server. By default an RPC/network failure ALSO returns [] — callers
    doing fail-closed verification (`market_ops.verify_sell_offer`) treat an
    empty/non-matching list as "no valid offer", never a false positive.
    Callers that must distinguish "genuinely no offers" from "lookup failed"
    (e.g. scripts/backfill_market.py's stale-close pass, where conflating the
    two would close a real live listing) pass `raise_on_error=True` to have
    the exception re-raised instead.
    """
    try:
        client = JsonRpcClient(config.JSON_RPC_URL)
        response = await asyncio.to_thread(client.request, NFTSellOffers(nft_id=nft_id))
        result = response.result
        # A non-tesSUCCESS RESULT (status:error) never raised above, so strict
        # callers would otherwise misread a soft error (tooBusy, slowDown, an
        # amendment blocker, …) as "no offers" and stale-close a live listing.
        # objectNotFound is the ONLY error that legitimately means "this NFT
        # has no offers" — whitelist it (empty list) and re-raise every other
        # unsuccessful response in strict mode.
        if isinstance(result, dict) and result.get("error"):
            if str(result.get("error")) == "objectNotFound":
                return []
            if raise_on_error:
                raise RuntimeError(f"nft_sell_offers error: {result.get('error')}")
            logging.warning(f"get_nft_sell_offers error for {nft_id}: {result.get('error')}")
            return []
        offers = result.get("offers") if isinstance(result, dict) else None
        if not isinstance(offers, list):
            return []
        normalized: list[dict[str, Any]] = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            normalized.append(
                {
                    "offer_index": offer.get("nft_offer_index", offer.get("index")),
                    "amount": offer.get("amount"),
                    "destination": offer.get("destination"),
                    "flags": offer.get("flags"),
                    "owner": offer.get("owner"),
                }
            )
        return normalized
    except Exception as e:
        if raise_on_error:
            raise
        logging.warning(f"get_nft_sell_offers failed for {nft_id}: {e}")
        return []


async def get_tx(tx_hash: str) -> dict[str, Any]:
    """Fetch a transaction by hash via the plain (non-clio) `tx` method, so
    this goes through JSON_RPC_URL like mint/burn/offer, not CLIO_WS_URL.

    Returns the raw result dict verbatim, including the not-yet-known-to-the-
    server shape (`{"error": "txnNotFound", ...}`, no "validated"/"meta"
    keys) — callers check `result.get("validated")`, which is falsy for both
    "not found yet" and "found but not validated", so this needs no special-
    casing for the not-found shape.

    Raises on a genuine RPC/network/connection failure (unlike
    get_nft_sell_offers, this does NOT swallow exceptions) — the marketplace
    list/buy finalize pollers (lfg_service/app.py, via lfg_core/market_flow.py)
    are fail-closed on writes and must be able to tell "the lookup itself
    broke" apart from "still pending"."""
    client = JsonRpcClient(config.JSON_RPC_URL)
    response = await asyncio.to_thread(client.request, Tx(transaction=tx_hash))
    return response.result


async def get_trustline_balance(address: str, currency: str, issuer: str) -> Decimal | None:
    """Balance `address` holds on its trustline to issuer/currency, as a
    Decimal — or None if there is no trustline or the lookup failed (callers
    treat both the same: not a holder)."""
    try:
        marker = None
        async with AsyncWebsocketClient(config.WS_URL) as websocket:
            while True:
                response = await websocket.request(
                    AccountLines(account=address, peer=issuer, marker=marker, limit=400)
                )
                result = response.result
                for line in result.get("lines", []):
                    if line.get("currency") == currency and line.get("account") == issuer:
                        return Decimal(line.get("balance", "0"))
                marker = result.get("marker")
                if not marker:
                    return None
    except Exception as e:
        logging.warning(f"account_lines lookup failed for {address}: {e}")
        return None


async def get_amm_xrp_cost(currency: str, issuer: str, token_amount: Decimal) -> Decimal | None:
    """XRP needed to buy `token_amount` of the token from its XRP/token AMM
    pool, including the pool's trading fee (constant-product exact-output
    quote). Returns the XRP value as a Decimal, or None if the pool cannot
    cover the amount or the lookup failed."""
    try:
        async with AsyncWebsocketClient(config.WS_URL) as websocket:
            response = await websocket.request(
                AMMInfo(asset=XRP(), asset2=IssuedCurrency(currency=currency, issuer=issuer))
            )
            amm = response.result["amm"]
        xrp_pool = Decimal(amm["amount"]) / 1_000_000  # drops -> XRP
        token_pool = Decimal(amm["amount2"]["value"])
        dy = Decimal(token_amount)
        if dy >= token_pool:
            return None
        fee = Decimal(amm.get("trading_fee", 0)) / 100_000  # 1/100000 units
        return (xrp_pool * dy / (token_pool - dy)) / (1 - fee)
    except Exception as e:
        logging.error(f"AMM quote failed for {currency}.{issuer}: {e}")
        return None


async def buy_and_burn(
    currency: str, issuer: str, value: str, max_xrp: str | None = None
) -> str | None:
    """Deliver `value` of an IOU to its own issuer — which destroys it. With
    `max_xrp` set this is a cross-currency Payment that buys the token off
    the DEX/AMM with at most that much of the bot wallet's XRP; without it,
    the bot wallet's existing token balance is spent. Returns the tx hash or
    None (callers treat the burn as best-effort)."""
    try:
        if config.SIGNING_ACCOUNT == issuer:
            # The bot wallet IS the issuer (testnet, where the SEED account
            # issues the IOU). Paying an IOU to its own issuer redeems/destroys
            # it on receipt, and you cannot send your own IOU to yourself —
            # there is nothing to burn. Return a truthy sentinel so callers'
            # `if not await buy_and_burn(...)` does not log a spurious error.
            logging.info(
                f"buy_and_burn: wallet is the issuer of {currency}; the IOU is redeemed on "
                f"receipt, nothing to burn (no-op)."
            )
            return "self-issuer-noop"
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        kwargs: dict[str, Any] = {
            "account": config.SIGNING_ACCOUNT,
            "destination": issuer,
            "amount": IssuedCurrencyAmount(currency=currency, issuer=issuer, value=value),
            "source_tag": config.SOURCE_TAG,
        }
        if max_xrp is not None:
            kwargs["send_max"] = xrp_to_drops(Decimal(max_xrp))
        response = await asyncio.to_thread(submit_and_wait, Payment(**kwargs), client, wallet)
        result = response.result["meta"]["TransactionResult"]
        if result == "tesSUCCESS":
            logging.info(f"Burned {value} {currency}: {response.result['hash']}")
            return response.result["hash"]  # type: ignore[no-any-return]
        logging.error(f"buy_and_burn result: {result}")
        return None
    except Exception:
        logging.error(f"buy_and_burn error: {traceback.format_exc()}")
        return None


async def burn_nft(nft_id: str, owner: str | None = None) -> str | None:
    """Burn an NFT held by `owner` (None = held by the issuer wallet itself)
    using the issuer wallet's burn authority. Returns the transaction hash
    or None."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        kwargs: dict[str, Any] = {
            "account": config.SIGNING_ACCOUNT,
            "nftoken_id": nft_id,
            "source_tag": config.SOURCE_TAG,
        }
        if owner and owner != config.SIGNING_ACCOUNT:
            kwargs["owner"] = owner
        burn = NFTokenBurn(**kwargs)

        retries = 5
        hash_txn = None
        for attempt in range(1, retries + 1):
            try:
                response = await asyncio.to_thread(submit_and_wait, burn, client, wallet)
                hash_txn = response.result["hash"]
                break
            except Exception as e:
                logging.error(f"Burn attempt {attempt} failed: {e}")
                if attempt == retries:
                    return None
                await asyncio.sleep(5)

        for check_attempt in range(1, retries + 1):
            try:
                txn = await asyncio.to_thread(client.request, Tx(transaction=hash_txn))
                res = txn.result
                if res["meta"]["TransactionResult"] == "tesSUCCESS":
                    logging.info(f"NFT burned: {nft_id} ({hash_txn})")
                    return hash_txn
                logging.warning(f"Burn result: {res['meta']['TransactionResult']}")
                return None
            except Exception as e:
                logging.error(f"Burn status check {check_attempt} failed: {e}")
                if check_attempt == retries:
                    return None
                await asyncio.sleep(5)
        return None
    except Exception:
        logging.error(f"burn_nft error: {traceback.format_exc()}")
        return None


async def modify_nft(nft_id: str, owner: str, uri: str) -> str | None:
    """Update a mutable NFT's URI in place via NFTokenModify (Dynamic NFTs
    amendment). `owner` is the current holder (None/issuer-wallet = held by
    the issuer wallet itself); `uri` is the plain (non-hex) new metadata URL.
    Requires the NFT to have the mutable flag. Returns the transaction hash
    or None."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        kwargs: dict[str, Any] = {
            "account": config.SIGNING_ACCOUNT,
            "nftoken_id": nft_id,
            "uri": convert_str_to_hex(uri),
            "source_tag": config.SOURCE_TAG,
        }
        if owner and owner != config.SIGNING_ACCOUNT:
            kwargs["owner"] = owner
        modify = NFTokenModify(**kwargs)

        retries = 5
        hash_txn = None
        for attempt in range(1, retries + 1):
            try:
                response = await asyncio.to_thread(submit_and_wait, modify, client, wallet)
                hash_txn = response.result["hash"]
                break
            except Exception as e:
                logging.error(f"Modify attempt {attempt} failed: {e}")
                if attempt == retries:
                    return None
                await asyncio.sleep(5)

        for check_attempt in range(1, retries + 1):
            try:
                txn = await asyncio.to_thread(client.request, Tx(transaction=hash_txn))
                res = txn.result
                if res["meta"]["TransactionResult"] == "tesSUCCESS":
                    logging.info(f"NFT modified: {nft_id} ({hash_txn})")
                    return hash_txn
                logging.warning(f"Modify result: {res['meta']['TransactionResult']}")
                return None
            except Exception as e:
                logging.error(f"Modify status check {check_attempt} failed: {e}")
                if check_attempt == retries:
                    return None
                await asyncio.sleep(5)
        return None
    except Exception:
        logging.error(f"modify_nft error: {traceback.format_exc()}")
        return None


def bot_wallet_address() -> str:
    """The account bot txs run as (mint/offer/fee account). SEED-derived by
    default; on mainnet SIGNING_ACCOUNT overrides it to the issuer address
    (SEED then holds the issuer's regular-key seed)."""
    return config.SIGNING_ACCOUNT


RIPPLE_EPOCH_OFFSET = 946684800  # seconds between the Unix and Ripple epochs


def _extract_tx_and_meta(message: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    """Pull (tx, meta) out of a subscription stream message or an account_tx
    entry. rippled API v1 nests the transaction under 'transaction'/'tx';
    API v2 (the default for current xrpl-py) uses 'tx_json'."""
    if not isinstance(message, dict):
        return None, None
    tx = message.get("tx_json") or message.get("transaction") or message.get("tx")
    if not isinstance(tx, dict):
        return None, None
    meta = message.get("meta") or message.get("metaData")
    return tx, meta


def _payment_matches(
    tx: dict[str, Any],
    meta: Any,
    destination: str,
    expected_sender: str,
    expected_amount: str,
    currency: str,
    issuer: str,
) -> bool:
    if tx.get("TransactionType") != "Payment":
        return False
    if tx.get("Account", "") != expected_sender:
        return False
    if tx.get("Destination") != destination:
        return False
    # Prefer the validated delivered amount (also guards against partial
    # payments); fall back to Amount (API v1) / DeliverMax (API v2).
    amount = None
    if isinstance(meta, dict):
        amount = meta.get("delivered_amount") or meta.get("DeliveredAmount")
    if amount is None:
        amount = tx.get("Amount", tx.get("DeliverMax"))
    if currency == "XRP":
        # Native XRP amounts are drops strings; expected_amount is in XRP.
        if isinstance(amount, dict):
            return False
        try:
            return Decimal(amount) >= Decimal(xrp_to_drops(Decimal(expected_amount)))  # type: ignore[arg-type]
        except (InvalidOperation, TypeError, ValueError):
            return False
    if not isinstance(amount, dict):
        return False
    if amount.get("currency") != currency or amount.get("issuer") != issuer:
        return False
    try:
        return Decimal(amount.get("value", "0")) >= Decimal(expected_amount)
    except (InvalidOperation, TypeError):
        return False


def _tx_unix_time(entry: dict[str, Any], tx: dict[str, Any]) -> float | None:
    """Validation time of an account_tx entry as a Unix timestamp, or None."""
    date = tx.get("date")
    if isinstance(date, (int, float)):
        return date + RIPPLE_EPOCH_OFFSET
    iso = entry.get("close_time_iso")  # API v2 puts the time on the entry
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


async def _recent_payment_exists(
    websocket: Any, account: str, matches: Callable[..., bool], not_before_unix: float
) -> bool:
    """Check already-validated transactions for a matching payment. Covers
    payments that land between the payment link being shown to the user and
    the live subscription becoming active."""
    response = await websocket.request(AccountTx(account=account, limit=20))
    for entry in response.result.get("transactions", []):
        if not entry.get("validated", True):
            continue
        tx, meta = _extract_tx_and_meta(entry)
        if tx is None:
            continue
        when = _tx_unix_time(entry, tx)
        # Unknown-age transactions are skipped so an old payment can't be
        # replayed for a free mint.
        if when is None or when < not_before_unix:
            continue
        if matches(tx, meta):
            return True
    return False


async def wait_for_payment(
    destination: str,
    expected_sender: str,
    expected_amount: str = "1",
    timeout_seconds: int | None = None,
    not_before: float | None = None,
    currency: str | None = None,
    issuer: str | None = None,
) -> bool:
    """
    Subscribe to the destination account and wait for a token payment from
    expected_sender. Sender verification prevents one user's payment from
    triggering another user's mint. `not_before` (Unix time, default now-10s)
    bounds the backfill check for payments that landed before the
    subscription was active. currency/issuer default to the LFGO mint token;
    pass others (e.g. BRIX) for swap fees.
    """
    timeout_seconds = timeout_seconds or config.PAYMENT_TIMEOUT_SECONDS
    currency = currency or config.TOKEN_CURRENCY_HEX
    issuer = issuer or config.TOKEN_ISSUER_ADDRESS
    start_time = time.time()
    deadline = start_time + timeout_seconds
    if not_before is None:
        not_before = start_time - 10
    context = f"{expected_amount} {currency} from {expected_sender} to {destination}"

    def matches(tx: dict[str, Any], meta: Any) -> bool:
        return _payment_matches(
            tx, meta, destination, expected_sender, expected_amount, currency, issuer
        )

    async def watch(websocket: Any) -> bool:
        async for message in websocket:
            tx, meta = _extract_tx_and_meta(message)
            if tx and matches(tx, meta):
                logging.info(f"✅ Payment received from {expected_sender}: {tx.get('hash')}")
                return True
        return False  # stream closed without a matching payment

    # A dropped websocket must not look like "payment never arrived": keep
    # reconnecting until the deadline, re-checking recent history each time
    # to catch a payment that validated while the connection was down.
    reconnects = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            logging.warning(
                f"Payment wait timed out after {timeout_seconds}s "
                f"({context}; {reconnects} reconnects)"
            )
            return False
        try:
            async with AsyncWebsocketClient(config.WS_URL) as websocket:
                await websocket.send(Subscribe(accounts=[destination]))
                logging.info(
                    f"Subscribed to {destination}; waiting up to {int(remaining)}s for {context}"
                )

                if await asyncio.wait_for(
                    _recent_payment_exists(websocket, destination, matches, not_before),
                    timeout=max(1, min(remaining, 15)),
                ):
                    logging.info(f"✅ Payment found in recent history ({context})")
                    return True

                if await asyncio.wait_for(watch(websocket), timeout=remaining):
                    return True
                logging.warning(f"Payment subscription stream closed; reconnecting ({context})")
        except asyncio.TimeoutError:
            # Only terminal once the overall deadline is spent — a stalled
            # history check times out well before that and just reconnects.
            if time.time() >= deadline:
                logging.warning(
                    f"Payment wait timed out after {timeout_seconds}s "
                    f"({context}; {reconnects} reconnects)"
                )
                return False
            logging.warning(f"Payment history check timed out; reconnecting ({context})")
        except Exception as e:
            logging.error(f"Payment subscription error ({context}): {e}")
            logging.error(traceback.format_exc())
        await asyncio.sleep(min(2**reconnects, 15))
        reconnects += 1
