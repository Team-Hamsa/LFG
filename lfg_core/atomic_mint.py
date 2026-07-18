"""Restart-safe orchestration for one-approval XRPL atomic mint actions."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from lfg_core import xrpl_actions

PREPARING = "preparing"
AWAITING_SIGNATURE = "awaiting_signature"
CONFIRMING = "confirming"
DONE = "done"
REJECTED = "rejected"
EXPIRED = "expired"
FAILED = "failed"
INDETERMINATE = "indeterminate"
TERMINAL_STATES = {DONE, REJECTED, EXPIRED, FAILED, INDETERMINATE}


@dataclass
class AtomicMintSession:
    id: str
    user_id: str
    wallet: str
    platform: str
    network: str
    campaign: str | None = None
    state: str = PREPARING
    created_at: int = field(default_factory=lambda: int(time.time()))
    pay_with: str | None = None
    pay_amount: str | None = None
    payment: xrpl_actions.MintPayment | None = None
    nft_number: int | None = None
    metadata_url: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    traits: dict[str, str] | None = None
    body_type: str | None = None
    ticket_sequence: int | None = None
    offer_id: str | None = None
    batch_json: dict[str, Any] | None = None
    inner_hashes: tuple[str, str, str] | None = None
    last_ledger_sequence: int | None = None
    xumm_uuid: str | None = None
    xumm_url: str | None = None
    qr_url: str | None = None
    push: str | None = None
    issued_user_token: str | None = None
    outer_hash: str | None = None
    nft_id: str | None = None
    ledger_index: int | None = None
    error_code: str | None = None
    headroom_reserved: bool = False
    assets_prepared: bool = False
    return_url: dict[str, str] | None = field(default=None, repr=False)
    push_user_token: str | None = field(default=None, repr=False)
    _published: bool = field(default=False, repr=False)

    @classmethod
    def new(
        cls,
        *,
        user_id: str,
        wallet: str,
        platform: str,
        network: str,
        campaign: str | None = None,
    ) -> AtomicMintSession:
        return cls(
            uuid.uuid4().hex,
            user_id,
            wallet,
            platform,
            network,
            campaign=campaign,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": "xrpl-action-session",
            "version": "1",
            "sessionId": self.id,
            "state": self.state,
            "pay_with": self.pay_with,
            "pay_amount": self.pay_amount,
            "nft_number": self.nft_number,
            "image_url": self.image_url,
            "video_url": self.video_url,
            "error_code": self.error_code,
        }
        if self.state == AWAITING_SIGNATURE:
            data.update(
                {
                    "type": "xrpl-sign-request",
                    "account": self.wallet,
                    "transaction": self.batch_json,
                    "wallets": {
                        "xaman": {
                            "uuid": self.xumm_uuid,
                            "deeplink": self.xumm_url,
                            "qr": self.qr_url,
                            "push": self.push,
                        }
                    },
                }
            )
        if self.state == DONE:
            data.update(
                {
                    "outer_hash": self.outer_hash,
                    "inner_hashes": list(self.inner_hashes or ()),
                    "nft_id": self.nft_id,
                    "ledger_index": self.ledger_index,
                }
            )
        return data


@dataclass
class AtomicMintDeps:
    capability: Callable[[], Awaitable[xrpl_actions.BatchCapability]]
    choose_payment: Callable[[str], Awaitable[xrpl_actions.MintPayment]]
    reserve_headroom: Callable[[AtomicMintSession], Awaitable[bool]]
    allocate_number: Callable[[], Awaitable[int]]
    prepare_assets: Callable[[int, str], Awaitable[Any]]
    list_tickets: Callable[[], Awaitable[list[int]]]
    lease_ticket: Callable[[AtomicMintSession, list[int]], Awaitable[int | None]]
    prepare_batch: Callable[[AtomicMintSession, Any, Any], Awaitable[Any]]
    persist: Callable[[AtomicMintSession], Awaitable[None]]
    create_payload: Callable[
        [AtomicMintSession], Awaitable[dict[str, Any] | None]
    ]
    payload_status: Callable[[str], Awaitable[dict[str, Any] | None]]
    verify_batch: Callable[
        [AtomicMintSession], Awaitable[xrpl_actions.VerifiedAtomicMint | None]
    ]
    current_ledger: Callable[[], Awaitable[int]]
    ledger_tickets: Callable[[], Awaitable[list[int]]]
    record_mint: Callable[[AtomicMintSession, str], Awaitable[bool]]
    buy_and_burn: Callable[[AtomicMintSession], Awaitable[None]]
    settle_headroom: Callable[[AtomicMintSession, bool], Awaitable[None]]
    discard_assets: Callable[[AtomicMintSession], Awaitable[None]]
    release_number: Callable[[AtomicMintSession], Awaitable[None]]
    release_ticket: Callable[[AtomicMintSession], Awaitable[bool]]
    consume_ticket: Callable[[AtomicMintSession], Awaitable[None]]
    quarantine_ticket: Callable[[AtomicMintSession], Awaitable[None]]


async def _persist_failure(
    session: AtomicMintSession, deps: AtomicMintDeps, code: str
) -> None:
    session.state = FAILED
    session.error_code = code
    await deps.persist(session)


async def _cleanup_unpublished(
    session: AtomicMintSession, deps: AtomicMintDeps
) -> None:
    if session.assets_prepared:
        await deps.discard_assets(session)
        session.assets_prepared = False
    if session.nft_number is not None:
        await deps.release_number(session)
    if session.headroom_reserved:
        await deps.settle_headroom(session, False)
        session.headroom_reserved = False


async def prepare_session(
    session: AtomicMintSession, deps: AtomicMintDeps
) -> None:
    """Prepare, freeze, persist, then publish exactly one Batch request."""

    capability = await deps.capability()
    if not capability.enabled:
        await _persist_failure(
            session, deps, capability.reason or "batch_unavailable"
        )
        return
    if not await deps.reserve_headroom(session):
        await _persist_failure(session, deps, "capacity_reached")
        return
    session.headroom_reserved = True
    await deps.persist(session)
    try:
        payment = await deps.choose_payment(session.wallet)
        session.payment = payment
        session.pay_with = payment.pay_with
        session.pay_amount = payment.display_amount
        session.nft_number = await deps.allocate_number()
        await deps.persist(session)
        assets = await deps.prepare_assets(
            session.nft_number, f"action:{session.id}"
        )
        session.metadata_url = assets.metadata_url
        session.image_url = assets.image_url
        session.video_url = assets.video_url
        session.traits = dict(assets.traits)
        session.body_type = assets.body_type
        session.assets_prepared = True
        await deps.persist(session)
        tickets = await deps.list_tickets()
        ticket = await deps.lease_ticket(session, tickets)
        if ticket is None:
            await _cleanup_unpublished(session, deps)
            await _persist_failure(session, deps, "ticket_unavailable")
            return
        session.ticket_sequence = ticket
        await deps.persist(session)
        try:
            prepared = await deps.prepare_batch(session, assets, payment)
        except Exception:
            # No request was published and no signed transaction can exist.
            await deps.release_ticket(session)
            await _cleanup_unpublished(session, deps)
            await _persist_failure(session, deps, "batch_prepare_failed")
            return
        canonical = prepared.transaction.to_xrpl()
        if not isinstance(canonical, dict) or canonical.get("TransactionType") != "Batch":
            await deps.release_ticket(session)
            await _cleanup_unpublished(session, deps)
            await _persist_failure(session, deps, "batch_prepare_failed")
            return
        session.batch_json = canonical
        session.offer_id = prepared.offer_id
        session.inner_hashes = prepared.inner_hashes
        session.last_ledger_sequence = prepared.last_ledger_sequence
        # This is the durability boundary: the fixed transaction and its lease
        # exist before Xaman can show or push it to anybody.
        await deps.persist(session)
        payload = await deps.create_payload(session)
        if not payload:
            # Creation timeouts are ambiguous: Xaman may have created/pushed
            # the fixed request. Keep Ticket/headroom through ledger finality.
            await _persist_failure(session, deps, "signing_unavailable")
            return
        session.xumm_uuid = payload.get("uuid")
        session.xumm_url = payload.get("xumm_url")
        session.qr_url = payload.get("qr_url")
        session.push = payload.get("push")
        if not session.xumm_uuid or not session.xumm_url:
            await _persist_failure(session, deps, "signing_unavailable")
            return
        session.state = AWAITING_SIGNATURE
        session.error_code = None
        await deps.persist(session)
    except Exception:
        logging.exception("atomic mint preparation failed")
        if session.ticket_sequence is None:
            await _cleanup_unpublished(session, deps)
        await _persist_failure(session, deps, "preparation_failed")


async def _settle_verified_session(
    session: AtomicMintSession,
    deps: AtomicMintDeps,
    verified: xrpl_actions.VerifiedAtomicMint,
) -> None:
    session.nft_id = verified.nft_id
    session.ledger_index = verified.ledger_index
    saved = await deps.record_mint(session, verified.nft_id)
    if not saved:
        session.error_code = "record_recovery_required"
    if session.pay_with == "XRP":
        try:
            await deps.buy_and_burn(session)
        except Exception:
            logging.exception("post-mint XRP buy-and-burn failed")
    await deps.consume_ticket(session)
    if session.headroom_reserved:
        await deps.settle_headroom(session, True)
        session.headroom_reserved = False
    session.state = DONE
    await deps.persist(session)


async def refresh_session(
    session: AtomicMintSession, deps: AtomicMintDeps
) -> None:
    """Advance a live session from Xaman status and fixed ledger hashes."""

    if session.state == AWAITING_SIGNATURE and session.xumm_uuid:
        status = await deps.payload_status(session.xumm_uuid)
        if not status:
            return
        if status.get("signed") and status.get("account") == session.wallet:
            issued_token = status.get("user_token")
            if issued_token:
                session.issued_user_token = str(issued_token)
            txid = status.get("txid")
            if not txid:
                return
            session.outer_hash = str(txid)
            session.state = CONFIRMING
            await deps.persist(session)
        elif status.get("signed"):
            await _persist_failure(session, deps, "wallet_mismatch")
            return
        elif status.get("cancelled"):
            session.state = REJECTED
            session.error_code = "rejected"
            await deps.persist(session)
            return
        elif status.get("expired"):
            session.state = EXPIRED
            session.error_code = "expired"
            await deps.persist(session)
            return
    if session.state == CONFIRMING:
        try:
            verified = await deps.verify_batch(session)
        except xrpl_actions.AtomicMintInvariantError:
            session.state = FAILED
            session.error_code = "batch_failed"
            await deps.persist(session)
            return
        if verified is not None:
            await _settle_verified_session(session, deps, verified)


async def reconcile_session(
    session: AtomicMintSession, deps: AtomicMintDeps
) -> None:
    """Resolve one persisted lease without ever guessing that it is reusable."""

    if session.state == DONE:
        return
    lookup_failed = False
    if session.outer_hash:
        try:
            verified = await deps.verify_batch(session)
        except xrpl_actions.AtomicMintInvariantError:
            verified = None
        except Exception:
            verified = None
            lookup_failed = True
        if verified is not None:
            await _settle_verified_session(session, deps, verified)
            return
    if session.ticket_sequence is None:
        await _cleanup_unpublished(session, deps)
        if session.state not in TERMINAL_STATES:
            session.state = FAILED
            session.error_code = "interrupted_preparation"
        await deps.persist(session)
        return
    if session.last_ledger_sequence is None:
        session.state = INDETERMINATE
        session.error_code = "outcome_indeterminate"
        await deps.quarantine_ticket(session)
        await deps.persist(session)
        return
    try:
        current = await deps.current_ledger()
    except Exception:
        return
    if current <= session.last_ledger_sequence:
        return
    if lookup_failed:
        session.state = INDETERMINATE
        session.error_code = "outcome_indeterminate"
        await deps.quarantine_ticket(session)
        await deps.persist(session)
        return
    try:
        ledger_tickets = await deps.ledger_tickets()
    except Exception:
        session.state = INDETERMINATE
        session.error_code = "outcome_indeterminate"
        await deps.quarantine_ticket(session)
        await deps.persist(session)
        return
    if session.ticket_sequence in ledger_tickets:
        released = await deps.release_ticket(session)
        if released:
            session.ticket_sequence = None
        await _cleanup_unpublished(session, deps)
        if session.state not in (REJECTED, EXPIRED, FAILED):
            session.state = EXPIRED
            session.error_code = "expired"
        await deps.persist(session)
        return
    session.state = INDETERMINATE
    session.error_code = "outcome_indeterminate"
    await deps.quarantine_ticket(session)
    await deps.persist(session)


async def reconcile_sessions(
    sessions: list[AtomicMintSession], deps: AtomicMintDeps
) -> None:
    for session in sessions:
        await reconcile_session(session, deps)
