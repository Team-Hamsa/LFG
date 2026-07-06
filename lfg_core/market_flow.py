# lfg_core/market_flow.py
# In-app marketplace session flows: List / Cancel / Buy (#44 Task 8).
#
# Unlike mint/swap/economy, there is no multi-step orchestration to run in a
# background task before the user signs — each op is a single XUMM sign
# request (NFTokenCreateOffer / NFTokenCancelOffer / NFTokenAcceptOffer), so
# `lfg_service/app.py`'s POST handlers build the payload directly and return
# immediately. The state machine lives here in the `advance_*_session`
# functions, called by the GET status handlers on every poll:
#
# - List: XUMM's payload status yields a txid only, not meta (spec §Q4). Once
#   signed, `advance_list_session` fetches the tx by hash and only writes a
#   `market_listings` row once it is validated + tesSUCCESS (the offer index
#   is inside the tx meta, not knowable any earlier). Not-yet-validated is
#   `PENDING`; bounded at MAX_FINALIZE_POLLS, after which it gives up and
#   reports `UNKNOWN` (no row) — the listener/backfill self-heal from the
#   ledger regardless of whether this poller ever converges.
# - Cancel: needs only `signed` (an NFTokenCancelOffer either lands or it
#   doesn't — there's no created-object index to extract from meta).
# - Buy: mirrors List's tx-fetch — a signed NFTokenAcceptOffer can still fail
#   on-ledger (tecOBJECT_NOT_FOUND) if the offer was filled/cancelled between
#   this app's verify step and the user signing (spec §Q4's documented race);
#   the caller maps that outcome to `{"state": "failed", "reason":
#   "listing_unavailable"}` and marks the listing stale.
#
# These functions are deliberately DB-free: they return a plain dict/str
# describing what changed, and lfg_service/app.py performs the actual
# sqlite write (via run_in_executor, same posture as every other market_store
# call site in app.py) using the per-listing-kind network it already knows.
# This keeps the state machine synchronous-I/O-free and unit-testable
# (tests/test_market_flow.py) without a throwaway sqlite fixture per case.

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from lfg_core import market_ops, xrpl_ops, xumm_ops

# Shared session states across List/Cancel/Buy.
AWAITING_SIGNATURE = "awaiting_signature"
PENDING = "pending"  # signed; tx not yet validated (List/Buy only)
DONE = "done"
FAILED = "failed"
UNKNOWN = "unknown"  # tx lookup gave up/failed (List/Buy only) — self-heals later

TERMINAL_STATES = {DONE, FAILED, UNKNOWN}

# ~30s at typical frontend poll intervals (spec §Q4: "bounded at 10 polls (~30s)").
MAX_FINALIZE_POLLS = 10


@dataclass
class ListSession:
    discord_id: str
    wallet_address: str
    nft_id: str
    listing_kind: str  # 'character' | 'trait' — the LISTING's kind
    amount_drops: int
    slot: str | None = None
    value: str | None = None
    platform: str = "discord"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    state: str = AWAITING_SIGNATURE
    error: str | None = None
    payload_uuid: str | None = None
    qr_url: str | None = None
    xumm_url: str | None = None
    txid: str | None = None
    poll_count: int = 0
    offer_index: str | None = None
    kind: str = "list"  # the OP kind, for the shared session-dict status router

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "qr_url": self.qr_url,
            "xumm_url": self.xumm_url,
            "offer_index": self.offer_index,
        }


@dataclass
class CancelSession:
    discord_id: str
    wallet_address: str
    offer_index: str
    network: str  # the listing's onchain db network, resolved at start time
    platform: str = "discord"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    state: str = AWAITING_SIGNATURE
    error: str | None = None
    payload_uuid: str | None = None
    qr_url: str | None = None
    xumm_url: str | None = None
    kind: str = "cancel"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "qr_url": self.qr_url,
            "xumm_url": self.xumm_url,
            "offer_index": self.offer_index,
        }


@dataclass
class BuySession:
    discord_id: str
    wallet_address: str
    offer_index: str
    nft_id: str
    listing_kind: str
    network: str
    amount_drops: int
    platform: str = "discord"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    state: str = AWAITING_SIGNATURE
    error: str | None = None
    reason: str | None = None  # "listing_unavailable" on the post-sign race
    payload_uuid: str | None = None
    qr_url: str | None = None
    xumm_url: str | None = None
    instruction: str | None = None
    txid: str | None = None
    poll_count: int = 0
    kind: str = "buy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "reason": self.reason,
            "qr_url": self.qr_url,
            "xumm_url": self.xumm_url,
            "instruction": self.instruction,
            "offer_index": self.offer_index,
        }


