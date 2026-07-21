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
    AccountObjects,
    AccountObjectType,
    AccountTx,
    AMMInfo,
    Ledger,
    NFTBuyOffers,
    NFTSellOffers,
    Subscribe,
    Tx,
)
from xrpl.models.transactions import (
    NFTokenBurn,
    NFTokenCancelOffer,
    NFTokenCreateOffer,
    NFTokenMint,
    NFTokenModify,
    Payment,
)
from xrpl.models.transactions.nftoken_create_offer import NFTokenCreateOfferFlag
from xrpl.models.transactions.transaction import Transaction
from xrpl.transaction import autofill_and_sign, submit_and_wait
from xrpl.utils import get_nftoken_id, xrp_to_drops
from xrpl.wallet import Wallet

from lfg_core import config, memos, payment_ledger

# On-ledger NFToken flag bits (mirror the tf* mint flags)
NFT_FLAG_BURNABLE = 0x0001
TF_TRANSFERABLE = 0x0008
NFT_FLAG_MUTABLE = 0x0010


class IndeterminateResultError(RuntimeError):
    """The on-ledger outcome of a submitted transaction could not be determined.

    Submission raised (timeout / network error) AND a follow-up lookup of the
    exact transaction hash did not return a validated result, so the transaction
    MAY or MAY NOT have committed. Callers MUST treat this as neither success nor
    definitive failure: never run on-chain compensation and never blind-resubmit
    — reconcile from chain / fail closed instead.

    It is deliberately distinct from a None return, which means a DEFINITIVE,
    validated failure (or that no transaction was ever forwarded). In the trait
    economy this raise is what makes closet_token.sync_closet surface
    ClosetIndeterminateError so the phase-aware _sync_then_persist taxonomy (#107)
    engages instead of collapsing an unknown outcome to a plain ClosetError
    ('did NOT commit') and running an asset-destroying compensation (#179)."""


def convert_str_to_hex(string: str) -> str:
    """Convert string to hex for XRPL URI"""
    return string.encode("utf-8").hex().upper()


def _validated_result(result: dict[str, Any], label: str) -> dict[str, Any] | None:
    """Classify a VALIDATED transaction result dict: return it on tesSUCCESS,
    else None (a definitive on-ledger failure)."""
    meta = result.get("meta")
    tx_result = meta.get("TransactionResult") if isinstance(meta, dict) else None
    if tx_result == "tesSUCCESS":
        return result
    logging.warning(f"{label} result: {tx_result}")
    return None


async def _confirm_by_hash(
    client: JsonRpcClient, tx_hash: str, attempts: int = 3
) -> dict[str, Any] | None:
    """Look the transaction up by hash and return its result dict IFF the ledger
    reports it VALIDATED (any TransactionResult); else None (not found yet, not
    validated, or the lookup itself failed). Used only after a submit raised, to
    decide committed vs. indeterminate WITHOUT resubmitting a fresh transaction."""
    for attempt in range(attempts):
        try:
            response = await asyncio.to_thread(client.request, Tx(transaction=tx_hash))
            result = response.result
            if (
                isinstance(result, dict)
                and result.get("validated")
                and isinstance(result.get("meta"), dict)
            ):
                return result
        except Exception as e:
            logging.warning(f"tx confirm lookup failed for {tx_hash}: {e}")
        if attempt + 1 < attempts:
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _submit_and_confirm(
    tx: Transaction, wallet: Wallet, client: JsonRpcClient, label: str
) -> dict[str, Any] | None:
    """Sign `tx` ONCE, submit it, and confirm the outcome from the ledger.

    Returns the validated result dict on tesSUCCESS; None on a definitive,
    validated failure; raises IndeterminateResultError when the outcome cannot be
    determined.

    Signing once fixes the transaction hash and LastLedgerSequence, so a
    submission that raises is never blind-resubmitted as a fresh (duplicate)
    transaction — submit_and_wait already polls across ledgers until
    LastLedgerSequence, so if it raised the tx may still have landed. Instead of
    resubmitting, the prior hash is looked up on-ledger and only its validated
    outcome is trusted; an unconfirmable outcome fails closed as indeterminate.
    (This also removes the duplicate-mint risk of the old blind retry loop, #179.)"""
    signed = await asyncio.to_thread(autofill_and_sign, tx, client, wallet)
    try:
        # Pass wallet=None: `signed` is already signed, so submit_and_wait must
        # not re-sign/re-autofill it — otherwise the submitted tx could differ
        # from signed.get_hash() and the exception path would confirm the wrong
        # hash, marking an actually-submitted tx as indeterminate (#188).
        response = await asyncio.to_thread(submit_and_wait, signed, client, None, autofill=False)
    except Exception as e:
        logging.warning(f"{label}: submit_and_wait raised ({e}); confirming by hash")
        confirmed = await _confirm_by_hash(client, signed.get_hash())
        if confirmed is None:
            raise IndeterminateResultError(
                f"{label}: on-ledger outcome unknown after submit raised ({e})"
            ) from e
        return _validated_result(confirmed, label)
    return _validated_result(response.result, label)


