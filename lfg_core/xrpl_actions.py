"""Protocol primitives for payment-first XRPL mint actions.

This module deliberately owns no service or UI state.  It is the fail-closed
boundary for amendment capabilities, issuer Ticket discovery, and the ledger
key derivation needed to reference an NFToken offer created earlier in the
same Batch.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from xrpl.asyncio.transaction import autofill
from xrpl.clients import JsonRpcClient
from xrpl.core import keypairs
from xrpl.core.addresscodec import decode_classic_address
from xrpl.core.binarycodec import encode_for_signing_batch
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.requests import AccountObjects, AccountObjectType, Feature
from xrpl.models.transactions import (
    Batch,
    BatchFlag,
    NFTokenAcceptOffer,
    NFTokenMint,
    Payment,
    TransactionFlag,
)
from xrpl.models.transactions.batch import BatchSigner
from xrpl.utils import get_nftoken_id
from xrpl.wallet import Wallet

from lfg_core import memos

BATCH_V1_1_ID = "9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377"
NFTOKEN_MINT_OFFER_ID = (
    "EE3CF852F0506782D05E65D49E5DCC3D16D50898CD1B646BAE274863401CC3CE"
)
OBSOLETE_BATCH_ID = (
    "894646DD5284E97DECFE6674A6D6152686791C4A95F8C132CCA9BAF9E5812FB6"
)
NFTOKEN_OFFER_NAMESPACE = 0x0071


@dataclass(frozen=True)
class BatchCapability:
    enabled: bool
    reason: str | None


@dataclass(frozen=True)
class MintPayment:
    pay_with: Literal["LFGO", "XRP"]
    display_amount: str
    destination: str
    amount: str | IssuedCurrencyAmount


@dataclass(frozen=True)
class PreparedBatch:
    transaction: Batch
    offer_id: str
    inner_hashes: tuple[str, str, str]
    last_ledger_sequence: int


@dataclass(frozen=True)
class VerifiedAtomicMint:
    nft_id: str
    ledger_index: int


class AtomicMintInvariantError(ValueError):
    """Raised when a prepared Batch differs from the approved mint contract."""


def _flags_value(flags: Any) -> int:
    if flags is None:
        return 0
    if isinstance(flags, int) and not isinstance(flags, bool):
        return flags
    raise AtomicMintInvariantError("transaction flags must be a canonical integer")


def nft_offer_id(account: str, sequence_or_ticket: int) -> str:
    """Return the NFToken offer ledger index for an account and sequence.

    NFTokenMintOffer lets the mint transaction create this offer.  Leasing a
    Ticket makes its identifying sequence stable while the buyer reviews the
    interactive Batch, so the later accept leg can reference this key up front.
    """

    if (
        isinstance(sequence_or_ticket, bool)
        or not isinstance(sequence_or_ticket, int)
        or not 0 <= sequence_or_ticket <= 0xFFFFFFFF
    ):
        raise ValueError("sequence_or_ticket must be a uint32")
    account_id = decode_classic_address(account)
    payload = (
        NFTOKEN_OFFER_NAMESPACE.to_bytes(2, "big")
        + account_id
        + sequence_or_ticket.to_bytes(4, "big")
    )
    return hashlib.sha512(payload).digest()[:32].hex().upper()


def evaluate_capabilities(
    rows: Mapping[str, Mapping[str, Any]], *, configured: bool
) -> BatchCapability:
    """Evaluate the exact amendment matrix; configuration can only close it."""

    if not configured:
        return BatchCapability(False, "action_disabled")
    obsolete = rows.get(OBSOLETE_BATCH_ID, {})
    if obsolete.get("enabled"):
        return BatchCapability(False, "obsolete_batch_enabled")
    batch = rows.get(BATCH_V1_1_ID, {})
    mint_offer = rows.get(NFTOKEN_MINT_OFFER_ID, {})
    if not batch.get("supported") or not batch.get("enabled"):
        return BatchCapability(False, "batch_unavailable")
    if not mint_offer.get("supported") or not mint_offer.get("enabled"):
        return BatchCapability(False, "mint_offer_unavailable")
    return BatchCapability(True, None)


async def fetch_batch_capability(
    client: JsonRpcClient, *, configured: bool
) -> BatchCapability:
    """Read `feature` rows in rippled's map-by-amendment response shape."""

    if not configured:
        return BatchCapability(False, "action_disabled")
    rows: dict[str, Mapping[str, Any]] = {}
    for amendment_id in (
        BATCH_V1_1_ID,
        NFTOKEN_MINT_OFFER_ID,
        OBSOLETE_BATCH_ID,
    ):
        unavailable = (
            "mint_offer_unavailable"
            if amendment_id == NFTOKEN_MINT_OFFER_ID
            else "batch_unavailable"
        )
        try:
            response = await asyncio.to_thread(
                client.request, Feature(feature=amendment_id)
            )
        except Exception:
            return BatchCapability(False, unavailable)
        result = response.result if isinstance(response.result, dict) else {}
        row = result.get(amendment_id)
        if not isinstance(row, dict):
            return BatchCapability(False, unavailable)
        rows[amendment_id] = row
    return evaluate_capabilities(rows, configured=True)


