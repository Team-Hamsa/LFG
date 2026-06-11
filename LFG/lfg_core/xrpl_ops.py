# lfg_core/xrpl_ops.py
# XRPL operations: mint, offer creation, payment watching (extracted from main.py).

import time
import asyncio
import logging
import traceback
from datetime import datetime
from decimal import Decimal, InvalidOperation

from xrpl.clients import JsonRpcClient
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.wallet import Wallet
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.transactions import (NFTokenMint, NFTokenCreateOffer,
                                      NFTokenBurn, NFTokenModify)
from xrpl.models.transactions.nftoken_create_offer import NFTokenCreateOfferFlag
from xrpl.models.requests import Tx, Subscribe, AccountNFTs, AccountTx
from xrpl.transaction import submit_and_wait

from lfg_core import config

# On-ledger NFToken flag bits (mirror the tf* mint flags)
NFT_FLAG_BURNABLE = 0x0001
NFT_FLAG_MUTABLE = 0x0010


def convert_str_to_hex(string: str) -> str:
    """Convert string to hex for XRPL URI"""
    return string.encode('utf-8').hex().upper()


async def mint_nft(metadata_cdn_url: str, taxon: int, issuer: str):
    """Mint an NFT on XRPL; returns the NFToken ID or None."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)

        kwargs = dict(
            account=wallet.classic_address,
            uri=convert_str_to_hex(metadata_cdn_url),
            nftoken_taxon=taxon,
            transfer_fee=config.NFT_TRANSFER_FEE,
            flags=config.NFT_FLAGS,
        )
        if issuer != wallet.classic_address:
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
                        return nft_id
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


async def create_nft_offer(nft_id: str, destination: str, amount="0"):
    """Create a sell offer transferring the NFT to destination; returns offer ID
    or None. amount may be an XRP-drops string or an IssuedCurrencyAmount."""
    try:
        client = JsonRpcClient(config.JSON_RPC_URL)
        wallet = Wallet.from_seed(config.SEED)

        offer = NFTokenCreateOffer(
            account=wallet.classic_address,
            destination=destination,
            amount=amount,
            nftoken_id=nft_id,
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
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
                    return offer_id
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


async def get_account_nfts(address: str, issuer: str):
    """List NFTs held by `address` that were issued by `issuer`.
    Returns a list of {"nft_id", "uri_hex", "flags"} dicts."""
    nfts = []
    marker = None
    async with AsyncWebsocketClient(config.WS_URL) as websocket:
        while True:
            response = await websocket.request(
                AccountNFTs(account=address, marker=marker, limit=400))
            result = response.result
            for nft in result.get("account_nfts", []):
                if nft.get("Issuer") != issuer:
                    continue
                nfts.append({"nft_id": nft["NFTokenID"],
                             "uri_hex": nft.get("URI", ""),
                             "flags": nft.get("Flags", 0)})
            marker = result.get("marker")
            if not marker:
                break
    return nfts


async def burn_nft(nft_id: str, owner: str = None):
    """Burn an NFT held by `owner` (None = held by the issuer wallet itself)
    using the issuer wallet's burn authority. Returns the transaction hash
    or None."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        kwargs = dict(account=wallet.classic_address, nftoken_id=nft_id)
        if owner and owner != wallet.classic_address:
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


