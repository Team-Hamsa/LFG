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

from lfg_core import market_ops, memos, xrpl_ops, xumm_ops

# Shared session states across List/Cancel/Buy.
AWAITING_SIGNATURE = "awaiting_signature"
PENDING = "pending"  # signed; tx not yet validated (List/Buy only)
DONE = "done"
FAILED = "failed"
UNKNOWN = "unknown"  # tx lookup gave up/failed (List/Buy only) — self-heals later

# --- Trait sell wizard (Task 9, spec §Q7): Extract (existing Phase-4 flow,
# Xaman signature 1) -> the plain List flow on the freshly-owned token (Xaman
# signature 2), driven together as one polled TraitSellSession so the frontend
# can treat "sell a trait out of my Closet" as one action with two QR steps.
EXTRACT_PENDING = "extract_pending"  # the ExtractSession's background task is still running
EXTRACT_DONE = "extract_done"  # extract finished; showing the accept-offer QR (signature 1)
LIST_PENDING = "list_pending"  # signature 1 confirmed; showing the sell-offer QR (signature 2)
LISTED = "listed"  # terminal success (named for the wizard's own status field)

# economy_flow.RUNNING mirrored as a literal: market_flow has never imported
# economy_flow and TraitSellSession.extract_session is deliberately duck-typed
# (read via .state/.error/.nft_id/.accept, the same shape EconomyWebSession.inner
# already carries) so this module's dependency graph stays flat. DONE/FAILED
# below already match economy_flow's own "done"/"failed" constants verbatim.
_EXTRACT_RUNNING = "running"

TERMINAL_STATES = {DONE, FAILED, UNKNOWN, LISTED}

# On a validated-but-failed NFTokenAcceptOffer, only these tec codes mean the
# offer itself is gone, so the caller may stale-close the listing:
# tecOBJECT_NOT_FOUND (consumed/cancelled between verify and sign — spec §Q4's
# documented race) and tecEXPIRED (the offer carried an Expiration that lapsed
# before the accept landed, #183). Both are seller-/offer-side terminal states,
# not buyer error. Every OTHER failure is buyer-side (tecINSUFFICIENT_FUNDS,
# tecCANT_ACCEPT_OWN_OFFER, …) or unknown — the offer is still healthy, so we
# fail the session WITHOUT closing the row (closing it would be a griefing
# lever: a broke/self-accepting buyer could delist anyone's listing). Be
# conservative: unknown tec ⇒ leave live.
_OFFER_GONE_TEC_CODES = frozenset({"tecOBJECT_NOT_FOUND", "tecEXPIRED"})

# ~30s at typical frontend poll intervals (spec §Q4: "bounded at 10 polls (~30s)").
MAX_FINALIZE_POLLS = 10