async def list_ticket_sequences(client: JsonRpcClient, account: str) -> list[int]:
    """Return validated TicketSequence values currently owned by `account`."""

    response = await asyncio.to_thread(
        client.request,
        AccountObjects(
            account=account,
            type=AccountObjectType.TICKET,
            ledger_index="validated",
        ),
    )
    result = response.result if isinstance(response.result, dict) else {}
    objects = result.get("account_objects", [])
    if not isinstance(objects, list):
        return []
    return sorted(
        obj["TicketSequence"]
        for obj in objects
        if isinstance(obj, dict)
        and obj.get("LedgerEntryType") == "Ticket"
        and isinstance(obj.get("TicketSequence"), int)
        and not isinstance(obj.get("TicketSequence"), bool)
    )


def build_atomic_mint_batch(
    *,
    buyer: str,
    issuer_account: str,
    nft_issuer: str,
    issuer_ticket: int,
    metadata_url: str,
    payment: MintPayment,
    platform: str,
    campaign: str | None,
    nft_flags: int,
    nft_taxon: int,
    transfer_fee: int,
    source_tag: int,
) -> Batch:
    """Build the only allowed payment-first three-leg mint Batch."""

    inner_flag = int(TransactionFlag.TF_INNER_BATCH_TXN)
    payment_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_PAYMENT, campaign
    )
    mint_memos = memos.build_memo_models(
        memos.INITIATOR_BACKEND, platform, memos.ACTION_MINT, campaign
    )
    accept_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_ACCEPT_OFFER, campaign
    )
    offer_id = nft_offer_id(issuer_account, issuer_ticket)
    mint_kwargs: dict[str, Any] = {
        "account": issuer_account,
        "sequence": 0,
        "ticket_sequence": issuer_ticket,
        "uri": metadata_url.encode().hex().upper(),
        "nftoken_taxon": nft_taxon,
        "flags": nft_flags | inner_flag,
        "amount": "0",
        "destination": buyer,
        "source_tag": source_tag,
        "memos": mint_memos,
    }
    if nft_flags & 0x0008:
        mint_kwargs["transfer_fee"] = transfer_fee
    if nft_issuer != issuer_account:
        mint_kwargs["issuer"] = nft_issuer
    return Batch(
        account=buyer,
        flags=BatchFlag.TF_ALL_OR_NOTHING,
        source_tag=source_tag,
        memos=memos.build_memo_models(
            memos.INITIATOR_USER, platform, memos.ACTION_MINT, campaign
        ),
        raw_transactions=[
            Payment(
                account=buyer,
                destination=payment.destination,
                amount=payment.amount,
                flags=inner_flag,
                source_tag=source_tag,
                memos=payment_memos,
            ),
            NFTokenMint(**mint_kwargs),
            NFTokenAcceptOffer(
                account=buyer,
                nftoken_sell_offer=offer_id,
                flags=inner_flag,
                source_tag=source_tag,
                memos=accept_memos,
            ),
        ],
    )


