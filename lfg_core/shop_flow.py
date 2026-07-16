"""Trait Shop buy flow (#217): quote-frozen BRIX purchase of an on-demand
minted trait token, settled into the buyer's Closet.

Order of operations (fail-safe, mirrors economy_flow conventions):
  precheck (service layer: economy enabled, active Closet, quote) ->
  mint trait token (reversible: revert = issuer burn + supply reversal) ->
  supply_changes growth row (shop mints are NOT supply-neutral) ->
  BRIX destination-locked sell offer with on-ledger Expiration ->
  XUMM accept (signer must match buyer) ->
  settle via run_deposit (burn back into Closet) ->
  shop_count increment (pricing feedback).
The expiry/settlement sweep in lfg_service owns retry of anything that
stalls after "accepted"; this module never blind-retries on-chain writes.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from xrpl.utils import xrp_to_drops

from lfg_core import config, memos, rarity, shop_store
from lfg_core import trait_token as tt
from lfg_core.economy_flow import DepositSession, EconomyDeps, run_deposit

log = logging.getLogger(__name__)

RIPPLE_EPOCH_OFFSET = 946_684_800

RUNNING = "running"
AWAITING_ACCEPT = "awaiting_accept"
SETTLING = "settling"
DONE = "done"
FAILED = "failed"

# Mirrors mint_flow/swap_flow/market_flow/economy_api's TERMINAL_STATES: a
# session in one of these states is finished and no longer blocks a new buy
# from the same wallet (lfg_service.app's concurrent-session guard, #217).
TERMINAL_STATES = {DONE, FAILED}

# Callable shapes injected via ShopDeps — kept loose (Callable[..., Awaitable])
# because keyword args differ per call site (mirrors xrpl_ops.mint_nft /
# create_nft_offer / burn_nft's real signatures).
MintFn = Callable[..., Awaitable[str | None]]
OfferFn = Callable[..., Awaitable[str | None]]
BurnFn = Callable[..., Awaitable[str | None]]
PayloadStatusFn = Callable[[str], Awaitable[dict[str, Any] | None]]
AcceptPayloadFn = Callable[..., Awaitable[dict[str, Any]]]


def ripple_expiration(now_unix: int, ttl: int) -> int:
    """Ripple-epoch Expiration value for an on-ledger offer valid `ttl`
    seconds from `now_unix` (unix time)."""
    return now_unix + ttl - RIPPLE_EPOCH_OFFSET


def brix_amount(value: int) -> dict[str, str]:
    """IssuedCurrencyAmount for a BRIX-denominated offer."""
    return {
        "currency": config.TOKEN_CURRENCY_HEX,
        "issuer": config.TOKEN_ISSUER_ADDRESS,
        "value": str(value),
    }


@dataclass
class ShopBuySession:
    buyer: str
    slot: str
    value: str
    price_brix: int
    # #238 XRP fallback: detected by the service (brix_payment.detect_payment_path)
    # before the session is created. On the XRP path price_xrp carries the
    # buffered AMM quote the offer is denominated in.
    pay_with: str = "BRIX"
    price_xrp: str | None = None
    platform: str = "discord"
    push_user_token: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = RUNNING
    error: str | None = None
    nft_id: str | None = None
    offer_index: str | None = None
    accept: dict[str, Any] | None = None

    def fail(self, msg: str) -> None:
        self.state = FAILED
        self.error = msg

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "buyer": self.buyer,
            "slot": self.slot,
            "value": self.value,
            "price_brix": self.price_brix,
            "pay_with": self.pay_with,
            "price_xrp": self.price_xrp,
            "state": self.state,
            "error": self.error,
            "nft_id": self.nft_id,
            "offer_index": self.offer_index,
            "accept": self.accept,
        }


@dataclass
class ShopDeps:
    conn: sqlite3.Connection  # economy onchain DB (trait_tokens/supply_changes/shop_orders)
    app_conn_factory: Callable[[], sqlite3.Connection]  # app DB for rarity/shop_count
    economy_deps: EconomyDeps
    mint_fn: MintFn
    offer_fn: OfferFn
    burn_fn: BurnFn
    payload_status_fn: PayloadStatusFn
    accept_payload_fn: AcceptPayloadFn
    now_ts_fn: Callable[[], int] = lambda: int(time.time())
    network: str = "testnet"
    # #238: best-effort post-settlement AMM buyback on the XRP path, called as
    # buy_and_burn_fn(brix_value, max_xrp=price_xrp) — the service wires
    # xrpl_ops.buy_and_burn with the BRIX currency/issuer baked in. None = skip
    # the call (the buyback_done flag is still set: single attempt).
    buy_and_burn_fn: Callable[..., Awaitable[str | None]] | None = None


def _record_supply_change(
    deps: ShopDeps, *, kind: str, delta: int, session: ShopBuySession, reason: str
) -> None:
    from lfg_core import economy_store as es

    es.record_supply_change(
        deps.conn,
        kind=kind,
        edition=None,
        body_value="",
        body_class="",
        trait_deltas={f"{session.slot}|{session.value}": delta},
        actor="shop",
        reason=reason,
    )


async def _revert_mint(deps: ShopDeps, session: ShopBuySession, nft_id: str) -> None:
    """Compensate a successful mint that could not be listed/logged: burn the
    token back and record a matching supply reversal row. If the burn itself
    fails (raises or returns falsy), leave the token minted and route to the
    admin-intervention message instead of silently losing the +1 supply row."""
    try:
        revert_hash = await deps.burn_fn(nft_id, "")
    except Exception:
        log.exception(f"Shop buy {session.id} revert burn crashed for nft_id={nft_id}")
        revert_hash = None
    if revert_hash:
        try:
            _record_supply_change(
                deps,
                kind="burn",
                delta=-1,
                session=session,
                reason=f"shop revert {session.id}",
            )
        except Exception:
            log.exception(
                f"Shop buy {session.id} revert burn succeeded (nft_id={nft_id}) but the "
                "supply reversal row failed to write — ledger and supply mirror are now "
                "out of sync; needs an admin audit note"
            )
        session.nft_id = None
        session.fail("failed to list your trait token for sale; the mint was reverted")
    else:
        session.fail(
            f"failed to list trait token {nft_id} for sale and the compensating burn "
            "failed too — an admin must burn it"
        )


async def run_buyback_if_due(
    deps: ShopDeps,
    *,
    session_id: str,
    pay_with: str | None,
    price_brix: int,
    price_xrp: str | None,
    buyback_done: int | None,
) -> None:
    """#238: after an XRP-path order settles, convert the collected XRP into
    BRIX through the AMM and burn it (`buy_and_burn_fn(price_brix,
    max_xrp=price_xrp)`), best-effort and silent — a failed buyback only logs
    (the XRP stays in the app wallet). `buyback_done` is flipped to 1 after
    the single attempt regardless of outcome, so the poll path and the sweep
    can both call this without double-firing. No-op on the BRIX path or when
    the flag is already set."""
    if pay_with != "XRP" or buyback_done:
        return
    if deps.buy_and_burn_fn is not None:
        try:
            await deps.buy_and_burn_fn(str(price_brix), max_xrp=price_xrp)
        except Exception:
            log.error(
                f"Shop buy {session_id} post-settlement buyback failed (order settled; "
                f"collected XRP stays in the app wallet): {traceback.format_exc()}"
            )
    try:
        shop_store.update_order(deps.conn, session_id, now_ts=deps.now_ts_fn(), buyback_done=1)
    except Exception:
        log.error(f"Shop buy {session_id} buyback_done flag write failed: {traceback.format_exc()}")


async def start_shop_buy(session: ShopBuySession, deps: ShopDeps) -> None:
    """Mint the trait token, record supply growth, create the BRIX
    destination-locked sell offer, and build the XUMM accept payload. On
    mint failure the session/order simply fail (nothing minted, nothing to
    revert). On offer failure after a successful mint, the mint is reverted
    (issuer burn + a compensating supply_changes row) before failing."""
    now_ts = deps.now_ts_fn()
    shop_store.create_order(
        deps.conn,
        session.id,
        session.buyer,
        session.slot,
        session.value,
        session.price_brix,
        now_ts,
        pay_with=session.pay_with,
        price_xrp=session.price_xrp,
    )
    try:
        econ = deps.economy_deps
        assert econ.trait_compose_fn is not None and econ.trait_upload_fn is not None
        image_url = await econ.trait_compose_fn(session.slot, session.value)
        meta_url = await econ.trait_upload_fn(
            tt.build_trait_metadata(session.slot, session.value, image_url)
        )

        nft_id = await deps.mint_fn(
            meta_url,
            config.TRAIT_TAXON,
            flags=config.TRAIT_NFT_FLAGS,
            action=memos.ACTION_SHOP_BUY,
            platform=memos.platform_for_surface(session.platform),
        )
        if not nft_id:
            session.fail("failed to mint your trait token; nothing was charged")
            shop_store.update_order(deps.conn, session.id, now_ts=deps.now_ts_fn(), status="failed")
            return
        session.nft_id = nft_id

        try:
            _record_supply_change(
                deps,
                kind="mint",
                delta=1,
                session=session,
                reason=f"shop purchase {session.id}",
            )
        except Exception:
            # The mint growth row didn't land — attempt to burn the token
            # back before failing so we don't strand a minted-but-unlogged
            # token, then re-raise into the outer handler.
            await _revert_mint(deps, session, nft_id)
            raise

        expiration = ripple_expiration(deps.now_ts_fn(), config.SHOP_OFFER_TTL_SECONDS)
        # #238: on the XRP fallback path the destination-locked offer is
        # denominated in XRP drops instead of the BRIX IssuedCurrencyAmount;
        # everything else about the offer is identical.
        if session.pay_with == "XRP":
            assert session.price_xrp is not None
            offer_amount: dict[str, str] | str = xrp_to_drops(Decimal(session.price_xrp))
        else:
            offer_amount = brix_amount(session.price_brix)
        try:
            offer_index = await deps.offer_fn(
                nft_id,
                session.buyer,
                amount=offer_amount,
                expiration=expiration,
                platform=memos.platform_for_surface(session.platform),
                action=memos.ACTION_SHOP_BUY,
            )
        except Exception:
            offer_index = None
        if not offer_index:
            await _revert_mint(deps, session, nft_id)
            shop_store.update_order(deps.conn, session.id, now_ts=deps.now_ts_fn(), status="failed")
            return
        session.offer_index = offer_index

        session.accept = await deps.accept_payload_fn(
            offer_index, user_token=session.push_user_token
        )
        shop_store.update_order(
            deps.conn,
            session.id,
            now_ts=deps.now_ts_fn(),
            status="pending_accept",
            nft_id=nft_id,
            offer_index=offer_index,
        )
        session.state = AWAITING_ACCEPT
    except Exception as e:
        logging.error(f"Shop buy {session.id} failed: {traceback.format_exc()}")
        if session.state != FAILED:
            session.fail(str(e))
        try:
            shop_store.update_order(deps.conn, session.id, now_ts=deps.now_ts_fn(), status="failed")
        except Exception:
            logging.error(
                f"Shop buy {session.id} order-fail write failed: {traceback.format_exc()}"
            )


async def advance_shop_buy(session: ShopBuySession, deps: ShopDeps) -> None:
    """Poll the XUMM accept payload; once signed, verify the signer matches
    the buyer (else fail signer_mismatch, leaving the order pending_accept
    for the expiry sweep) and settle the sale by depositing the token back
    into the buyer's Closet. Settlement failure leaves the order accepted
    (state 'settling') for the sweep to retry — it is NOT a session failure."""
    if session.state not in (AWAITING_ACCEPT, SETTLING):
        return
    if session.accept is None or session.nft_id is None:
        return
    status = await deps.payload_status_fn(session.accept["uuid"])
    if status is None or not status.get("signed"):
        return

    signer = status.get("account")
    if signer != session.buyer:
        session.fail("signer_mismatch")
        return

    shop_store.update_order(deps.conn, session.id, now_ts=deps.now_ts_fn(), status="accepted")

    dep_session = DepositSession(owner=session.buyer, nft_id=session.nft_id)
    try:
        await run_deposit(dep_session, deps.economy_deps)
    except Exception:
        logging.error(f"Shop buy {session.id} settlement raised: {traceback.format_exc()}")
        session.state = SETTLING
        return

    if dep_session.state != DONE:
        session.state = SETTLING
        return

    shop_store.update_order(deps.conn, session.id, now_ts=deps.now_ts_fn(), status="settled")
    order = shop_store.get_order(deps.conn, session.id)
    await run_buyback_if_due(
        deps,
        session_id=session.id,
        pay_with=session.pay_with,
        price_brix=session.price_brix,
        price_xrp=session.price_xrp,
        buyback_done=order.get("buyback_done") if order else 1,
    )
    try:
        app_conn = deps.app_conn_factory()
        try:
            rarity.increment_shop_count(app_conn, deps.network, session.slot, session.value)
        finally:
            app_conn.close()
    except Exception:
        log.warning(
            f"Shop buy {session.id} shop_count increment failed (order settled; "
            f"pricing feedback skipped): {traceback.format_exc()}"
        )
    session.state = DONE
