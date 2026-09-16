"""Fakes shared by the Closet Market flow tests (#443)."""

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from lfg_core import closet_market_flow as cmf
from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_store as es
from lfg_core import market_ops
from lfg_core.xrpl_ops import TxOutcome

APP = "rAppWallet"
SELLER, BUYER, OTHER = "rSeller", "rBuyer", "rOther"
CANCEL_AFTER = 800_000_000  # ripple epoch


def make_db(path, seller_count=1):
    c = sqlite3.connect(path)
    es.init_economy_schema(c)
    for owner in (SELLER, BUYER, OTHER):
        es.set_closet_token(c, owner, f"C-{owner}", "00", status=ct.ACTIVE)
    es.set_closet_contents(c, SELLER, [("Head", "Crown", seller_count)], [])
    c.commit()
    c.close()


def conn(path):
    c = sqlite3.connect(path)
    es.init_economy_schema(c)
    return c


@dataclass
class Fakes:
    payload_status: dict = field(default_factory=dict)
    txs: dict = field(default_factory=dict)
    escrows: dict = field(default_factory=dict)
    finish_outcomes: list = field(default_factory=list)
    cancel_outcomes: list = field(default_factory=list)
    payment_outcomes: list = field(default_factory=list)
    found: dict = field(default_factory=dict)
    found_invoices: dict = field(default_factory=dict)  # invoice_id -> [account_tx entry]
    escrows_by_condition: dict = field(default_factory=dict)  # (owner, condition) -> node
    lookup_error: Exception | None = None  # raised by both reconciliation lookups
    invoice_lookups: list = field(default_factory=list)
    escrow_lookups: list = field(default_factory=list)
    # require_ledger passed to each reconciliation lookup, in call order
    invoice_requires: list = field(default_factory=list)
    escrow_requires: list = field(default_factory=list)
    # Highest ledger the reconciliation lookups can prove they cover; None =
    # always covered. Below a lookup's require_ledger the lookup raises.
    lookup_coverage: int | None = None
    ledger_index: int | None = 100
    mirror_error: Exception | None = None
    finishes: list = field(default_factory=list)
    cancels: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    mirrored: list = field(default_factory=list)

    async def payload_status_fn(self, uuid):
        return self.payload_status.get(uuid)

    async def get_tx_fn(self, tx_hash):
        return self.txs.get(tx_hash, {"validated": False})

    async def get_escrow_fn(self, owner, seq):
        return self.escrows.get((owner, seq))

    async def escrow_finish_fn(
        self, owner, seq, condition, fulfillment, tag, *, max_last_ledger_seq=None
    ):
        self.finishes.append((owner, seq, condition, fulfillment, tag))
        state = self.finish_outcomes.pop(0) if self.finish_outcomes else "confirmed"
        if state == "crash":
            # Simulates the tx landing on-ledger and the process dying before
            # it can record the outcome: the escrow is gone, but nothing here
            # ever gets to persist a hash.
            self.escrows.pop((owner, seq), None)
            raise RuntimeError("simulated crash after the tx landed")
        if state == "confirmed":
            self.escrows.pop((owner, seq), None)
        return TxOutcome(state, f"FIN{len(self.finishes)}" if state == "confirmed" else None, 140)

    async def escrow_cancel_fn(self, owner, seq, tag, *, max_last_ledger_seq=None):
        self.cancels.append((owner, seq, tag))
        state = self.cancel_outcomes.pop(0) if self.cancel_outcomes else "confirmed"
        if state == "crash":
            self.escrows.pop((owner, seq), None)
            raise RuntimeError("simulated crash after the tx landed")
        if state == "confirmed":
            self.escrows.pop((owner, seq), None)
        return TxOutcome(state, "CAN" if state == "confirmed" else None, 140)

    async def payment_fn(self, destination, value, tag, action, *, max_last_ledger_seq=None):
        self.payments.append((destination, value, tag, action))
        state = self.payment_outcomes.pop(0) if self.payment_outcomes else "confirmed"
        if state == "crash":
            raise RuntimeError("simulated crash after the tx landed")
        return TxOutcome(state, f"PAY{len(self.payments)}" if state == "confirmed" else None, 140)

    async def find_txs_fn(self, tag, lls):
        return self.found.get(tag, [])

    def _check_coverage(self, require_ledger):
        if self.lookup_error is not None:
            raise self.lookup_error
        if (
            require_ledger is not None
            and self.lookup_coverage is not None
            and self.lookup_coverage < require_ledger
        ):
            raise RuntimeError("lookup does not cover the required ledger")

    async def find_invoice_payments_fn(self, invoice_id, min_ledger, *, require_ledger=None):
        self.invoice_lookups.append((invoice_id, min_ledger))
        self.invoice_requires.append(require_ledger)
        self._check_coverage(require_ledger)
        return list(self.found_invoices.get(invoice_id, []))

    async def find_escrow_by_condition_fn(self, owner, condition, *, require_ledger=None):
        self.escrow_lookups.append((owner, condition))
        self.escrow_requires.append(require_ledger)
        self._check_coverage(require_ledger)
        return self.escrows_by_condition.get((owner, condition))

    async def ledger_index_fn(self):
        return self.ledger_index

    async def mirror_fn(self, c, owner):
        if self.mirror_error is not None:
            raise self.mirror_error
        self.mirrored.append(owner)