async def mint_nft(
    metadata_cdn_url: str,
    taxon: int,
    issuer: str,
    flags: int | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
    action: str = memos.ACTION_MINT,
) -> str | None:
    """Mint an NFT on XRPL; returns the NFToken ID or None. `flags` overrides
    config.NFT_FLAGS (e.g. burnable economy characters / soulbound buckets).
    `platform` records the originating surface in the provenance memo (#54);
    `action` the app operation (economy assembles/extracts pass their own so
    the memo distinguishes them from plain mints and legacy remint swaps)."""
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
            "memos": memos.build_memo_models(memos.INITIATOR_BACKEND, platform, action, campaign),
        }
        # TransferFee is only valid on transferable tokens; XRPL rejects it as
        # temMALFORMED otherwise (e.g. the soulbound Bucket, flags=16).
        if eff_flags & TF_TRANSFERABLE:
            kwargs["transfer_fee"] = config.NFT_TRANSFER_FEE
        if issuer != config.SIGNING_ACCOUNT:
            kwargs["issuer"] = issuer
        payment = NFTokenMint(**kwargs)

        # submit_and_wait already returns only after the tx validates, so its
        # response IS the on-ledger outcome — no separate (flaky) Tx re-check
        # that could turn a committed mint into a false failure.
        result = await _submit_and_confirm(payment, wallet, client, "NFTokenMint")
        if result is None:
            return None  # definitive, validated failure
        meta = result["meta"]
        nft_id = meta.get("nftoken_id") if isinstance(meta, dict) else None
        if not nft_id:
            # The convenience meta.nftoken_id field is not always present; the
            # mint DID validate (tesSUCCESS), so the token exists on-chain.
            # Derive the id from the affected nodes rather than returning None
            # (which callers read as a definitive failure and would compensate
            # against an asset that already exists, #188).
            try:
                nft_id = get_nftoken_id(meta)
            except Exception:
                nft_id = None
        if nft_id:
            logging.info(f"NFT minted: {nft_id}")
            return str(nft_id)
        # Committed but unidentifiable: fail closed as indeterminate, never as a
        # definitive-failure None — the NFT is on-ledger and must not be treated
        # as "mint failed".
        raise IndeterminateResultError(
            "NFTokenMint validated (tesSUCCESS) but its NFTokenID could not be resolved from meta"
        )

    except IndeterminateResultError:
        raise  # never collapse an unknown outcome to a definitive-failure None
    except Exception:
        logging.error(f"mint_nft error: {traceback.format_exc()}")
        return None