def _capture_issued_token(session: Any, s: dict[str, Any]) -> None:
    """#212: stamp the push token XUMM issued on a signed payload so the
    service can persist it (tokens rotate; capturing on every signed payload —
    not just sign-in — keeps them fresh and self-heals an app-key swap). Only
    when the signer IS the session's wallet: a shared QR signed by a different
    account must never overwrite this user's stored token. Sessions that build
    a LATER payload from their own stored token (the trait-sell wizard) get
    that token refreshed too, so signature 2 already uses the rotated one."""
    if s.get("signed") and s.get("user_token") and s.get("account") == session.wallet_address:
        session.issued_user_token = s["user_token"]
        if hasattr(session, "push_user_token"):
            session.push_user_token = s["user_token"]


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
    # #212: push delivery state of this op's sign request ("sent" | "failed" |
    # None) and the fresh push token observed once it is signed (cleared by
    # the service after persisting). Shared shape across List/Cancel/Buy.
    push: str | None = None
    issued_user_token: str | None = field(default=None, repr=False)
    kind: str = "list"  # the OP kind, for the shared session-dict status router

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "qr_url": self.qr_url,
            "xumm_url": self.xumm_url,
            "push": self.push,
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
    push: str | None = None  # see ListSession
    issued_user_token: str | None = field(default=None, repr=False)
    kind: str = "cancel"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "qr_url": self.qr_url,
            "xumm_url": self.xumm_url,
            "push": self.push,
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
    push: str | None = None  # see ListSession
    issued_user_token: str | None = field(default=None, repr=False)
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
            "push": self.push,
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
    for `market_store.record_listing_creation(conn, MarketListing(**dict))` the poll a
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
        _capture_issued_token(session, s)
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
        # On-ledger truth from the CreatedNode, not session.amount_drops — the
        # signed sell offer's Amount is what a buyer will actually pay.
        "amount_drops": extracted["amount_drops"],
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
    _capture_issued_token(session, s)
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
        # Signer must be the buyer who started this session. A registered
        # buyer could start a buy, share the QR, and have a DIFFERENT wallet
        # sign it: the ledger sale then succeeds for that other wallet while
        # this session would otherwise close the listing and (for a trait)
        # settle via session.wallet_address — the wrong owner, leaving the
        # paid trait unsettled. Fail this session without touching the row;
        # the listener's accept path closes/attributes the listing to the
        # real signer from on-ledger truth. Fail-closed: a missing signer
        # account is treated as a mismatch.
        if s.get("account") != session.wallet_address:
            session.state = FAILED
            session.error = "buy offer signed by a different account than the buyer"
            session.reason = "signer_mismatch"
            return None
        _capture_issued_token(session, s)
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
    result = meta.get("TransactionResult")
    if result == "tesSUCCESS":
        session.state = DONE
        return "sold"

    session.state = FAILED
    session.error = f"transaction failed: {result}"
    if result in _OFFER_GONE_TEC_CODES:
        # The offer is genuinely gone (verify/sign race) — stale-close the row.
        session.reason = "listing_unavailable"
        return "stale"
    # Buyer-side / unknown failure: the offer is still on-ledger. Fail the
    # session but leave the listing live (return None ⇒ no close).
    session.reason = "purchase_failed"
    return None


@dataclass
class TraitSellSession:
    """The composite "sell a trait out of my Closet" wizard (spec §Q7):
    Extract (existing Phase-4 `economy_flow.ExtractSession`, run in the
    background by `economy_api.start_extract` — Xaman signature 1) then the
    plain Q4 List flow on the freshly-owned token (Xaman signature 2).

    `extract_session` is intentionally `Any` (duck-typed): see the
    `_EXTRACT_RUNNING` comment above for why this module doesn't import
    economy_flow. `_list_session`/`_extract_payload_uuid` are private working
    state for `advance_trait_sell_session`, not part of the client-facing
    `to_dict()`."""

    discord_id: str
    wallet_address: str
    slot: str
    value: str
    amount_drops: int
    extract_session: Any
    platform: str = "discord"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    state: str = EXTRACT_PENDING
    error: str | None = None
    nft_id: str | None = None
    offer_index: str | None = None
    extract_qr_url: str | None = None
    extract_xumm_url: str | None = None
    list_qr_url: str | None = None
    list_xumm_url: str | None = None
    # #135/#212: stored push token for this user (threaded into the wizard's
    # own List payload), per-signature push states for the UI, and the fresh
    # token observed once a signature lands (cleared by the service).
    push_user_token: str | None = field(default=None, repr=False)
    extract_push: str | None = None
    list_push: str | None = None
    issued_user_token: str | None = field(default=None, repr=False)
    _extract_payload_uuid: str | None = field(default=None, repr=False)
    _list_session: Any = field(default=None, repr=False)
    kind: str = "trait_list"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "nft_id": self.nft_id,
            "offer_index": self.offer_index,
            "extract_qr_url": self.extract_qr_url,
            "extract_xumm_url": self.extract_xumm_url,
            "extract_push": self.extract_push,
            "list_qr_url": self.list_qr_url,
            "list_xumm_url": self.list_xumm_url,
            "list_push": self.list_push,
        }