def deps(path, f, tmp_path, *, fee_bps=0, now=1_000_000.0):
    return cmf.ClosetMarketDeps(
        conn_factory=lambda: conn(path),
        app_account=APP,
        fee_bps=fee_bps,
        payload_status_fn=f.payload_status_fn,
        get_tx_fn=f.get_tx_fn,
        get_escrow_fn=f.get_escrow_fn,
        escrow_finish_fn=f.escrow_finish_fn,
        escrow_cancel_fn=f.escrow_cancel_fn,
        payment_fn=f.payment_fn,
        find_txs_fn=f.find_txs_fn,
        ledger_index_fn=f.ledger_index_fn,
        mirror_fn=f.mirror_fn,
        unseal_fn=lambda blob: "FULFILL",
        find_invoice_payments_fn=f.find_invoice_payments_fn,
        find_escrow_by_condition_fn=f.find_escrow_by_condition_fn,
        records_dir=str(tmp_path / "records"),
        now_fn=lambda: now,
    )


_UNSET: Any = object()


def pending_bid(path, *, owner=BUYER, price="10", user_lls=None, payload_uuid=_UNSET):
    c = conn(path)
    order = cms.create_pending_bid(
        c,
        owner=owner,
        slot="Head",
        value="Crown",
        price_brix=price,
        platform=None,
        condition="COND",
        fulfillment_enc="SEALED",
        cancel_after=CANCEL_AFTER,
        payload_uuid=f"U-{owner}" if payload_uuid is _UNSET else payload_uuid,
        xumm_url=None if payload_uuid is None else "x",
        qr_url=None if payload_uuid is None else "q",
        push=None,
        user_lls=user_lls,
    )
    c.close()
    return order


def open_bid(path, f, *, owner=BUYER, price="10", seq=5):
    order = pending_bid(path, owner=owner, price=price)
    c = conn(path)
    order = cms.mark_bid_open(
        c, order["id"], escrow_tx_hash=f"ESC-{order['id']}", escrow_owner_seq=seq
    )
    c.close()
    f.escrows[(owner, seq)] = escrow_node(owner, price)
    return order


def escrow_node(owner, price, condition="COND", **extra):
    return {
        "Account": owner,
        "Destination": APP,
        "Amount": market_ops.brix_amount_dict(price),
        "Condition": condition,
        **extra,
    }


def escrow_create_tx(owner, price, *, seq=7, condition="COND", result="tesSUCCESS", amount=None):
    return {
        "validated": True,
        "hash": "ECH",
        "meta": {"TransactionResult": result},
        "tx_json": {
            "TransactionType": "EscrowCreate",
            "Account": owner,
            "Destination": APP,
            "Amount": amount or market_ops.brix_amount_dict(price),
            "Condition": condition,
            "CancelAfter": CANCEL_AFTER,
            "Sequence": seq,
        },
    }


def landed(tx_hash="LANDED", result="tesSUCCESS"):
    return {
        "validated": True,
        "hash": tx_hash,
        "meta": {"TransactionResult": result},
        "tx_json": {"Account": APP},
    }


def run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def order(path, order_id) -> dict[str, Any]:
    c = conn(path)
    try:
        return cms.get_order(c, order_id)
    finally:
        c.close()


def fill(path, fill_id) -> dict[str, Any]:
    c = conn(path)
    try:
        return cms.get_fill(c, fill_id)
    finally:
        c.close()