async def advance_list_session(
    session: ListSession,
    *,
    get_payload_status: Any = None,
    get_tx: Any = None,
) -> dict[str, Any] | None:
    """Advance a ListSession from its XUMM + tx status. Returns a dict ready
    for `market_store.upsert_listing(conn, MarketListing(**dict))` the poll a
    tesSUCCESS NFTokenCreateOffer is confirmed and its offer index extracted;
    None every other poll (mutates session.state/error/txid/poll_count/
    offer_index in place — the caller only needs to inspect `session` and the
    return value, never re-derive anything from XUMM/tx responses itself).

    `get_payload_status`/`get_tx` default to the real xumm_ops/xrpl_ops
    functions, resolved at call time (NOT bound as ordinary default argument
    values — those are captured once at import, before a test's monkeypatch
    of xumm_ops/xrpl_ops can ever reach them) so callers that invoke this
    with no overrides (lfg_service/app.py) still observe a monkeypatched
    xumm_ops.get_payload_status / xrpl_ops.get_tx in tests."""
    get_payload_status = get_payload_status or xumm_ops.get_payload_status
    get_tx = get_tx or xrpl_ops.get_tx
    if session.state not in (AWAITING_SIGNATURE, PENDING):
        return None  # terminal: never re-poll (also protects against a
        # duplicate write if the caller polls again after DONE)

    if session.txid is None:
        s = await get_payload_status(session.payload_uuid)
        if s is None:
            return None  # transient XUMM API error; try again next poll
        if s.get("expired"):
            session.state = FAILED
            session.error = "signing request expired"
            return None
        if not s.get("signed"):
            return None  # still awaiting_signature
        txid = s.get("txid")
        if not txid:
            return None  # signed, but XUMM hasn't surfaced the txid yet
        session.txid = txid
        session.state = PENDING

    try:
        tx = await get_tx(session.txid)
    except Exception as e:
        session.state = UNKNOWN
        session.error = f"tx lookup failed: {e}"
        return None

    if not tx.get("validated"):
        session.poll_count += 1
        if session.poll_count >= MAX_FINALIZE_POLLS:
            session.state = UNKNOWN
            session.error = "gave up waiting for validation"
        return None

    meta = tx.get("meta") or {}
    if meta.get("TransactionResult") != "tesSUCCESS":
        session.state = FAILED
        session.error = f"transaction failed: {meta.get('TransactionResult')}"
        return None

    extracted = market_ops.extract_created_sell_offer(meta, session.nft_id)
    if extracted is None:
        session.state = FAILED
        session.error = "could not find the created sell offer in transaction metadata"
        return None

    session.offer_index = extracted["offer_index"]
    session.state = DONE
    return {
        "offer_index": extracted["offer_index"],
        "nft_id": session.nft_id,
        "kind": session.listing_kind,
        "seller": session.wallet_address,
        "amount_drops": session.amount_drops,
        "slot": session.slot,
        "value": session.value,
    }


async def advance_cancel_session(
    session: CancelSession,
    *,
    get_payload_status: Any = None,
) -> bool:
    """Advance a CancelSession from its XUMM status. Returns True exactly
    once — the poll `signed` first becomes true — signalling the caller to
    `close_listing(reason='cancelled')`; False every other poll (including
    every poll after that first True, since the session is then terminal).

    See advance_list_session's docstring for why `get_payload_status` is
    resolved at call time rather than bound as a default argument value."""
    get_payload_status = get_payload_status or xumm_ops.get_payload_status
    if session.state != AWAITING_SIGNATURE:
        return False
    s = await get_payload_status(session.payload_uuid)
    if s is None:
        return False
    if s.get("expired"):
        session.state = FAILED
        session.error = "signing request expired"
        return False
    if not s.get("signed"):
        return False
    session.state = DONE
    return True


async def advance_buy_session(
    session: BuySession,
    *,
    get_payload_status: Any = None,
    get_tx: Any = None,
) -> str | None:
    """Advance a BuySession from its XUMM + tx status. Returns "sold" the
    poll a tesSUCCESS NFTokenAcceptOffer is confirmed (caller closes the
    listing reason='sold' and — for a trait listing — triggers settlement,
    see `trigger_trait_settlement`); "stale" the poll an on-ledger failure
    surfaces post-sign (the documented verify/sign race: the offer was
    filled/cancelled first — caller closes reason='stale'); None every other
    poll.

    See advance_list_session's docstring for why `get_payload_status`/
    `get_tx` are resolved at call time rather than bound as default
    argument values."""
    get_payload_status = get_payload_status or xumm_ops.get_payload_status
    get_tx = get_tx or xrpl_ops.get_tx
    if session.state not in (AWAITING_SIGNATURE, PENDING):
        return None

    if session.txid is None:
        s = await get_payload_status(session.payload_uuid)
        if s is None:
            return None
        if s.get("expired"):
            session.state = FAILED
            session.error = "signing request expired"
            return None
        if not s.get("signed"):
            return None
        txid = s.get("txid")
        if not txid:
            return None
        session.txid = txid
        session.state = PENDING

    try:
        tx = await get_tx(session.txid)
    except Exception as e:
        session.state = UNKNOWN
        session.error = f"tx lookup failed: {e}"
        return None

    if not tx.get("validated"):
        session.poll_count += 1
        if session.poll_count >= MAX_FINALIZE_POLLS:
            session.state = UNKNOWN
            session.error = "gave up waiting for validation"
        return None

    meta = tx.get("meta") or {}
    if meta.get("TransactionResult") == "tesSUCCESS":
        session.state = DONE
        return "sold"

    session.state = FAILED
    session.reason = "listing_unavailable"
    session.error = f"transaction failed: {meta.get('TransactionResult')}"
    return "stale"


def trigger_trait_settlement(offer_index: str) -> None:
    """Seam for Task 9 (spec §Q7): a confirmed trait sale needs the sold
    trait token burned back into the buyer's Closet (settlement). This is
    deliberately a no-op — Task 9 implements the real trigger (most likely
    scheduling `run_deposit` on the buyer's behalf and `mark_settled` on
    success). Called exactly once per confirmed trait sale, from the buy
    status handler in lfg_service/app.py.

    TODO(Task 9): kick off trait-sale settlement here.
    """
    return None
