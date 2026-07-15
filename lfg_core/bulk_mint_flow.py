# Bulk mint (#215): a durable batch job. After one K x payment, a background
# task loops mint_flow.mint_one_unit K times, persisting after each unit so a
# restart resumes the remainder. Offers never expire, so acceptance is fully
# decoupled (Phase B / #218).
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from lfg_core import config, entitlement, memos, supply, xrpl_ops, xumm_ops

JOBS_DIR = os.getenv("BULK_MINT_JOBS_DIR", "bulk_mint_jobs")

AWAITING_PAYMENT = "awaiting_payment"
PAID = "paid"
FULFILLING = "fulfilling"
DONE = "done"
FAILED = "failed"
PAYMENT_TIMEOUT = "payment_timeout"
CANCELLED = "cancelled"
# FULFILLING is deliberately NOT terminal: the job must stay live in
# /api/mint/active so the client can re-attach, and so the restart sweep
# resumes it.
TERMINAL_STATES = {DONE, FAILED, PAYMENT_TIMEOUT, CANCELLED}

PENDING = "pending"
MINTED = "minted"
OFFERED = "offered"
UNIT_FAILED = "failed"


class CollectionFull(Exception):
    """No headroom under MAX_COLLECTION_SIZE."""


@dataclass
class Unit:
    index: int
    state: str = PENDING
    nft_number: int | None = None
    nft_id: str | None = None
    image_url: str | None = None
    offer_id: str | None = None
    error: str | None = None