async def modify_nft(nft_id: str, owner: str, uri: str):
    """Update a mutable NFT's URI in place via NFTokenModify (Dynamic NFTs
    amendment). `owner` is the current holder (None/issuer-wallet = held by
    the issuer wallet itself); `uri` is the plain (non-hex) new metadata URL.
    Requires the NFT to have the mutable flag. Returns the transaction hash
    or None."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        kwargs = dict(account=wallet.classic_address, nftoken_id=nft_id,
                      uri=convert_str_to_hex(uri))
        if owner and owner != wallet.classic_address:
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
    """Classic address of the wallet behind SEED (mint/offer/fee account)."""
    return Wallet.from_seed(config.SEED).classic_address


RIPPLE_EPOCH_OFFSET = 946684800  # seconds between the Unix and Ripple epochs


def _extract_tx_and_meta(message: dict):
    """Pull (tx, meta) out of a subscription stream message or an account_tx
    entry. rippled API v1 nests the transaction under 'transaction'/'tx';
    API v2 (the default for current xrpl-py) uses 'tx_json'."""
    if not isinstance(message, dict):
        return None, None
    tx = message.get('tx_json') or message.get('transaction') or message.get('tx')
    if not isinstance(tx, dict):
        return None, None
    meta = message.get('meta') or message.get('metaData')
    return tx, meta


def _payment_matches(tx: dict, meta, destination: str, expected_sender: str,
                     expected_amount: str, currency: str, issuer: str) -> bool:
    if tx.get('TransactionType') != 'Payment':
        return False
    if tx.get('Account', '') != expected_sender:
        return False
    if tx.get('Destination') != destination:
        return False
    # Prefer the validated delivered amount (also guards against partial
    # payments); fall back to Amount (API v1) / DeliverMax (API v2).
    amount = None
    if isinstance(meta, dict):
        amount = meta.get('delivered_amount') or meta.get('DeliveredAmount')
    if amount is None:
        amount = tx.get('Amount', tx.get('DeliverMax'))
    if not isinstance(amount, dict):
        return False
    if amount.get('currency') != currency or amount.get('issuer') != issuer:
        return False
    try:
        return Decimal(amount.get('value', '0')) >= Decimal(expected_amount)
    except (InvalidOperation, TypeError):
        return False


def _tx_unix_time(entry: dict, tx: dict):
    """Validation time of an account_tx entry as a Unix timestamp, or None."""
    date = tx.get('date')
    if isinstance(date, (int, float)):
        return date + RIPPLE_EPOCH_OFFSET
    iso = entry.get('close_time_iso')  # API v2 puts the time on the entry
    if iso:
        try:
            return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


async def _recent_payment_exists(websocket, account: str, matches,
                                 not_before_unix: float) -> bool:
    """Check already-validated transactions for a matching payment. Covers
    payments that land between the payment link being shown to the user and
    the live subscription becoming active."""
    response = await websocket.request(AccountTx(account=account, limit=20))
    for entry in response.result.get('transactions', []):
        if not entry.get('validated', True):
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


async def wait_for_payment(destination: str, expected_sender: str,
                           expected_amount: str = "1",
                           timeout_seconds: int = None,
                           not_before: float = None,
                           currency: str = None,
                           issuer: str = None) -> bool:
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
    context = (f"{expected_amount} {currency} from {expected_sender} "
               f"to {destination}")

    def matches(tx, meta):
        return _payment_matches(tx, meta, destination, expected_sender,
                                expected_amount, currency, issuer)

    async def watch(websocket):
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
                f"({context}; {reconnects} reconnects)")
            return False
        try:
            async with AsyncWebsocketClient(config.WS_URL) as websocket:
                await websocket.send(Subscribe(accounts=[destination]))
                logging.info(f"Subscribed to {destination}; waiting up to "
                             f"{int(remaining)}s for {context}")

                if await _recent_payment_exists(websocket, destination,
                                                matches, not_before):
                    logging.info(f"✅ Payment found in recent history ({context})")
                    return True

                if await asyncio.wait_for(watch(websocket), timeout=remaining):
                    return True
                logging.warning(f"Payment subscription stream closed; "
                                f"reconnecting ({context})")
        except asyncio.TimeoutError:
            logging.warning(
                f"Payment wait timed out after {timeout_seconds}s "
                f"({context}; {reconnects} reconnects)")
            return False
        except Exception as e:
            logging.error(f"Payment subscription error ({context}): {e}")
            logging.error(traceback.format_exc())
        await asyncio.sleep(min(2 ** reconnects, 15))
        reconnects += 1