def validate_atomic_mint_batch(
    batch: Batch,
    *,
    buyer: str,
    issuer_account: str,
    nft_issuer: str,
    issuer_ticket: int,
    payment: MintPayment,
    metadata_url: str,
    platform: str,
    campaign: str | None,
    nft_flags: int,
    nft_taxon: int,
    transfer_fee: int,
    source_tag: int,
) -> None:
    """Reject any autofilled Batch that differs from the frozen action."""

    expected_outer_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_MINT, campaign
    )
    expected_payment_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_PAYMENT, campaign
    )
    expected_mint_memos = memos.build_memo_models(
        memos.INITIATOR_BACKEND, platform, memos.ACTION_MINT, campaign
    )
    expected_accept_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_ACCEPT_OFFER, campaign
    )
    if _flags_value(batch.flags) != int(BatchFlag.TF_ALL_OR_NOTHING):
        raise AtomicMintInvariantError("Batch must be ALLORNOTHING")
    if (
        batch.account != buyer
        or batch.source_tag != source_tag
        or batch.memos != expected_outer_memos
        or len(batch.raw_transactions) != 3
    ):
        raise AtomicMintInvariantError("outer Batch mismatch")
    pay, mint, accept = batch.raw_transactions
    if not (
        isinstance(pay, Payment)
        and isinstance(mint, NFTokenMint)
        and isinstance(accept, NFTokenAcceptOffer)
    ):
        raise AtomicMintInvariantError("wrong inner order")
    inner_flag = int(TransactionFlag.TF_INNER_BATCH_TXN)
    if (
        pay.account != buyer
        or pay.destination != payment.destination
        or pay.amount != payment.amount
        or _flags_value(pay.flags) != inner_flag
        or pay.source_tag != source_tag
        or pay.memos != expected_payment_memos
        or pay.ticket_sequence is not None
    ):
        raise AtomicMintInvariantError("payment mismatch")
    expected_issuer = nft_issuer if nft_issuer != issuer_account else None
    if (
        mint.account != issuer_account
        or mint.sequence != 0
        or mint.ticket_sequence != issuer_ticket
        or _flags_value(mint.flags) != (nft_flags | inner_flag)
        or mint.nftoken_taxon != nft_taxon
        or mint.issuer != expected_issuer
        or mint.source_tag != source_tag
        or mint.memos != expected_mint_memos
    ):
        raise AtomicMintInvariantError("issuer mint mismatch")
    if (
        mint.amount != "0"
        or mint.destination != buyer
        or mint.uri != metadata_url.encode().hex().upper()
    ):
        raise AtomicMintInvariantError("mint offer mismatch")
    expected_fee = transfer_fee if nft_flags & 0x0008 else None
    if mint.transfer_fee != expected_fee:
        raise AtomicMintInvariantError("transfer fee mismatch")
    if (
        accept.account != buyer
        or accept.nftoken_sell_offer != nft_offer_id(issuer_account, issuer_ticket)
        or _flags_value(accept.flags) != inner_flag
        or accept.source_tag != source_tag
        or accept.memos != expected_accept_memos
        or accept.ticket_sequence is not None
    ):
        raise AtomicMintInvariantError("accept offer mismatch")
    if (
        batch.sequence is None
        or pay.sequence != batch.sequence + 1
        or accept.sequence != batch.sequence + 2
    ):
        raise AtomicMintInvariantError("buyer sequence allocation mismatch")
    for transaction in batch.raw_transactions:
        if (
            transaction.fee != "0"
            or transaction.signing_pub_key != ""
            or transaction.txn_signature is not None
            or transaction.signers is not None
        ):
            raise AtomicMintInvariantError("inner signing fields invalid")


def sign_issuer_batch(
    batch: Batch, *, wallet: Wallet, issuer_account: str
) -> Batch:
    """Attach the issuer's BatchSigner, including regular-key deployments."""

    if issuer_account not in {tx.account for tx in batch.raw_transactions}:
        raise AtomicMintInvariantError("issuer is not an inner account")
    fields: Any = {
        "flags": _flags_value(batch.flags),
        "transaction_ids": [tx.get_hash() for tx in batch.raw_transactions],
    }
    signature = keypairs.sign(encode_for_signing_batch(fields), wallet.private_key)
    signer = BatchSigner(
        account=issuer_account,
        signing_pub_key=wallet.public_key,
        txn_signature=signature,
    )
    return replace(batch, batch_signers=[signer])


async def prepare_atomic_mint_batch(
    *,
    client: Any,
    wallet: Wallet,
    buyer: str,
    issuer_account: str,
    nft_issuer: str,
    issuer_ticket: int,
    metadata_url: str,
    payment: MintPayment,
    platform: str,
    campaign: str | None,
    nft_flags: int,
    nft_taxon: int,
    transfer_fee: int,
    source_tag: int,
) -> PreparedBatch:
    """Autofill, validate, and issuer-sign one fixed atomic mint Batch."""

    draft = build_atomic_mint_batch(
        buyer=buyer,
        issuer_account=issuer_account,
        nft_issuer=nft_issuer,
        issuer_ticket=issuer_ticket,
        metadata_url=metadata_url,
        payment=payment,
        platform=platform,
        campaign=campaign,
        nft_flags=nft_flags,
        nft_taxon=nft_taxon,
        transfer_fee=transfer_fee,
        source_tag=source_tag,
    )
    filled = await autofill(draft, client, signers_count=1)
    if not isinstance(filled, Batch):
        raise AtomicMintInvariantError("autofill returned a non-Batch transaction")
    validate_atomic_mint_batch(
        filled,
        buyer=buyer,
        issuer_account=issuer_account,
        nft_issuer=nft_issuer,
        issuer_ticket=issuer_ticket,
        payment=payment,
        metadata_url=metadata_url,
        platform=platform,
        campaign=campaign,
        nft_flags=nft_flags,
        nft_taxon=nft_taxon,
        transfer_fee=transfer_fee,
        source_tag=source_tag,
    )
    signed = sign_issuer_batch(
        filled, wallet=wallet, issuer_account=issuer_account
    )
    inner_hashes = tuple(tx.get_hash() for tx in signed.raw_transactions)
    if len(inner_hashes) != 3 or signed.last_ledger_sequence is None:
        raise AtomicMintInvariantError("autofilled Batch is incomplete")
    return PreparedBatch(
        transaction=signed,
        offer_id=nft_offer_id(issuer_account, issuer_ticket),
        inner_hashes=(inner_hashes[0], inner_hashes[1], inner_hashes[2]),
        last_ledger_sequence=signed.last_ledger_sequence,
    )