async def create_nft_offer(
    nft_id: str,
    destination: str,
    amount: Any = "0",
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
    expiration: int | None = None,
    action: str = memos.ACTION_CREATE_OFFER,
) -> str | None:
    """Create a sell offer transferring the NFT to destination; returns offer ID
    or None. amount may be an XRP-drops string or an IssuedCurrencyAmount.
    expiration is a ripple-epoch timestamp; omitted from serialization when
    None. action lets callers (e.g. the trait shop) stamp non-default memo
    provenance."""
    try:
        client = JsonRpcClient(config.JSON_RPC_URL)
        wallet = Wallet.from_seed(config.SEED)

        offer = NFTokenCreateOffer(
            account=config.SIGNING_ACCOUNT,
            destination=destination,
            amount=amount,
            nftoken_id=nft_id,
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
            expiration=expiration,
            source_tag=config.SOURCE_TAG,
            memos=memos.build_memo_models(memos.INITIATOR_BACKEND, platform, action, campaign),
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


async def cancel_nft_offer(offer_index: str, platform: str = memos.PLATFORM_BACKEND) -> str | None:
    """Cancel an issuer-created NFTokenOffer (e.g. an expired/orphaned Trait
    Shop sell offer) using the issuer wallet's own signing authority. Returns
    the transaction hash, or None on a definitive failure — including the
    benign case where the ledger object is already gone (accepted or
    previously cancelled): callers that only want the offer purged before an
    idempotent follow-up (e.g. the shop expiry sweep) should treat any None
    here as safe to ignore and proceed."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        cancel = NFTokenCancelOffer(
            account=config.SIGNING_ACCOUNT,
            nftoken_offers=[offer_index],
            source_tag=config.SOURCE_TAG,
            memos=memos.build_memo_models(
                memos.INITIATOR_BACKEND, platform, memos.ACTION_CANCEL_OFFER
            ),
        )
        result = await _submit_and_confirm(cancel, wallet, client, "NFTokenCancelOffer")
        if result is None:
            return None  # definitive failure (incl. offer already gone)
        tx_hash: str = result["hash"]
        logging.info(f"NFT offer cancelled: {offer_index} ({tx_hash})")
        return tx_hash
    except IndeterminateResultError:
        raise  # never collapse an unknown outcome to a definitive-failure None
    except Exception:
        logging.error(f"cancel_nft_offer error: {traceback.format_exc()}")
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
    `{offer_index, amount, destination, flags, owner, expiration}`.
    `offer_index` accepts either the `nft_offer_index` or `index` field —
    different server versions key the offer's ledger index differently (drift
    guard, mirrors Baysed market.py:386-390). `expiration` is the offer's
    XRPL `Expiration` (Ripple-epoch seconds) or None when the offer never
    expires; `market_ops.verify_sell_offer` uses it to reject an already-
    expired offer before a buyer signs a doomed accept (#183).

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
                    "expiration": offer.get("expiration"),
                }
            )
        return normalized
    except Exception as e:
        if raise_on_error:
            raise
        logging.warning(f"get_nft_sell_offers failed for {nft_id}: {e}")
        return []


# NFTokenOffer ledger-object flag: this offer SELLS the token (vs a buy bid).
LSF_SELL_NFTOKEN = 0x00000001


async def get_account_nft_offers(address: str) -> list[dict[str, Any]]:
    """Every live NFTokenOffer object OWNED by `address`, via paginated
    `account_objects` (one call per page instead of one `nft_sell_offers`
    call per token). Used by the pending-offers tray (#218): the app's
    gift/mint offers are all created by the signing account, so its account
    objects are the complete set of claimable offers.

    Each dict is normalized to `{offer_index, nft_id, amount, destination,
    flags, owner, expiration}` — the same shape as get_nft_sell_offers plus
    `nft_id`. Always raises on RPC/soft errors (callers are fail-closed
    verifiers or 503 the read; an empty list must mean "genuinely none")."""
    out: list[dict[str, Any]] = []
    client = JsonRpcClient(config.JSON_RPC_URL)
    marker: Any = None
    while True:
        req = AccountObjects(
            account=address,
            type=AccountObjectType.NFT_OFFER,
            limit=400,
            marker=marker,
        )
        response = await asyncio.to_thread(client.request, req)
        result = response.result
        if not isinstance(result, dict) or result.get("error"):
            err = result.get("error") if isinstance(result, dict) else "malformed result"
            raise RuntimeError(f"account_objects error for {address}: {err}")
        for obj in result.get("account_objects") or []:
            if not isinstance(obj, dict) or obj.get("LedgerEntryType") != "NFTokenOffer":
                continue
            out.append(
                {
                    "offer_index": obj.get("index"),
                    "nft_id": obj.get("NFTokenID"),
                    "amount": obj.get("Amount"),
                    "destination": obj.get("Destination"),
                    "flags": obj.get("Flags"),
                    "owner": obj.get("Owner", address),
                    "expiration": obj.get("Expiration"),
                }
            )
        marker = result.get("marker")
        if not marker:
            return out


def filter_claimable_offers(
    offers: list[dict[str, Any]], wallet: str, now_unix: float
) -> list[dict[str, Any]]:
    """The subset of get_account_nft_offers() rows `wallet` can claim: sell
    offers destination-locked to that wallet and not expired. Pure (Node-free
    unit target, tests/test_pending_offers.py). Offers with no Expiration
    never expire — the bulk/single mint gift offers (#215) are all such."""
    claimable = []
    for o in offers:
        if not (o.get("flags") or 0) & LSF_SELL_NFTOKEN:
            continue
        if o.get("destination") != wallet:
            continue
        exp = o.get("expiration")
        if exp is not None and exp + RIPPLE_EPOCH_OFFSET <= now_unix:
            continue
        claimable.append(o)
    return claimable


async def get_nft_buy_offers(nft_id: str, raise_on_error: bool = False) -> list[dict[str, Any]]:
    """List BUY offers (bids, #283) for `nft_id` via the standard rippled
    `nft_buy_offers` method — the buy-side twin of get_nft_sell_offers, with
    identical normalization, objectNotFound whitelisting, and strict-mode
    raise semantics (see that function's docstring)."""
    try:
        client = JsonRpcClient(config.JSON_RPC_URL)
        response = await asyncio.to_thread(client.request, NFTBuyOffers(nft_id=nft_id))
        result = response.result
        if isinstance(result, dict) and result.get("error"):
            if str(result.get("error")) == "objectNotFound":
                return []
            if raise_on_error:
                raise RuntimeError(f"nft_buy_offers error: {result.get('error')}")
            logging.warning(f"get_nft_buy_offers error for {nft_id}: {result.get('error')}")
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
                    "expiration": offer.get("expiration"),
                }
            )
        return normalized
    except Exception as e:
        if raise_on_error:
            raise
        logging.warning(f"get_nft_buy_offers failed for {nft_id}: {e}")
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


async def get_ledger_time() -> int:
    """The most-recently-validated ledger's close time, in **Ripple-epoch
    seconds** — the same epoch an NFTokenOffer's `Expiration` field uses, so an
    offer's Expiration can be compared against it directly with no conversion.
    Fetched via the plain (non-clio) `ledger` method through JSON_RPC_URL, like
    mint/burn/offer/get_tx.

    Raises on an RPC/network failure or a malformed response (like get_tx, and
    unlike get_nft_sell_offers, this does NOT swallow) so a fail-closed caller
    (`market_ops.verify_sell_offer`) can tell "the lookup itself broke" apart
    from a real answer and refuse to hand the buyer a doomed payload."""
    client = JsonRpcClient(config.JSON_RPC_URL)
    response = await asyncio.to_thread(client.request, Ledger(ledger_index="validated"))
    result = response.result
    ledger = result.get("ledger") if isinstance(result, dict) else None
    close_time = ledger.get("close_time") if isinstance(ledger, dict) else None
    if not isinstance(close_time, int):
        raise RuntimeError(f"ledger response missing close_time: {result!r}")
    return close_time


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
            "memos": memos.build_memo_models(
                memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_BUY_AND_BURN
            ),
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


async def burn_nft(
    nft_id: str, owner: str | None = None, platform: str = memos.PLATFORM_BACKEND
) -> str | None:
    """Burn an NFT held by `owner` (None = held by the issuer wallet itself)
    using the issuer wallet's burn authority. Returns the transaction hash
    or None. `platform` records the originating surface in the memo (#54)."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        kwargs: dict[str, Any] = {
            "account": config.SIGNING_ACCOUNT,
            "nftoken_id": nft_id,
            "source_tag": config.SOURCE_TAG,
            "memos": memos.build_memo_models(memos.INITIATOR_BACKEND, platform, memos.ACTION_BURN),
        }
        if owner and owner != config.SIGNING_ACCOUNT:
            kwargs["owner"] = owner
        burn = NFTokenBurn(**kwargs)

        result = await _submit_and_confirm(burn, wallet, client, "NFTokenBurn")
        if result is None:
            return None  # definitive, validated failure
        tx_hash: str = result["hash"]
        logging.info(f"NFT burned: {nft_id} ({tx_hash})")
        return tx_hash

    except IndeterminateResultError:
        raise  # never collapse an unknown outcome to a definitive-failure None
    except Exception:
        logging.error(f"burn_nft error: {traceback.format_exc()}")
        return None


async def modify_nft(
    nft_id: str, owner: str, uri: str, platform: str = memos.PLATFORM_BACKEND
) -> str | None:
    """Update a mutable NFT's URI in place via NFTokenModify (Dynamic NFTs
    amendment). `owner` is the current holder (None/issuer-wallet = held by
    the issuer wallet itself); `uri` is the plain (non-hex) new metadata URL.
    Requires the NFT to have the mutable flag. Returns the transaction hash
    or None. `platform` records the originating surface in the memo (#54)."""
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        kwargs: dict[str, Any] = {
            "account": config.SIGNING_ACCOUNT,
            "nftoken_id": nft_id,
            "uri": convert_str_to_hex(uri),
            "source_tag": config.SOURCE_TAG,
            "memos": memos.build_memo_models(
                memos.INITIATOR_BACKEND, platform, memos.ACTION_MODIFY
            ),
        }
        if owner and owner != config.SIGNING_ACCOUNT:
            kwargs["owner"] = owner
        modify = NFTokenModify(**kwargs)

        result = await _submit_and_confirm(modify, wallet, client, "NFTokenModify")
        if result is None:
            return None  # definitive, validated failure
        tx_hash: str = result["hash"]
        logging.info(f"NFT modified: {nft_id} ({tx_hash})")
        return tx_hash

    except IndeterminateResultError:
        raise  # never collapse an unknown outcome to a definitive-failure None
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
    # A validated tec... payment moved no funds and has no delivered_amount,
    # so the DeliverMax fallback below would happily match it — refuse any
    # explicit non-success result before looking at amounts (#197 review).
    if isinstance(meta, dict):
        tx_result = meta.get("TransactionResult")
        if tx_result is not None and tx_result != "tesSUCCESS":
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


def _tx_hash(entry: dict[str, Any], tx: dict[str, Any]) -> str | None:
    """Tx hash of a stream message or account_tx entry across API versions:
    v2 puts it on the entry/message, v1 inside the transaction object."""
    h = entry.get("hash") or tx.get("hash")
    return h if isinstance(h, str) else None


async def _recent_payment_exists(
    websocket: Any,
    account: str,
    claim: Callable[[dict[str, Any], Any, dict[str, Any]], bool],
    not_before_unix: float,
) -> bool:
    """Check already-validated transactions for a claimable payment. Covers
    payments that land between the payment link being shown to the user and
    the live subscription becoming active — and, when the caller widened
    not_before for credits (issue #196) or resumed a durable bulk record
    (#228), payments from before this process was listening.

    The scan is time-bounded, never page-bounded: it pages via marker until
    the first entry older than not_before_unix (account_tx returns
    newest-first) or history ends, so a valid payment can't be stranded
    behind busy issuer traffic that accumulated while the service was down.
    For a live session not_before is ~start-10s, so this is a single page in
    practice. A progress guard aborts (loudly) if a page fails to reach
    strictly older transactions, so a server that returns markers forever
    cannot loop the scan."""
    marker = None
    prev_oldest: float | None = None
    while True:
        request = AccountTx(account=account, limit=200, marker=marker)
        response = await websocket.request(request)
        oldest: float | None = None
        for entry in response.result.get("transactions", []):
            if not entry.get("validated", True):
                continue
            tx, meta = _extract_tx_and_meta(entry)
            if tx is None:
                continue
            when = _tx_unix_time(entry, tx)
            # Unknown-age transactions are skipped so an old payment can't be
            # replayed for a free mint.
            if when is None:
                continue
            oldest = when if oldest is None else min(oldest, when)
            if when < not_before_unix:
                return False  # newest-first: everything after this is older
            if claim(tx, meta, entry):
                return True
        marker = response.result.get("marker")
        if not marker:
            return False
        if oldest is None or (prev_oldest is not None and oldest >= prev_oldest):
            logging.warning(
                f"Payment history scan for {account} aborted: page made no "
                f"progress toward the not_before floor (oldest {oldest}, "
                f"previous {prev_oldest}); an unconsumed payment may exist "
                f"beyond it"
            )
            return False
        prev_oldest = oldest


async def wait_for_payment(
    destination: str,
    expected_sender: str,
    expected_amount: str = "1",
    timeout_seconds: int | None = None,
    not_before: float | None = None,
    currency: str | None = None,
    issuer: str | None = None,
    allow_credit: bool = False,
    claimant: str | None = None,
) -> bool:
    """
    Subscribe to the destination account and wait for a token payment from
    expected_sender. Sender verification prevents one user's payment from
    triggering another user's mint. `not_before` (Unix time, default now-10s)
    bounds the backfill check for payments that landed before the
    subscription was active. currency/issuer default to the LFGO mint token;
    pass others (e.g. BRIX) for swap fees.

    Every matched payment is claimed by tx hash in the consumed-payment
    ledger, so one on-ledger payment can never satisfy two waits (#196).
    allow_credit additionally widens the backfill window to the ledger's
    bootstrap floor: an unconsumed payment the sender made while no session
    was listening (duplicate sign, post-timeout landing) is honoured instead
    of silently kept. Only safe for destinations that receive nothing but
    this payment type (the LFGO issuer) — an unrelated older payment to a
    busier account could otherwise be claimed.

    `claimant` (#228) tags the ledger claim with the calling flow's exact
    identity (e.g. "bulk:<job_id>") so that, after a crash between the claim
    committing and the caller persisting its paid state, the resumed flow can
    reconcile via payment_ledger.find_claimed instead of reading the dedup
    miss as "never paid".
    """
    timeout_seconds = timeout_seconds or config.PAYMENT_TIMEOUT_SECONDS
    currency = currency or config.TOKEN_CURRENCY_HEX
    issuer = issuer or config.TOKEN_ISSUER_ADDRESS
    start_time = time.time()
    deadline = start_time + timeout_seconds
    if not_before is None:
        not_before = start_time - 10
    backfill_not_before = not_before
    if allow_credit:
        # Credits are spendable back to the credit floor: never before the
        # ledger bootstrap (pre-tracking payments were matched but never
        # recorded) and never older than the TTL (which is what keeps the
        # scan depth bounded as issuer history grows).
        credit_floor = max(
            payment_ledger.bootstrap_floor(),
            start_time - config.MINT_CREDIT_TTL_SECONDS,
        )
        backfill_not_before = min(not_before, credit_floor)
    context = f"{expected_amount} {currency} from {expected_sender} to {destination}"

    def claim(tx: dict[str, Any], meta: Any, entry: dict[str, Any]) -> bool:
        if not _payment_matches(
            tx, meta, destination, expected_sender, expected_amount, currency, issuer
        ):
            return False
        tx_hash = _tx_hash(entry, tx)
        if tx_hash is None:
            # No hash means no way to mark it consumed; refuse rather than
            # let the same payment satisfy this and a later wait.
            logging.warning(f"Matching payment without a tx hash ignored ({context})")
            return False
        return payment_ledger.try_consume(tx_hash, expected_sender, destination, claimant=claimant)

    async def watch(websocket: Any) -> bool:
        async for message in websocket:
            tx, meta = _extract_tx_and_meta(message)
            if tx and claim(tx, meta, message):
                logging.info(f"✅ Payment received from {expected_sender}: {_tx_hash(message, tx)}")
                return True
        return False  # stream closed without a matching payment

    async def final_grace_check() -> bool:
        # A payment signed in time can validate seconds after the deadline
        # (issue #196: one landed 11s late and was silently kept). Wait out
        # the grace period, then re-check history once before giving up.
        if not allow_credit:
            return False
        await asyncio.sleep(config.PAYMENT_GRACE_SECONDS)
        try:
            async with AsyncWebsocketClient(config.WS_URL) as websocket:
                if await asyncio.wait_for(
                    _recent_payment_exists(websocket, destination, claim, backfill_not_before),
                    timeout=15,
                ):
                    logging.info(f"✅ Payment found in post-timeout grace check ({context})")
                    return True
        except Exception as e:
            logging.error(f"Post-timeout grace check failed ({context}): {e}")
        return False

    # A dropped websocket must not look like "payment never arrived": keep
    # reconnecting until the deadline, re-checking recent history each time
    # to catch a payment that validated while the connection was down.
    reconnects = 0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            if await final_grace_check():
                return True
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
                    _recent_payment_exists(websocket, destination, claim, backfill_not_before),
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
                if await final_grace_check():
                    return True
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
