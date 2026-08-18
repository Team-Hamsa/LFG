# lfg_core/xrpl_ops.py
# XRPL operations: mint, offer creation, payment watching (extracted from main.py).

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
import traceback
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO, Literal, cast, overload

from xrpl.asyncio.clients import AsyncJsonRpcClient, AsyncWebsocketClient
from xrpl.clients import JsonRpcClient
from xrpl.models import IssuedCurrencyAmount, TransactionMetadata
from xrpl.models.currencies import XRP, IssuedCurrency
from xrpl.models.requests import (
    AccountInfo,
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

from lfg_core import config, memos, owner_lock, payment_ledger

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


@dataclass(frozen=True)
class MintNFTResult:
    """Confirmed NFToken mint identifiers for callers that need durability."""

    nft_id: str
    tx_hash: str


@dataclass(frozen=True)
class MintPreparation:
    state: Literal["prepared", "failed"]
    tx_hash: str | None
    tx_blob: str | None
    error: str | None
    signed_ledger_floor: int | None = None


@dataclass(frozen=True)
class MintSubmission:
    state: Literal["validated", "failed", "indeterminate"]
    tx_hash: str | None
    nft_id: str | None
    error: str | None


@dataclass(frozen=True)
class MintReconciliation:
    complete: bool
    state: Literal["validated", "failed", "indeterminate"]
    tx_hash: str | None
    nft_id: str | None
    error: str | None


@dataclass(frozen=True)
class BurnPreparation:
    state: Literal["prepared", "noop", "failed"]
    tx_hash: str | None
    tx_blob: str | None
    error: str | None
    signed_ledger_floor: int | None = None


@dataclass(frozen=True)
class BurnSubmission:
    state: Literal["validated", "failed", "indeterminate"]
    tx_hash: str | None
    error: str | None


@dataclass(frozen=True)
class BurnReconciliation:
    complete: bool
    tx_hash: str | None
    error: str | None


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


# Bounded retry budget for a malformed confirm-poll body (#385): rippled
# sometimes answers HTTP 200 with a JSON body missing the `result` key, which
# xrpl-py surfaces as KeyError('result') from json_to_response. The Tx lookup
# is a pure read — re-issuing it is safe and idempotent, unlike the submit.
_MALFORMED_POLL_RETRIES = 4


def _is_malformed_result_error(e: BaseException) -> bool:
    """True when an exception is the rippled 200-with-no-`result`-key shape
    (#385) — KeyError('result') raised by xrpl-py's json_to_response, possibly
    wrapped by a chained exception."""
    seen: set[int] = set()
    current: BaseException | None = e
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, KeyError) and current.args == ("result",):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _tx_lookup_with_retry(
    client: JsonRpcClient, tx_hash: str, label: str, retries: int = _MALFORMED_POLL_RETRIES
) -> dict[str, Any]:
    """Pure-read `Tx` lookup, retrying (with backoff) ONLY the malformed
    200-body shape (#385) before letting the error propagate. Any other
    exception propagates immediately — the caller's fail-closed handling
    (`_confirm_by_hash`'s attempt loop / `get_tx`'s raise) is unchanged."""
    for attempt in range(retries + 1):
        try:
            response = await asyncio.to_thread(client.request, Tx(transaction=tx_hash))
            return response.result
        except Exception as e:
            if not _is_malformed_result_error(e) or attempt >= retries:
                raise
            delay = 0.5 * (2**attempt)
            logging.warning(
                f"{label}: malformed rippled response (no 'result' key) for "
                f"{tx_hash}; retrying read {attempt + 1}/{retries} in {delay}s"
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def _confirm_by_hash(
    client: JsonRpcClient, tx_hash: str, attempts: int = 3
) -> dict[str, Any] | None:
    """Look the transaction up by hash and return its result dict IFF the ledger
    reports it VALIDATED (any TransactionResult); else None (not found yet, not
    validated, or the lookup itself failed). Used only after a submit raised, to
    decide committed vs. indeterminate WITHOUT resubmitting a fresh transaction."""
    for attempt in range(attempts):
        try:
            result = await _tx_lookup_with_retry(client, tx_hash, "tx confirm lookup")
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


def _acquire_submission_file_lock(account: str) -> BinaryIO:
    """Take the cross-process half of the per-Account sequence lock."""
    lock_root = os.getenv(
        "XRPL_SUBMISSION_LOCK_DIR",
        os.path.join(tempfile.gettempdir(), "lfg-xrpl-submission-locks"),
    )
    os.makedirs(lock_root, mode=0o700, exist_ok=True)
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
    handle = open(os.path.join(lock_root, f"{digest}.lock"), "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _release_submission_file_lock(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


async def _acquire_submission_lock_cancellation_safe(account: str) -> BinaryIO:
    task = asyncio.create_task(asyncio.to_thread(_acquire_submission_file_lock, account))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A blocking flock cannot be cancelled in its worker thread. Wait for
        # it to acquire, release immediately, then preserve cancellation.
        handle = await task
        await asyncio.to_thread(_release_submission_file_lock, handle)
        raise


@asynccontextmanager
async def submission_coordinator(account: str) -> AsyncIterator[None]:
    """Serialize backend submissions for one Account across tasks AND processes.

    Both halves are required and both are held for the whole block:

    - `owner_lock` is an asyncio.Lock in a per-event-loop registry, so it only
      serializes tasks within one process. On its own it is NOT enough — the
      box runs several writers for SIGNING_ACCOUNT (the service, the sponsored
      burn worker, the CLI economy scripts, admin burns).
    - the flock'd file is the cross-process half.

    Earlier revisions took the file lock inside `_submission_scope` instead, so
    it was released the moment a helper returned. A caller that signs in one
    helper and forwards in another (the sponsored mint's prepare/submit split)
    left a window where another process could submit for the same Account,
    advance its sequence, and strand the already-signed blob at tefPAST_SEQ.
    Holding both here, for the caller's whole critical section, closes that —
    and gives the default `coordinator_held=False` path (mint_nft,
    create_nft_offer, buy_and_burn) cross-process protection it never had.
    """

    async with owner_lock.owner_lock(f"xrpl-submit:{account}"):
        handle = await _acquire_submission_lock_cancellation_safe(account)
        try:
            yield
        finally:
            release = asyncio.create_task(asyncio.to_thread(_release_submission_file_lock, handle))
            try:
                await asyncio.shield(release)
            except asyncio.CancelledError:
                await release
                raise


@asynccontextmanager
async def _submission_scope(account: str, coordinator_held: bool) -> AsyncIterator[None]:
    """Acquire the submission lock unless the caller already holds it.

    `coordinator_held=True` means an enclosing `submission_coordinator` owns
    both halves for a span wider than this helper — re-acquiring would
    self-deadlock on the non-reentrant asyncio.Lock, and releasing at this
    helper's exit would punch a hole in the caller's critical section."""
    if coordinator_held:
        yield
        return
    async with submission_coordinator(account):
        yield


async def _submit_and_confirm(
    tx: Transaction,
    wallet: Wallet,
    client: JsonRpcClient,
    label: str,
    *,
    coordinator_held: bool = False,
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
    (This also removes the duplicate-mint risk of the old blind retry loop, #179.)

    Serialized per signing account (fire-and-forget harvests, 2026-07-21):
    autofill reads the account sequence, so two concurrent backend-signed txs
    would sign the same sequence and one would fail tefPAST_SEQ. The lock
    spans sign→validate; backend txs pipeline instead of colliding. It is keyed
    on the transaction Account (not the seed-derived signer, which may be a
    regular key) via the loop-keyed owner_lock registry (#180)."""
    account = getattr(tx, "account", None) or wallet.classic_address
    async with _submission_scope(account, coordinator_held):
        signed = await asyncio.to_thread(autofill_and_sign, tx, client, wallet)
        try:
            # Pass wallet=None: `signed` is already signed, so submit_and_wait must
            # not re-sign/re-autofill it — otherwise the submitted tx could differ
            # from signed.get_hash() and the exception path would confirm the wrong
            # hash, marking an actually-submitted tx as indeterminate (#188).
            response = await asyncio.to_thread(
                submit_and_wait, signed, client, None, autofill=False
            )
        except Exception as e:
            logging.warning(f"{label}: submit_and_wait raised ({e}); confirming by hash")
            # A malformed poll body (#385) means submit_and_wait's Tx *read*
            # choked on upstream garbage, not that the submit failed — the tx
            # is usually fine and merely awaiting validation, so give the
            # confirm-by-hash read more patience before failing closed.
            attempts = 6 if _is_malformed_result_error(e) else 3
            confirmed = await _confirm_by_hash(client, signed.get_hash(), attempts=attempts)
            if confirmed is None:
                raise IndeterminateResultError(
                    f"{label}: on-ledger outcome unknown after submit raised ({e})"
                ) from e
            validated = _validated_result(confirmed, label)
        else:
            validated = _validated_result(response.result, label)
        if validated is not None:
            # Some xrpl-py response fakes and older rippled response shapes
            # omit `hash`; the exact signed hash is authoritative because this
            # helper signs once and never resubmits a fresh transaction.
            if not validated.get("hash"):
                validated["hash"] = signed.get_hash()
        return validated


@overload
async def mint_nft(
    metadata_cdn_url: str,
    taxon: int,
    issuer: str,
    flags: int | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
    action: str = memos.ACTION_MINT,
    *,
    return_details: Literal[False] = False,
) -> str | None: ...


@overload
async def mint_nft(
    metadata_cdn_url: str,
    taxon: int,
    issuer: str,
    flags: int | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
    action: str = memos.ACTION_MINT,
    *,
    return_details: Literal[True],
) -> MintNFTResult | None: ...


async def mint_nft(
    metadata_cdn_url: str,
    taxon: int,
    issuer: str,
    flags: int | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    campaign: str | None = None,
    action: str = memos.ACTION_MINT,
    *,
    return_details: bool = False,
) -> str | MintNFTResult | None:
    """Mint an NFT on XRPL; returns the NFToken ID or None by default.

    `return_details=True` opts into a `MintNFTResult` containing the same ID
    plus the validated transaction hash. `flags` overrides config.NFT_FLAGS
    (e.g. burnable economy characters / soulbound buckets). `platform`
    records the originating surface in the provenance memo (#54); `action`
    the app operation (economy assembles/extracts pass their own so the memo
    distinguishes them from plain mints and legacy remint swaps).
    """
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
            if return_details:
                return MintNFTResult(nft_id=str(nft_id), tx_hash=str(result["hash"]))
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


def _sponsored_mint_transaction(
    metadata_cdn_url: str,
    taxon: int,
    issuer: str,
    *,
    flags: int | None,
    platform: str,
    campaign: str,
) -> NFTokenMint:
    eff_flags = config.NFT_FLAGS if flags is None else flags
    kwargs: dict[str, Any] = {
        "account": config.SIGNING_ACCOUNT,
        "uri": convert_str_to_hex(metadata_cdn_url),
        "nftoken_taxon": taxon,
        "flags": eff_flags,
        "source_tag": config.SOURCE_TAG,
        "memos": memos.build_memo_models(
            memos.INITIATOR_BACKEND,
            platform,
            memos.ACTION_MINT,
            campaign,
        ),
    }
    if eff_flags & TF_TRANSFERABLE:
        kwargs["transfer_fee"] = config.NFT_TRANSFER_FEE
    if issuer != config.SIGNING_ACCOUNT:
        kwargs["issuer"] = issuer
    return NFTokenMint(**kwargs)


async def prepare_sponsored_mint(
    metadata_cdn_url: str,
    taxon: int,
    issuer: str,
    *,
    campaign: str,
    flags: int | None = None,
    platform: str = memos.PLATFORM_BACKEND,
    coordinator_held: bool = False,
) -> MintPreparation:
    """Sign one correlated mint and return its immutable identity without forwarding."""

    if not campaign.strip():
        return MintPreparation("failed", None, None, "claim correlation is required")
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        async with _submission_scope(config.SIGNING_ACCOUNT, coordinator_held):
            signed_ledger_floor = await _current_validated_ledger_index(client)
            if signed_ledger_floor is None:
                return MintPreparation(
                    "failed",
                    None,
                    None,
                    "mint preparation could not observe a validated ledger floor",
                )
            tx = _sponsored_mint_transaction(
                metadata_cdn_url,
                taxon,
                issuer,
                flags=flags,
                platform=platform,
                campaign=campaign,
            )
            signed = await asyncio.to_thread(autofill_and_sign, tx, client, wallet)
        return MintPreparation(
            "prepared",
            signed.get_hash(),
            signed.blob(),
            None,
            signed_ledger_floor,
        )
    except Exception as exc:
        return MintPreparation("failed", None, None, f"mint preparation failed: {exc}")


def _classify_sponsored_mint(result: object, tx_hash: str) -> MintSubmission:
    if not isinstance(result, dict) or result.get("validated") is not True:
        return MintSubmission(
            "indeterminate", tx_hash, None, "response was not explicitly validated"
        )
    result_hash = result.get("hash")
    if result_hash is not None and result_hash != tx_hash:
        return MintSubmission("indeterminate", tx_hash, None, "validated response hash mismatch")
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return MintSubmission("indeterminate", tx_hash, None, "validated response omitted metadata")
    engine_result = meta.get("TransactionResult")
    if not isinstance(engine_result, str) or not engine_result:
        return MintSubmission(
            "indeterminate", tx_hash, None, "validated response omitted TransactionResult"
        )
    if engine_result != "tesSUCCESS":
        return MintSubmission("failed", tx_hash, None, engine_result)
    nft_id = meta.get("nftoken_id")
    if not nft_id:
        try:
            nft_id = get_nftoken_id(cast(TransactionMetadata, meta))
        except Exception:
            nft_id = None
    if not nft_id:
        return MintSubmission(
            "indeterminate",
            tx_hash,
            None,
            "validated mint succeeded but NFTokenID could not be derived",
        )
    return MintSubmission("validated", tx_hash, str(nft_id), None)


async def submit_sponsored_mint(
    *,
    signed_tx_blob: str,
    signed_tx_hash: str,
    coordinator_held: bool = False,
    prove_expiry: bool = False,
) -> MintSubmission:
    """Forward exactly the persisted signed mint; never sign a replacement."""

    try:
        signed = Transaction.from_blob(signed_tx_blob)
        decoded_hash = signed.get_hash()
    except Exception as exc:
        return MintSubmission(
            "indeterminate",
            signed_tx_hash,
            None,
            f"persisted mint decode failed: {exc}",
        )
    if decoded_hash != signed_tx_hash:
        return MintSubmission(
            "indeterminate", signed_tx_hash, None, "signed mint hash/blob mismatch"
        )
    client = JsonRpcClient(config.JSON_RPC_URL)
    last_ledger_sequence = getattr(signed, "last_ledger_sequence", None)
    if (
        prove_expiry
        and isinstance(last_ledger_sequence, int)
        and not isinstance(last_ledger_sequence, bool)
    ):
        current_ledger = await _current_validated_ledger_index(client)
        if current_ledger is not None and current_ledger > last_ledger_sequence:
            # The exact identity can no longer enter a validated ledger. One
            # final exact-hash lookup distinguishes a transaction that landed
            # before expiry from the crash-before-forward case. Never sign a
            # replacement here: an expired absence restores the durable free
            # promise, while a validated match is recorded normally.
            confirmed = await _confirm_by_hash(client, signed_tx_hash)
            if confirmed is not None:
                return _classify_sponsored_mint(confirmed, signed_tx_hash)
            return MintSubmission(
                "failed",
                signed_tx_hash,
                None,
                "prepared mint expired without validation",
            )
    try:
        transaction_account = getattr(signed, "account", None) or config.SIGNING_ACCOUNT
        async with _submission_scope(transaction_account, coordinator_held):
            try:
                response = await asyncio.to_thread(
                    submit_and_wait, signed, client, None, autofill=False
                )
                result = response.result
            except Exception as exc:
                confirmed = await _confirm_by_hash(client, signed_tx_hash)
                if confirmed is None:
                    return MintSubmission(
                        "indeterminate",
                        signed_tx_hash,
                        None,
                        f"mint submit outcome unknown after exception: {exc}",
                    )
                result = confirmed
        return _classify_sponsored_mint(result, signed_tx_hash)
    except Exception as exc:
        return MintSubmission(
            "indeterminate",
            signed_tx_hash,
            None,
            f"mint outcome unknown after forwarding began: {exc}",
        )


async def reconcile_sponsored_mint(tx_hash: str) -> MintReconciliation:
    """Classify only the exact journaled hash; this path never submits."""

    client = JsonRpcClient(config.JSON_RPC_URL)
    try:
        response = await asyncio.to_thread(client.request, Tx(transaction=tx_hash))
        result = response.result
    except Exception as exc:
        return MintReconciliation(False, "indeterminate", tx_hash, None, str(exc))
    if not isinstance(result, dict) or result.get("validated") is not True:
        return MintReconciliation(
            False, "indeterminate", tx_hash, None, "exact mint hash is not validated"
        )
    classified = _classify_sponsored_mint(result, tx_hash)
    complete = classified.state in ("validated", "failed")
    return MintReconciliation(
        complete,
        classified.state,
        classified.tx_hash,
        classified.nft_id,
        classified.error,
    )


async def account_exists(address: str) -> bool | None:
    """Does this account exist on-ledger (i.e. is it funded above the base
    reserve)?

    True  -- present in the validated ledger.
    False -- DEFINITIVELY absent: rippled answered `actNotFound`.
    None  -- unknown: the lookup itself failed, which says nothing either way.

    The three-way return is the point. An unfunded destination makes
    NFTokenCreateOffer fail `tecNO_DST` no matter how often it is retried, so
    callers may treat False as terminal — but only False. None must stay
    fail-closed, or a transient RPC blip reads as "this account is gone" and
    silently drops real work.
    """
    try:
        client = AsyncJsonRpcClient(config.JSON_RPC_URL)
        response = await client.request(AccountInfo(account=address, ledger_index="validated"))
    except Exception as e:
        logging.warning(f"account_exists({address}) lookup failed: {e}")
        return None
    if response.is_successful():
        return True
    error = response.result.get("error")
    if error == "actNotFound":
        return False
    logging.warning(f"account_exists({address}) inconclusive: {error}")
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

        result = await _submit_and_confirm(offer, wallet, client, "NFTokenCreateOffer")
        if result is None:
            return None
        meta = result.get("meta")
        offer_id = meta.get("offer_id") if isinstance(meta, dict) else None
        if not isinstance(offer_id, str) or not offer_id:
            raise IndeterminateResultError(
                "NFTokenCreateOffer validated but its offer ID was absent"
            )
        logging.info(f"Offer created: {offer_id}")
        return offer_id

    # NOTE (#211): every indeterminate outcome MUST collapse to None here,
    # including IndeterminateResultError out of _submit_and_confirm. Callers
    # are written against the "falsy return means the offer may still have
    # landed — go look on-ledger" contract: swap_flow._create_offer_and_accept
    # adopts a landed issuer→swapper offer instead of stranding a reminted
    # token (tests/test_swap_offer_recovery.py::test_offer_failure_adopts_
    # landed_offer pins exactly this). Re-raising here skips that recovery.
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


def _valid_xrpl_amount_shape(amount: Any) -> bool:
    if isinstance(amount, str):
        return bool(amount.strip())
    if not isinstance(amount, dict):
        return False
    return all(
        isinstance(amount.get(field), str) and bool(amount[field].strip())
        for field in ("currency", "issuer", "value")
    )


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
            if raise_on_error:
                raise RuntimeError("malformed nft_sell_offers response: offers must be a list")
            return []
        normalized: list[dict[str, Any]] = []
        for offer in offers:
            if not isinstance(offer, dict):
                if raise_on_error:
                    raise RuntimeError("malformed nft_sell_offers response: invalid offer entry")
                continue
            offer_index = offer.get("nft_offer_index", offer.get("index"))
            amount = offer.get("amount")
            destination = offer.get("destination")
            if raise_on_error and (
                not isinstance(offer_index, str)
                or not offer_index
                or not _valid_xrpl_amount_shape(amount)
                or (destination is not None and not isinstance(destination, str))
                or not isinstance(offer.get("flags"), int)
                or isinstance(offer.get("flags"), bool)
                or not isinstance(offer.get("owner"), str)
                or not offer["owner"]
            ):
                raise RuntimeError("malformed nft_sell_offers response: incomplete offer entry")
            normalized.append(
                {
                    "offer_index": offer_index,
                    "amount": amount,
                    "destination": destination,
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
        # Free gifts only ("0" = zero XRP drops). The signing account also
        # holds PRICED destination-locked offers (Trait Shop #217 sells, XRP
        # or BRIX-dict amounts) — surfacing those as claimable would let a
        # user unknowingly sign a charging transaction (Greptile P1).
        if o.get("amount") != "0":
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
    broke" apart from "still pending".

    Like the confirm-by-hash path, this is a pure read — a malformed 200 body
    with no `result` key (#385) is retried a bounded number of times before
    the error propagates."""
    client = JsonRpcClient(config.JSON_RPC_URL)
    return await _tx_lookup_with_retry(client, tx_hash, "get_tx")


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


async def prepare_sponsored_burn(
    memo_id: str,
    *,
    amount: str | None = None,
    source_account: str | None = None,
    network: str | None = None,
    issuer: str | None = None,
    currency: str | None = None,
    source_tag: int | None = None,
    coordinator_held: bool = False,
) -> BurnPreparation:
    burn_amount = config.MINT_PRICE_LFGO if amount is None else amount
    source = config.SIGNING_ACCOUNT if source_account is None else source_account
    selected_network = config.XRPL_NETWORK if network is None else network
    burn_issuer = config.TOKEN_ISSUER_ADDRESS if issuer is None else issuer
    burn_currency = config.TOKEN_CURRENCY_HEX if currency is None else currency
    burn_source_tag = config.SOURCE_TAG if source_tag is None else source_tag
    # Network mismatch is checked FIRST, before the self-issuer shortcut.
    # Reversed, a testnet-scoped obligation processed while XRPL_NETWORK is
    # mainnet takes the `source == burn_issuer` branch, returns "noop", and
    # sponsored_burn.process_one maps noop to status="burned",
    # fulfillment="self_issuer_noop" — discharging a real LFGO debt with no
    # ledger effect. submit_sponsored_burn already validates network first;
    # preparation must match it.
    if selected_network != config.XRPL_NETWORK:
        return BurnPreparation(
            "failed", None, None, "burn obligation network does not match the active XRPL network"
        )
    if source == burn_issuer:
        if selected_network == "testnet":
            return BurnPreparation(
                "noop", None, None, "testnet self-issuer burn requires no transaction"
            )
        return BurnPreparation(
            "failed", None, None, "mainnet self-issuer burn topology is forbidden"
        )
    try:
        wallet = Wallet.from_seed(config.SEED)
        client = JsonRpcClient(config.JSON_RPC_URL)
        async with _submission_scope(source, coordinator_held):
            signed_ledger_floor = await _current_validated_ledger_index(client)
            if signed_ledger_floor is None:
                return BurnPreparation(
                    "failed",
                    None,
                    None,
                    "burn preparation could not observe a validated ledger floor",
                )
            payment = Payment(
                account=source,
                destination=burn_issuer,
                amount=IssuedCurrencyAmount(
                    currency=burn_currency,
                    issuer=burn_issuer,
                    value=burn_amount,
                ),
                source_tag=burn_source_tag,
                memos=memos.build_memo_models(
                    memos.INITIATOR_BACKEND,
                    memos.PLATFORM_BACKEND,
                    memos.ACTION_SPONSORED_MINT_BURN,
                    memo_id,
                ),
            )
            signed = await asyncio.to_thread(autofill_and_sign, payment, client, wallet)
        return BurnPreparation(
            "prepared",
            signed.get_hash(),
            signed.blob(),
            None,
            signed_ledger_floor,
        )
    except Exception as exc:
        return BurnPreparation("failed", None, None, f"burn preparation failed: {exc}")


def _classify_sponsored_burn(result: object, tx_hash: str) -> BurnSubmission:
    if not isinstance(result, dict) or result.get("validated") is not True:
        return BurnSubmission("indeterminate", tx_hash, "response was not explicitly validated")
    result_hash = result.get("hash")
    if result_hash is not None and result_hash != tx_hash:
        return BurnSubmission("indeterminate", tx_hash, "validated response hash mismatch")
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return BurnSubmission("indeterminate", tx_hash, "validated response omitted metadata")
    engine_result = meta.get("TransactionResult")
    if not isinstance(engine_result, str) or not engine_result:
        return BurnSubmission(
            "indeterminate", tx_hash, "validated response omitted TransactionResult"
        )
    if engine_result == "tesSUCCESS":
        return BurnSubmission("validated", tx_hash, None)
    return BurnSubmission("failed", tx_hash, engine_result)


async def submit_sponsored_burn(
    memo_id: str,
    *,
    amount: str | None = None,
    source_account: str | None = None,
    signed_tx_blob: str | None = None,
    signed_tx_hash: str | None = None,
    network: str | None = None,
    issuer: str | None = None,
    currency: str | None = None,
    source_tag: int | None = None,
    coordinator_held: bool = False,
) -> BurnSubmission:
    source = config.SIGNING_ACCOUNT if source_account is None else source_account
    selected_network = config.XRPL_NETWORK if network is None else network
    burn_issuer = config.TOKEN_ISSUER_ADDRESS if issuer is None else issuer
    if selected_network != config.XRPL_NETWORK:
        return BurnSubmission(
            "failed", signed_tx_hash, "burn obligation network does not match active XRPL network"
        )
    if source == burn_issuer:
        if selected_network == "testnet":
            return BurnSubmission("validated", None, None)
        return BurnSubmission("failed", signed_tx_hash, "mainnet self-issuer burn is forbidden")
    if signed_tx_blob is None or signed_tx_hash is None:
        prepared = await prepare_sponsored_burn(
            memo_id,
            amount=amount,
            source_account=source,
            network=selected_network,
            issuer=burn_issuer,
            currency=currency,
            source_tag=source_tag,
            coordinator_held=coordinator_held,
        )
        if prepared.state == "noop":
            return BurnSubmission("validated", None, None)
        if prepared.state != "prepared" or not prepared.tx_blob or not prepared.tx_hash:
            return BurnSubmission("failed", None, prepared.error or "burn preparation failed")
        signed_tx_blob, signed_tx_hash = prepared.tx_blob, prepared.tx_hash
    try:
        signed = Transaction.from_blob(signed_tx_blob)
        decoded_hash = signed.get_hash()
    except Exception as exc:
        return BurnSubmission("failed", signed_tx_hash, f"persisted burn decode failed: {exc}")
    if decoded_hash != signed_tx_hash:
        return BurnSubmission("failed", signed_tx_hash, "signed burn hash/blob mismatch")
    client = JsonRpcClient(config.JSON_RPC_URL)
    try:
        async with _submission_scope(source, coordinator_held):
            try:
                response = await asyncio.to_thread(
                    submit_and_wait, signed, client, None, autofill=False
                )
                result = response.result
            except Exception as exc:
                confirmed = await _confirm_by_hash(client, signed_tx_hash)
                if confirmed is None:
                    return BurnSubmission(
                        "indeterminate",
                        signed_tx_hash,
                        f"submit outcome unknown after exception: {exc}",
                    )
                result = confirmed
        return _classify_sponsored_burn(result, signed_tx_hash)
    except Exception as exc:
        return BurnSubmission(
            "indeterminate",
            signed_tx_hash,
            f"burn outcome unknown after forwarding began: {exc}",
        )


def _ledger_index(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        index = int(value)
    except ValueError:
        return None
    return index if index > 0 else None


async def _current_validated_ledger_index(client: JsonRpcClient) -> int | None:
    response = await asyncio.to_thread(
        client.request,
        Ledger(ledger_index="validated"),
    )
    result = response.result
    if not isinstance(result, dict) or result.get("validated") is False:
        return None
    raw_index = result.get("ledger_index")
    if raw_index is None:
        ledger = result.get("ledger")
        raw_index = ledger.get("ledger_index") if isinstance(ledger, dict) else None
    return _ledger_index(raw_index)


async def sponsored_burn_identity_expired(signed_tx_blob: str) -> int | None:
    """Return the expired identity's LLS only after a later validated ledger."""

    try:
        signed = Transaction.from_blob(signed_tx_blob)
        last_ledger_sequence = signed.last_ledger_sequence
        if (
            isinstance(last_ledger_sequence, bool)
            or not isinstance(last_ledger_sequence, int)
            or last_ledger_sequence <= 0
        ):
            return None
        client = JsonRpcClient(config.JSON_RPC_URL)
        validated_index = await _current_validated_ledger_index(client)
        if validated_index is not None and validated_index > last_ledger_sequence:
            return last_ledger_sequence
        return None
    except Exception as exc:
        logging.warning("sponsored burn expiry check failed: %s", exc)
        return None


def _target_burn_amount(
    value: object,
    expected: Decimal,
    expected_currency: str,
    expected_issuer: str,
) -> Literal["match", "non_match", "incomplete"]:
    if isinstance(value, str):
        return "non_match"
    if not isinstance(value, dict):
        return "incomplete"
    currency, issuer = value.get("currency"), value.get("issuer")
    if not isinstance(currency, str) or not isinstance(issuer, str):
        return "incomplete"
    if currency != expected_currency or issuer != expected_issuer:
        return "non_match"
    amount = value.get("value")
    if not isinstance(amount, str):
        return "incomplete"
    try:
        return "match" if Decimal(amount) == expected else "non_match"
    except InvalidOperation:
        return "incomplete"


def _sponsored_burn_entry(
    entry: object,
    *,
    memo_id: str,
    amount: str,
    source_account: str,
    issuer: str,
    currency: str,
    source_tag: int,
    signed_tx_hash: str | None,
) -> tuple[Literal["match", "non_match", "incomplete"], str | None, str | None]:
    if not isinstance(entry, dict):
        return "incomplete", None, "account_tx contained a non-object entry"
    if entry.get("validated") is not True:
        return "incomplete", None, "account_tx contained a non-validated entry"
    tx, meta = _extract_tx_and_meta(entry)
    if tx is None:
        return "incomplete", None, "validated entry omitted transaction"
    if not isinstance(meta, dict):
        return "incomplete", None, "validated entry omitted metadata"
    engine_result = meta.get("TransactionResult")
    if not isinstance(engine_result, str) or not engine_result:
        return "incomplete", None, "metadata omitted TransactionResult"
    tx_type = tx.get("TransactionType")
    if not isinstance(tx_type, str) or not tx_type:
        return "incomplete", None, "transaction omitted TransactionType"
    if engine_result != "tesSUCCESS" or tx_type != "Payment":
        return "non_match", None, None
    account, destination = tx.get("Account"), tx.get("Destination")
    if not isinstance(account, str) or not isinstance(destination, str):
        return "incomplete", None, "Payment omitted account or destination"
    if account != source_account or destination != issuer:
        return "non_match", None, None
    observed_source_tag = tx.get("SourceTag")
    if observed_source_tag is not None and (
        isinstance(observed_source_tag, bool) or not isinstance(observed_source_tag, int)
    ):
        return "incomplete", None, "Payment had malformed SourceTag"
    if observed_source_tag != source_tag:
        return "non_match", None, None
    try:
        expected = Decimal(amount)
    except InvalidOperation:
        return "incomplete", None, "burn obligation had malformed amount"
    requested = _target_burn_amount(
        tx.get("Amount", tx.get("DeliverMax")), expected, currency, issuer
    )
    if requested == "non_match":
        return "non_match", None, None
    if requested == "incomplete":
        return "incomplete", None, "Payment had malformed requested amount"
    delivered = _target_burn_amount(
        meta.get("delivered_amount", meta.get("DeliveredAmount")),
        expected,
        currency,
        issuer,
    )
    if delivered == "non_match":
        return "non_match", None, None
    if delivered == "incomplete":
        return "incomplete", None, "Payment omitted exact delivered amount"
    decoded = memos.decode_memos(tx.get("Memos", []))
    if decoded is None:
        return "incomplete", None, "Payment had malformed or duplicate memos"
    if decoded != {
        "initiator": memos.INITIATOR_BACKEND,
        "platform": memos.PLATFORM_BACKEND,
        "action": memos.ACTION_SPONSORED_MINT_BURN,
        "campaign": memo_id,
    }:
        return "non_match", None, None
    tx_hash = _tx_hash(entry, tx)
    if tx_hash is None:
        return "incomplete", None, "matching burn omitted transaction hash"
    if signed_tx_hash is not None and tx_hash != signed_tx_hash:
        return "non_match", None, None
    return "match", tx_hash, None


def _burn_marker_key(marker: object) -> str | None:
    if isinstance(marker, bool):
        return None
    if isinstance(marker, (str, int)):
        return repr(marker)
    if not isinstance(marker, dict) or not marker:
        return None
    if not all(
        isinstance(key, str) and isinstance(value, (str, int)) and not isinstance(value, bool)
        for key, value in marker.items()
    ):
        return None
    return json.dumps(marker, sort_keys=True, separators=(",", ":"))


async def find_sponsored_burn(
    memo_id: str,
    *,
    amount: str | None = None,
    source_account: str | None = None,
    network: str | None = None,
    issuer: str | None = None,
    currency: str | None = None,
    source_tag: int | None = None,
    signed_tx_hash: str | None = None,
    required_ledger_min: int | None = None,
    required_ledger_max: int | None = None,
) -> BurnReconciliation:
    """Scan full validated history; malformed data never proves absence."""
    burn_amount = config.MINT_PRICE_LFGO if amount is None else amount
    source = config.SIGNING_ACCOUNT if source_account is None else source_account
    selected_network = config.XRPL_NETWORK if network is None else network
    burn_issuer = config.TOKEN_ISSUER_ADDRESS if issuer is None else issuer
    burn_currency = config.TOKEN_CURRENCY_HEX if currency is None else currency
    burn_source_tag = config.SOURCE_TAG if source_tag is None else source_tag
    if selected_network != config.XRPL_NETWORK:
        return BurnReconciliation(
            False, None, "burn obligation network does not match the active XRPL network"
        )
    bounded = required_ledger_min is not None or required_ledger_max is not None
    if bounded and (
        isinstance(required_ledger_min, bool)
        or not isinstance(required_ledger_min, int)
        or required_ledger_min <= 0
        or isinstance(required_ledger_max, bool)
        or not isinstance(required_ledger_max, int)
        or required_ledger_max < required_ledger_min
    ):
        return BurnReconciliation(False, None, "required account_tx ledger range was malformed")
    request_ledger_min = required_ledger_min if bounded else -1
    request_ledger_max = required_ledger_max if bounded else -1
    assert isinstance(request_ledger_min, int)
    assert isinstance(request_ledger_max, int)
    client = JsonRpcClient(config.JSON_RPC_URL)
    marker = None
    seen_markers: set[str] = set()
    incomplete_error: str | None = None
    try:
        while True:
            response = await asyncio.to_thread(
                client.request,
                AccountTx(
                    account=source,
                    ledger_index_min=request_ledger_min,
                    ledger_index_max=request_ledger_max,
                    limit=200,
                    marker=marker,
                ),
            )
            result = response.result
            if not isinstance(result, dict):
                return BurnReconciliation(False, None, "account_tx response was malformed")
            if bounded:
                if result.get("validated") is not True:
                    return BurnReconciliation(
                        False,
                        None,
                        "bounded account_tx response was not explicitly validated",
                    )
                response_account = result.get("account")
                if not isinstance(response_account, str) or response_account != source:
                    return BurnReconciliation(
                        False,
                        None,
                        "bounded account_tx response account did not match the requested account",
                    )
                returned_min = result.get("ledger_index_min")
                returned_max = result.get("ledger_index_max")
                if (
                    isinstance(returned_min, bool)
                    or not isinstance(returned_min, int)
                    or returned_min <= 0
                    or isinstance(returned_max, bool)
                    or not isinstance(returned_max, int)
                    or returned_max <= 0
                ):
                    return BurnReconciliation(
                        False,
                        None,
                        "bounded account_tx response omitted a well-formed ledger range",
                    )
                if returned_min > request_ledger_min:
                    return BurnReconciliation(
                        False,
                        None,
                        "account_tx history was pruned before the required ledger floor",
                    )
                if returned_max < request_ledger_max:
                    return BurnReconciliation(
                        False,
                        None,
                        "account_tx validated range lagged the required last ledger sequence",
                    )
            transactions = result.get("transactions")
            if not isinstance(transactions, list):
                return BurnReconciliation(False, None, "account_tx omitted transactions")
            for entry in transactions:
                state, tx_hash, error = _sponsored_burn_entry(
                    entry,
                    memo_id=memo_id,
                    amount=burn_amount,
                    source_account=source,
                    signed_tx_hash=signed_tx_hash,
                    issuer=burn_issuer,
                    currency=burn_currency,
                    source_tag=burn_source_tag,
                )
                if state == "incomplete":
                    if incomplete_error is None:
                        incomplete_error = error or "account_tx contained an incomplete entry"
                    continue
                if state == "match":
                    return BurnReconciliation(True, tx_hash, None)
            marker = result.get("marker")
            if marker is None:
                if incomplete_error is not None:
                    return BurnReconciliation(False, None, incomplete_error)
                return BurnReconciliation(True, None, None)
            marker_key = _burn_marker_key(marker)
            if marker_key is None:
                return BurnReconciliation(False, None, "account_tx marker was malformed")
            if marker_key in seen_markers:
                return BurnReconciliation(False, None, "account_tx marker repeated")
            seen_markers.add(marker_key)
    except Exception as exc:
        return BurnReconciliation(False, None, f"account_tx scan failed: {exc}")


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
        burn = Payment(**kwargs)
        result = await _submit_and_confirm(burn, wallet, client, "buy_and_burn")
        if result is None:
            return None
        tx_hash = result.get("hash")
        if not isinstance(tx_hash, str) or not tx_hash:
            # Raising here would only be caught by this function's own
            # `except Exception` two lines down and logged as a generic error
            # with a full traceback — noise for a case that is not a failure.
            # The burn DID validate; we just can't name its transaction.
            # Callers only check truthiness and continue (it is best-effort),
            # so say precisely what happened and return None.
            logging.warning(
                "buy_and_burn: %s %s validated but the response carried no transaction hash; "
                "the burn landed and cannot be cited",
                value,
                currency,
            )
            return None
        logging.info(f"Burned {value} {currency}: {tx_hash}")
        return tx_hash
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