def _transaction_json(result: Mapping[str, Any]) -> Mapping[str, Any]:
    transaction = result.get("tx_json")
    return transaction if isinstance(transaction, dict) else result


def _minted_nft_id(meta: Mapping[str, Any]) -> str | None:
    direct = meta.get("nftoken_id") or meta.get("NFTokenID")
    if direct:
        return str(direct)
    try:
        derived = get_nftoken_id(dict(meta))  # type: ignore[arg-type]
    except (IndexError, KeyError, TypeError):
        return None
    return str(derived) if derived else None


async def verify_atomic_batch_result(
    *,
    outer_hash: str,
    inner_hashes: tuple[str, str, str],
    expected_offer_id: str,
    fetch_tx: Callable[[str], Awaitable[Mapping[str, Any] | None]],
) -> VerifiedAtomicMint | None:
    """Verify the outer and all fixed inner hashes as one successful ledger unit.

    Missing or not-yet-validated transactions are pending (`None`).  A
    validated contradiction is definitive and raises rather than being retried
    as if it could later become the approved action.
    """

    hashes = (outer_hash, *inner_hashes)
    results = [await fetch_tx(tx_hash) for tx_hash in hashes]
    if any(result is None or not result.get("validated") for result in results):
        return None
    validated = [result for result in results if result is not None]
    if len(validated) != 4:
        return None
    ledger_indexes = {result.get("ledger_index") for result in validated}
    ledger_index = next(iter(ledger_indexes)) if len(ledger_indexes) == 1 else None
    if not isinstance(ledger_index, int):
        raise AtomicMintInvariantError("Batch transactions did not validate together")
    outer, payment, mint, accept = validated
    outer_meta = outer.get("meta")
    if (
        not isinstance(outer_meta, dict)
        or outer_meta.get("TransactionResult") != "tesSUCCESS"
        or _transaction_json(outer).get("TransactionType") != "Batch"
    ):
        raise AtomicMintInvariantError("outer Batch failed or mismatched")
    expected_types = ("Payment", "NFTokenMint", "NFTokenAcceptOffer")
    for result, expected_type in zip(
        (payment, mint, accept), expected_types, strict=True
    ):
        meta = result.get("meta")
        transaction = _transaction_json(result)
        if (
            not isinstance(meta, dict)
            or meta.get("TransactionResult") != "tesSUCCESS"
            or meta.get("ParentBatchID") != outer_hash
            or transaction.get("TransactionType") != expected_type
        ):
            raise AtomicMintInvariantError(
                f"{expected_type} inner result failed or mismatched"
            )
    accept_transaction = _transaction_json(accept)
    if accept_transaction.get("NFTokenSellOffer") != expected_offer_id:
        raise AtomicMintInvariantError("accepted NFT offer does not match prepared offer")
    mint_meta = mint["meta"]
    accept_meta = accept["meta"]
    nft_id = _minted_nft_id(mint_meta)
    if not nft_id:
        raise AtomicMintInvariantError("mint result did not identify an NFToken")
    accepted_nft_id = accept_meta.get("nftoken_id") or accept_meta.get("NFTokenID")
    if accepted_nft_id is not None and str(accepted_nft_id) != nft_id:
        raise AtomicMintInvariantError("accepted NFToken differs from minted NFToken")
    affected = accept_meta.get("AffectedNodes")
    if isinstance(affected, list):
        deleted_offer = any(
            isinstance(node, dict)
            and isinstance(node.get("DeletedNode"), dict)
            and node["DeletedNode"].get("LedgerEntryType") == "NFTokenOffer"
            and node["DeletedNode"].get("LedgerIndex") == expected_offer_id
            for node in affected
        )
        if not deleted_offer:
            raise AtomicMintInvariantError("accept result did not consume prepared offer")
    return VerifiedAtomicMint(nft_id=nft_id, ledger_index=ledger_index)
