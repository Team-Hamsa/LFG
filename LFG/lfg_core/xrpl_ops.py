# lfg_core/xrpl_ops.py
# XRPL operations: mint, offer creation, payment watching (extracted from main.py).

import json
import time
import asyncio
import logging
import traceback

from xrpl.clients import JsonRpcClient
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.wallet import Wallet
from xrpl.models.transactions import NFTokenMint, NFTokenCreateOffer
from xrpl.models.transactions.nftoken_create_offer import NFTokenCreateOfferFlag
from xrpl.models.requests import Tx, Subscribe
from xrpl.transaction import submit_and_wait

from lfg_core import config


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


async def create_nft_offer(nft_id: str, destination: str):
    """Create a zero-amount sell offer transferring the NFT; returns offer ID or None."""
    try:
        client = JsonRpcClient(config.JSON_RPC_URL)
        wallet = Wallet.from_seed(config.SEED)

        offer = NFTokenCreateOffer(
            account=wallet.classic_address,
            destination=destination,
            amount="0",
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


def _payment_matches(tx: dict, destination: str, expected_sender: str,
                     expected_amount: str, currency: str, issuer: str) -> bool:
    if tx.get('TransactionType') != 'Payment':
        return False
    if tx.get('Account', '') != expected_sender:
        return False
    if tx.get('Destination') != destination:
        return False
    amount = tx.get('Amount', {})
    return (isinstance(amount, dict)
            and amount.get('currency') == currency
            and amount.get('issuer') == issuer
            and amount.get('value') == expected_amount)


async def wait_for_payment(destination: str, expected_sender: str,
                           expected_amount: str = "1",
                           timeout_seconds: int = None) -> bool:
    """
    Subscribe to the destination account and wait for a token payment from
    expected_sender. Sender verification prevents one user's payment from
    triggering another user's mint.
    """
    timeout_seconds = timeout_seconds or config.PAYMENT_TIMEOUT_SECONDS
    currency = config.TOKEN_CURRENCY_HEX
    issuer = config.TOKEN_ISSUER_ADDRESS
    start_time = time.time()

    try:
        async with AsyncWebsocketClient(config.WS_URL) as websocket:
            await websocket.send(Subscribe(accounts=[destination]))
            logging.info(f"Subscribed to {destination}; waiting for payment from {expected_sender}")

            async for message in websocket:
                if time.time() - start_time > timeout_seconds:
                    logging.info("Payment subscription timeout")
                    return False

                tx = None
                if message.get('type') == 'transaction' and 'transaction' in message:
                    tx = message['transaction']
                elif message.get('account') == destination and 'transaction' in message:
                    tx = message['transaction']

                if tx and _payment_matches(tx, destination, expected_sender,
                                           expected_amount, currency, issuer):
                    logging.info(f"✅ Payment received from {expected_sender}: {tx.get('hash')}")
                    return True

    except asyncio.TimeoutError:
        logging.info("Payment subscription timeout")
        return False
    except Exception as e:
        logging.error(f"Error in payment subscription: {e}")
        logging.error(traceback.format_exc())
        return False

    return False