async def advance_trait_sell_session(
    session: TraitSellSession,
    *,
    get_payload_status: Any = None,
    create_sell_offer_payload: Any = None,
    get_tx: Any = None,
) -> dict[str, Any] | None:
    """Advance the trait-sell wizard through Extract -> List. Returns a dict
    ready for `market_store.record_listing_creation(conn, MarketListing(**dict))` the
    poll the embedded List step validates a tesSUCCESS NFTokenCreateOffer
    (mirrors `advance_list_session`'s own contract exactly — this delegates
    to it for that step); None on every other poll.

    See `advance_list_session`'s docstring for why the XUMM/tx callables are
    resolved at call time rather than bound as default argument values."""
    get_payload_status = get_payload_status or xumm_ops.get_payload_status
    create_sell_offer_payload = create_sell_offer_payload or xumm_ops.create_sell_offer_payload
    get_tx = get_tx or xrpl_ops.get_tx

    if session.state == EXTRACT_PENDING:
        extract = session.extract_session
        if extract.state == _EXTRACT_RUNNING:
            return None  # still minting/decrementing the Closet in the background
        if extract.state != DONE:
            session.state = FAILED
            session.error = extract.error or "extract failed"
            return None  # no listing, no orphan state beyond run_extract's own fail-safe
        session.nft_id = extract.nft_id
        accept = extract.accept or {}
        session.extract_qr_url = accept.get("qr_url")
        session.extract_xumm_url = accept.get("xumm_url")
        session.extract_push = accept.get("push")
        session._extract_payload_uuid = accept.get("uuid")
        session.state = EXTRACT_DONE
        return None

    if session.state == EXTRACT_DONE:
        if session.nft_id is None:
            # Unreachable in practice: EXTRACT_PENDING always sets nft_id
            # before transitioning here. Guarded for mypy's benefit and as a
            # fail-closed backstop rather than trusting the invariant blindly.
            session.state = FAILED
            session.error = "internal error: extract completed with no nft_id"
            return None
        # A self-offer skip (issuer-owned test/admin runs) carries no payload
        # uuid to wait on — proceed straight to listing.
        if session._extract_payload_uuid:
            s = await get_payload_status(session._extract_payload_uuid)
            if s is None:
                return None  # transient XUMM API error; try again next poll
            if s.get("expired"):
                session.state = FAILED
                session.error = "signing request for the trait handoff expired"
                return None
            if not s.get("signed"):
                return None  # still waiting on signature 1
            _capture_issued_token(session, s)

        payload = await create_sell_offer_payload(
            session.wallet_address,
            session.nft_id,
            str(session.amount_drops),
            user_token=session.push_user_token,
            platform=memos.platform_for_surface(session.platform),
        )
        if not payload:
            session.state = FAILED
            session.error = "could not reach Xaman to list the extracted trait"
            return None
        session.list_qr_url = payload["qr_url"]
        session.list_xumm_url = payload["xumm_url"]
        session.list_push = payload.get("push")
        inner = ListSession(
            discord_id=session.discord_id,
            wallet_address=session.wallet_address,
            nft_id=session.nft_id,
            listing_kind="trait",
            amount_drops=session.amount_drops,
            slot=session.slot,
            value=session.value,
            platform=session.platform,
        )
        inner.qr_url = payload["qr_url"]
        inner.xumm_url = payload["xumm_url"]
        inner.payload_uuid = payload.get("uuid")
        inner.push = payload.get("push")
        session._list_session = inner
        session.state = LIST_PENDING
        return None

    if session.state == LIST_PENDING:
        inner = session._list_session
        row = await advance_list_session(
            inner, get_payload_status=get_payload_status, get_tx=get_tx
        )
        # Bubble a token the inner List capture observed up to the wizard
        # session the service actually polls.
        if inner.issued_user_token:
            session.issued_user_token = inner.issued_user_token
            inner.issued_user_token = None
        if inner.state in (FAILED, UNKNOWN):
            session.state = FAILED
            session.error = inner.error
            return None
        if row is not None:
            session.offer_index = inner.offer_index
            session.state = LISTED
            return row
        return None

    return None
