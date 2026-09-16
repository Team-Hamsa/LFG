"""Pure fee-cover rules — who gets a promise, how much, and what a validated
accept refunds. No I/O: callers pass rows, the tx, and small callables.

Spec: docs/superpowers/specs/2026-09-14-marketplace-fee-cover-design.md.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from xrpl.core.addresscodec import encode_classic_address

from lfg_core import brokers, fee_cover_store
from lfg_core.fee_cover_store import BPS, Campaign, RefundDecision

# A refund is always strictly below the royalty received on the same sale, so
# no pattern of trades — including self-dealing through a fake broker account
# that sets NFTokenBrokerFee to the whole spread — can make LFG a net payer.
# A code constant on purpose: never an admin knob.
ROYALTY_CEILING_BPS = 5000

RECORDED_PROMISE_DECLINES = frozenset(
    {"system_wallet", "below_clearing", "below_min_bid", "budget_exhausted", "wallet_cap"}
)

_LSF_SELL_NFTOKEN = 0x00000001


class LinkageUnavailable(Exception):
    """The buyer/seller linkage lookup could not answer, so self-dealing can be
    neither proved nor ruled out. Raised by the `linked` callable and allowed to
    propagate out of `compute_refund`: an unknown link DEFERS the settlement
    (the promise stays open and the sweep retries) rather than resolving to
    "unrelated", which would pay a refund on a sale the rule might forbid."""


@dataclass(frozen=True)
class ExternalListing:
    offer_index: str
    seller: str
    ask_drops: int
    broker: str
    broker_rate: float

    @property
    def clearing_drops(self) -> int:
        return brokers.clearing_drops(self.ask_drops, self.broker_rate)


def external_listing_for(
    rows: Sequence[Mapping[str, Any]], owner: str, nft_id: str
) -> ExternalListing | None:
    """The cheapest live listing by `owner` whose destination is an allowlisted
    broker with a MEASURED rate — the listing a Buy-now bid clears against."""
    best: ExternalListing | None = None
    for row in rows:
        destination = row.get("destination")
        drops = row.get("amount_drops")
        if not destination or row.get("seller") != owner:
            continue
        if not isinstance(drops, int) or isinstance(drops, bool) or drops <= 0:
            continue
        resolved = brokers.resolve(str(destination), nft_id)
        if resolved is None or resolved.get("broker_rate") is None:
            continue
        candidate = ExternalListing(
            str(row["offer_index"]),
            owner,
            drops,
            str(destination),
            float(resolved["broker_rate"]),
        )
        if best is None or candidate.ask_drops < best.ask_drops:
            best = candidate
    return best


def promise_drops(bid_drops: int, broker_rate: float, coverage_bps: int) -> int:
    """The most this bid can be refunded: the broker's fee on it (rounded UP,
    exactly as cafe computes it) times coverage.

    Defined in the store so `record_promise` can re-derive it under its own
    write lock — the promise that is WRITTEN is always sized on the campaign as
    it stands at that moment. This is the same function, re-exported for the
    advisory quote."""
    return fee_cover_store.promise_drops(bid_drops, broker_rate, coverage_bps)


def promise_decline_reason(
    *,
    campaign: Campaign | None,
    listing: ExternalListing | None,
    bid_drops: int,
    bidder: str,
    system_wallets: Collection[str],
) -> str | None:
    """Pure promise eligibility. Budget and wallet-cap headroom are decided in
    the store, inside its write lock."""
    if campaign is None:
        return "campaign_inactive"
    if listing is None:
        return "not_external_listing"
    if bidder in system_wallets:
        return "system_wallet"
    if bid_drops < listing.clearing_drops:
        return "below_clearing"
    if bid_drops < campaign.min_bid_drops:
        return "below_min_bid"
    return None


def nft_issuer(nft_id: str) -> str:
    """The issuer AccountID encoded in bytes 4..24 of an NFTokenID."""
    return str(encode_classic_address(bytes.fromhex(nft_id[8:48])))


def _deleted_offers(meta: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    sell: dict[str, Any] | None = None
    buy: dict[str, Any] | None = None
    for node in meta.get("AffectedNodes") or []:
        deleted = node.get("DeletedNode") if isinstance(node, Mapping) else None
        if not isinstance(deleted, Mapping) or deleted.get("LedgerEntryType") != "NFTokenOffer":
            continue
        fields = dict(deleted.get("FinalFields") or {})
        fields["LedgerIndex"] = deleted.get("LedgerIndex")
        if int(fields.get("Flags") or 0) & _LSF_SELL_NFTOKEN:
            sell = fields
        else:
            buy = fields
    return sell, buy


def _balance_delta(meta: Mapping[str, Any], account: str) -> int | None:
    for node in meta.get("AffectedNodes") or []:
        modified = node.get("ModifiedNode") if isinstance(node, Mapping) else None
        if not isinstance(modified, Mapping) or modified.get("LedgerEntryType") != "AccountRoot":
            continue
        final = modified.get("FinalFields") or {}
        if final.get("Account") != account:
            continue
        previous = modified.get("PreviousFields") or {}
        try:
            return int(final["Balance"]) - int(previous["Balance"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def compute_refund(
    tx: Mapping[str, Any],
    *,
    promise: Mapping[str, Any],
    payer: str,
    broker_rate_for: Callable[[str], float | None],
    linked: Callable[[str, str], bool],
) -> RefundDecision:
    """What a validated accept refunds against its promise — derived from the
    transaction and its metadata, never from any listed price.

    `linked` may raise `LinkageUnavailable`, which propagates: an unanswerable
    linkage lookup must defer the settlement, never resolve to "unrelated"."""
    tx_hash = str(tx.get("hash") or "")
    raw_meta = tx.get("meta")
    meta: Mapping[str, Any] = raw_meta if isinstance(raw_meta, Mapping) else {}
    broker = tx.get("Account") if isinstance(tx.get("Account"), str) else None
    sell, buy = _deleted_offers(meta)
    seller = sell.get("Owner") if sell else None
    bidder = str(promise["bidder"])

    def decline(
        reason: str, *, fee: int | None = None, royalty: int | None = None
    ) -> RefundDecision:
        return RefundDecision(tx_hash, seller, broker, fee, royalty, 0, reason)

    brokered = (
        tx.get("validated") is True
        and meta.get("TransactionResult") == "tesSUCCESS"
        and tx.get("TransactionType") == "NFTokenAcceptOffer"
        and bool(tx.get("NFTokenSellOffer"))
        and tx.get("NFTokenBuyOffer") == promise["offer_index"]
        and sell is not None
        and buy is not None
        and buy.get("Owner") == bidder
        and broker is not None
        and broker_rate_for(broker) is not None
    )
    if not brokered:
        return decline("not_broker_settled")
    raw_fee = tx.get("NFTokenBrokerFee")
    if not isinstance(raw_fee, str) or not raw_fee.isdigit():
        return decline("fee_unobserved")
    fee = int(raw_fee)
    issuer = nft_issuer(str(promise["nft_id"]))
    if issuer in (seller, bidder):
        return decline("issuer_party", fee=fee)
    royalty = _balance_delta(meta, issuer)
    if issuer != payer or royalty is None or royalty <= 0:
        return decline("royalty_unobserved", fee=fee, royalty=royalty)
    if seller and linked(bidder, str(seller)):
        return decline("linked_counterparty", fee=fee, royalty=royalty)
    refund = min(
        int(promise["promised_drops"]),
        fee * int(promise["coverage_bps"]) // BPS,
        royalty * ROYALTY_CEILING_BPS // BPS,
    )
    if refund < 1:
        return decline("zero_refund", fee=fee, royalty=royalty)
    return RefundDecision(tx_hash, seller, broker, fee, royalty, refund, None)


def audit_violations(conn: sqlite3.Connection, network: str) -> list[str]:
    """Every committed refund must stay within its promise, its observed fee ×
    coverage, and ROYALTY_CEILING_BPS of its observed royalty; no campaign may
    pay more than its budget. Empty list = clean."""
    problems: list[str] = []
    for accept, refund, fee, royalty, promised, coverage in conn.execute(
        "SELECT r.accept_tx_hash, r.refund_drops, r.observed_fee_drops, r.observed_royalty_drops,"
        " p.promised_drops, p.coverage_bps FROM fee_cover_refunds r"
        " JOIN fee_cover_promises p ON p.offer_index = r.offer_index"
        " WHERE r.network = ? AND r.state IN ('owed','submitted','confirmed')",
        (network,),
    ):
        if refund > promised:
            problems.append(f"{accept}: refund {refund} exceeds promise {promised}")
        if fee is None or refund > fee * coverage // BPS:
            problems.append(f"{accept}: refund {refund} exceeds observed fee x coverage")
        if royalty is None or refund > royalty * ROYALTY_CEILING_BPS // BPS:
            problems.append(
                f"{accept}: refund {refund} exceeds {ROYALTY_CEILING_BPS // 100}% of observed royalty"
            )
    for campaign_id, budget, paid in conn.execute(
        "SELECT c.id, c.budget_drops, COALESCE(SUM(r.refund_drops), 0) FROM fee_cover_campaigns c"
        " LEFT JOIN fee_cover_refunds r ON r.campaign_id = c.id AND r.state = 'confirmed'"
        " WHERE c.network = ? GROUP BY c.id",
        (network,),
    ):
        if paid > budget:
            problems.append(f"campaign {campaign_id}: confirmed {paid} exceeds budget {budget}")
    return problems