class BulkMintJob:
    def __init__(
        self,
        discord_id: str,
        wallet_address: str,
        requested_qty: int,
        platform: str = "discord",
        push_user_token: str | None = None,
        return_url: dict[str, str] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.discord_id = discord_id
        self.wallet_address = wallet_address
        self.platform = platform
        self.push_user_token = push_user_token
        self.return_url = return_url
        self.requested_qty = requested_qty
        self.quantity = requested_qty
        self.network = config.XRPL_NETWORK
        self.created_at = time.time()
        self.paid_at: float | None = None
        self.state = AWAITING_PAYMENT
        self.error: str | None = None
        self.pay_with: str | None = None
        self.pay_amount: str | None = None
        self.unit_price: str | None = None
        self.payment_link: str | None = None
        self.payment_uuid: str | None = None
        self.entitlement: Any = None
        self.units: list[Unit] = []
        self.task: asyncio.Task[None] | None = None
        self._published = False

    def clamp_to_headroom(self) -> None:
        """Clamp quantity to min(requested, BULK_MINT_MAX, headroom). Raise
        CollectionFull if no headroom. Cap-exempt entitlements (burn) skip the
        headroom clamp (#220). Must run BEFORE prepare_payment so we never take
        payment for undeliverable mints."""
        cap_exempt = self.entitlement is not None and getattr(self.entitlement, "cap_exempt", False)
        q = min(self.requested_qty, config.BULK_MINT_MAX)
        if not cap_exempt:
            headroom = supply.remaining_headroom(self.network)
            if headroom <= 0:
                raise CollectionFull()
            q = min(q, headroom)
        self.quantity = q
        self.units = [Unit(index=i) for i in range(q)]
        if self.entitlement is None:
            self.entitlement = entitlement.PaymentEntitlement(quantity=q)

    def _payment_params(self) -> dict[str, Any]:
        if self.pay_with == "XRP":
            return {
                "destination": xrpl_ops.bot_wallet_address(),
                "value": self.pay_amount,
                "currency": "XRP",
                "issuer": None,
            }
        return {
            "destination": config.TOKEN_ISSUER_ADDRESS,
            "value": self.pay_amount,
            "currency": config.TOKEN_CURRENCY_HEX,
            "issuer": config.TOKEN_ISSUER_ADDRESS,
        }

    async def prepare_payment(self) -> None:
        """Detect LFGO vs XRP path (same rule as single mint) at K x price and
        build the XUMM payment payload."""
        balance = await xrpl_ops.get_trustline_balance(
            self.wallet_address, config.TOKEN_CURRENCY_HEX, config.TOKEN_ISSUER_ADDRESS
        )
        total_lfgo = Decimal(config.MINT_PRICE_LFGO) * self.quantity
        if balance is not None and balance >= total_lfgo:
            self.pay_with, self.unit_price = "LFGO", config.MINT_PRICE_LFGO
            self.pay_amount = str(total_lfgo)
        else:
            self.pay_with, self.unit_price = "XRP", config.MINT_PRICE_XRP
            self.pay_amount = str(Decimal(config.MINT_PRICE_XRP) * self.quantity)
        p = self._payment_params()
        payload = await xumm_ops.create_payment_payload(
            p["destination"],
            value=p["value"],
            currency=p["currency"],
            issuer=p["issuer"],
            return_url=self.return_url,
            user_token=self.push_user_token,
            platform=memos.platform_for_surface(self.platform),
        )
        if payload:
            self.payment_link = payload["xumm_url"]
            self.payment_uuid = payload.get("uuid")

    def cancel(self) -> bool:
        """Legal only while awaiting payment (once paid, fulfillment must
        complete). Synchronous state guard, same discipline as MintSession."""
        if self.state != AWAITING_PAYMENT:
            return False
        self.state = CANCELLED
        if self.task is not None:
            self.task.cancel()
        return True

    def mark_published(self) -> None:
        self._published = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "requested_qty": self.requested_qty,
            "quantity": self.quantity,
            "pay_with": self.pay_with,
            "pay_amount": self.pay_amount,
            "payment_link": self.payment_link,
            "network": self.network,
            "units": [asdict(u) for u in self.units],
            "minted": sum(1 for u in self.units if u.state in (MINTED, OFFERED)),
            "offered": sum(1 for u in self.units if u.state == OFFERED),
        }

    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "discord_id": self.discord_id,
            "wallet_address": self.wallet_address,
            "platform": self.platform,
            "push_user_token": self.push_user_token,
            "return_url": self.return_url,
            "requested_qty": self.requested_qty,
            "quantity": self.quantity,
            "network": self.network,
            "created_at": self.created_at,
            "paid_at": self.paid_at,
            "state": self.state,
            "error": self.error,
            "pay_with": self.pay_with,
            "pay_amount": self.pay_amount,
            "unit_price": self.unit_price,
            "payment_uuid": self.payment_uuid,
            "payment_link": self.payment_link,
            "entitlement": self.entitlement.to_dict() if self.entitlement else None,
            "units": [asdict(u) for u in self.units],
        }

    @classmethod
    def from_serialized(cls, d: dict[str, Any]) -> BulkMintJob:
        j = cls(
            d["discord_id"],
            d["wallet_address"],
            d["requested_qty"],
            platform=d["platform"],
            push_user_token=d.get("push_user_token"),
            return_url=d.get("return_url"),
        )
        j.id = d["id"]
        j.quantity = d["quantity"]
        j.network = d["network"]
        j.created_at = d["created_at"]
        j.paid_at = d.get("paid_at")
        j.state = d["state"]
        j.error = d.get("error")
        j.pay_with = d.get("pay_with")
        j.pay_amount = d.get("pay_amount")
        j.unit_price = d.get("unit_price")
        j.payment_uuid = d.get("payment_uuid")
        j.payment_link = d.get("payment_link")
        j.entitlement = entitlement.from_dict(d["entitlement"]) if d.get("entitlement") else None
        j.units = [Unit(**u) for u in d["units"]]
        return j


def _record_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def persist(job: BulkMintJob) -> None:
    """Atomically write the job's full reconstruction record."""
    os.makedirs(JOBS_DIR, exist_ok=True)
    data = job.serialize()
    fd, tmp = tempfile.mkstemp(dir=JOBS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _record_path(job.id))
    except Exception:
        logging.error("failed to persist bulk job %s", job.id)
        if os.path.exists(tmp):
            os.remove(tmp)


def delete_record(job_id: str) -> None:
    try:
        os.remove(_record_path(job_id))
    except FileNotFoundError:
        pass


def load_all_resumable() -> list[BulkMintJob]:
    out: list[BulkMintJob] = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for name in os.listdir(JOBS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, name)) as f:
                data = json.load(f)
            if data.get("state") in (PAID, FULFILLING):
                out.append(BulkMintJob.from_serialized(data))
        except Exception:
            logging.error("skipping unreadable bulk job record %s", name)
    return out
