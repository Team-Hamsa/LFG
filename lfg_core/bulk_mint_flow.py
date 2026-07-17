# Bulk mint (#215): a durable batch job. After one K x payment, a background
# task loops mint_flow.mint_one_unit K times, persisting after each unit so a
# restart resumes the remainder. The record is durable from the moment
# prepare_payment succeeds (#228): awaiting_payment/paid/fulfilling records
# all resume on restart, and an awaiting_payment resume only re-enters the
# ledger watch — the payment window stays anchored to created_at (never a
# fresh window) and the payload is never re-requested. Offers never expire,
# so acceptance is fully decoupled (Phase B / #218).
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

from lfg_core import (
    config,
    db_path,
    entitlement,
    memos,
    mint_credits,
    mint_flow,
    payment_ledger,
    supply,
    xrpl_ops,
    xumm_ops,
)

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
        # Runtime durability health (#228), never serialized: a record on disk
        # is by definition a successful write, so this is meaningful only for
        # the live in-memory job. True while the most recent persist() failed.
        self.persist_failed = False
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

    @property
    def payment_claimant(self) -> str:
        """Exact claimant tag stamped on this job's consumed-payment ledger
        row (#228) — lets a resumed job recognise a payment its pre-crash
        process already claimed, and cancel() refuse to drop a claimed one."""
        return f"bulk:{self.id}"

    def cancel(self) -> bool:
        """Legal only while awaiting payment (once paid, fulfillment must
        complete). Synchronous state guard, same discipline as MintSession."""
        if self.state != AWAITING_PAYMENT:
            return False
        # The payment can already be durably claimed while run_bulk_mint_job
        # is suspended between payment_ledger.try_consume committing and PAID
        # landing on this object (#228): state still reads AWAITING_PAYMENT
        # but the money is taken. Refuse the cancel — the in-flight watch
        # will surface PAID and fulfillment must run.
        # is not False: True means the money is taken (fulfillment must
        # run); None means the ledger read failed, so "unpaid" is unprovable
        # — refuse rather than risk deleting a paid job (user can retry).
        if payment_ledger.find_claimed(self.payment_claimant) is not False:
            return False
        self.state = CANCELLED
        # In-memory teardown first: delete_record never raises but CAN fail
        # on a degraded disk, and the payment watch must die regardless.
        if self.task is not None:
            self.task.cancel()
        # AWAITING_PAYMENT records are durable (#228) and resumable — drop the
        # file so a restart can't resurrect a job the user backed out of. If
        # the disk refuses the delete, rewrite the record as a cancelled
        # tombstone instead (terminal states are skipped by
        # load_all_resumable); persist never raises, so this is best-effort.
        if not delete_record(self.id) and not persist(self):
            # Disk can neither unlink nor write: the stale awaiting_payment
            # record survives and a restart will resurrect its payment watch.
            # Safe direction — the watch re-expires via the created_at TTL,
            # and a payment that actually landed is honoured, not lost — but
            # scream so the degraded disk gets fixed.
            logging.critical(
                f"bulk job {self.id}: cancel could neither delete nor tombstone "
                "its record; a restart will resurrect the payment watch"
            )
        return True

    def mark_published(self) -> None:
        self._published = True

    def to_dict(self) -> dict[str, Any]:
        """Client-facing shape (/api/mint/bulk, status poll, /active).

        Contract: `payment_link` MAY be null while state == AWAITING_PAYMENT —
        the start handler registers the job (making it visible to /active)
        BEFORE the XUMM payload build finishes, so null means "still
        preparing, keep polling", not an error. `persist_failed` flags
        degraded durability: the in-memory job is authoritative and keeps
        running; the flag clears on the next successful full-record persist.
        A unit MAY be `offered` with a null `offer_id`: its gift offer was
        already accepted (delivered while the service was down), so there is
        no offer left to accept — clients should treat it as claimed.
        """
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
            "persist_failed": self.persist_failed,
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


def persist(job: BulkMintJob) -> bool:
    """Atomically write the job's full reconstruction record. Returns False —
    NEVER raises — on failure (#228): a persist failure bubbling out of
    _on_mint would land inside the mint retry loop (re-mint risk), and one on
    the PAID write would abort fulfillment of a payment already taken. The
    job continues on in-memory state, flagged `persist_failed` (surfaced by
    to_dict) until a later persist succeeds and clears it — each write is a
    full-record rewrite, so any success restores complete durability."""
    tmp: str | None = None
    try:
        os.makedirs(JOBS_DIR, exist_ok=True)
        data = job.serialize()
        fd, tmp = tempfile.mkstemp(dir=JOBS_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _record_path(job.id))
    except Exception as e:
        logging.error("failed to persist bulk job %s (state=%s): %s", job.id, job.state, e)
        job.persist_failed = True
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False
    job.persist_failed = False
    return True


def delete_record(job_id: str) -> bool:
    """Remove a job's durable record. Same never-raise contract as persist()
    (#228): on the degraded-disk class persist_failed exists for, a raise
    mid-cancel would leave a CANCELLED job with a live payment watch and a
    resurrectable record. Returns False on a real deletion failure (missing
    file is success — nothing to resurrect)."""
    try:
        os.remove(_record_path(job_id))
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.error("failed to delete bulk job record %s: %s", job_id, e)
        return False
    return True


_UNIT_MAX_ATTEMPTS = 3

# Retry budget for a failed persist before fulfillment pauses (#228): once
# the on-disk record goes stale, every NEW unit started would be re-minted by
# a crash+restart resume, so we stop widening the exposure instead.
_PERSIST_RETRY_ATTEMPTS = 3
_PERSIST_RETRY_DELAY_SECONDS = 0.5


async def _retry_persist(job: BulkMintJob) -> bool:
    """Re-attempt a failed persist with a short backoff. True once a write
    lands (any success is a full-record rewrite, restoring durability)."""
    for _ in range(_PERSIST_RETRY_ATTEMPTS):
        await asyncio.sleep(_PERSIST_RETRY_DELAY_SECONDS)
        if persist(job):
            return True
    return False


# Wait budget for resuming an AWAITING_PAYMENT record whose payment window
# (created_at + PAYMENT_TIMEOUT_SECONDS) already elapsed: long enough for
# wait_for_payment's not_before-bounded backfill check to honour a payment
# that landed before the crash, never a fresh multi-minute wait.
_EXPIRED_PAYMENT_GRACE_SECONDS = 30


async def _ensure_offer(job: BulkMintJob, unit: Unit) -> None:
    """Re-offer-only path for a unit that already minted (unit.nft_id set)
    but never reached OFFERED — the crash window between the on-chain mint
    and the offer/XUMM steps landing. NEVER mints; only (re-)creates the sell
    offer for the existing nft_id. On success -> OFFERED. On failure, leave
    the unit MINTED with .error set so a later resume/backfill can retry the
    offer again without ever re-minting.

    Idempotent (#227): in the crash window where the original offer DID land
    on-chain but OFFERED was never persisted, creating again would leave two
    live offers for one unit — so an existing offer is adopted first. The
    match is exactly what create_nft_offer emits (our wallet as owner, the
    buyer as Destination, amount "0", no Expiration); anything looser risks
    adopting a foreign offer, and market_ops.verify_sell_offer is NOT
    reusable here because it rejects any Destination-carrying offer — which
    the gift offer always is. A lookup RPC failure is INDETERMINATE — a live
    offer may be hiding behind the blip, so creating blind could duplicate
    it; the unit stays MINTED and a later resume retries.

    The other half of the #227 window is the offer landing AND the buyer
    accepting it while we were down: the accept consumed the offer object, so
    there is nothing to adopt, and creating again can only tec-fail forever
    (we no longer own the token) — wedging the job FULFILLING and the user's
    bulk slot. A destination-locked amount-0 offer can only be taken by the
    destination, so current owner == buyer is proof of delivery: mark the
    unit OFFERED. Fail closed on an indeterminate owner lookup (nft_info
    returns None) — fall through to create, never mark an undelivered unit
    delivered on a transient clio blip."""
    assert unit.nft_id is not None, "_ensure_offer requires an already-minted unit"
    try:
        offers = await xrpl_ops.get_nft_sell_offers(unit.nft_id, raise_on_error=True)
    except Exception as e:
        unit.state = MINTED
        unit.error = f"offer lookup failed: {e}"
        return
    for offer in offers:
        if (
            offer.get("owner") == xrpl_ops.bot_wallet_address()
            and offer.get("destination") == job.wallet_address
            and offer.get("amount") == "0"
            and offer.get("expiration") is None
            and offer.get("offer_index")
        ):
            unit.offer_id = offer["offer_index"]
            unit.state = OFFERED
            unit.error = None
            return
    info = await xrpl_ops.nft_info(unit.nft_id)
    if info is not None and info.get("owner") == job.wallet_address:
        # Gift offer already accepted (see docstring): delivered. offer_id
        # stays None — the accept consumed the offer object, so there is
        # nothing left to accept (to_dict documents offered+null offer_id).
        unit.state = OFFERED
        unit.error = None
        return
    try:
        offer_id = await xrpl_ops.create_nft_offer(
            unit.nft_id,
            job.wallet_address,
            platform=memos.platform_for_surface(job.platform),
        )
    except Exception as e:
        unit.state = MINTED
        unit.error = str(e)
        return
    if not offer_id:
        unit.state = MINTED
        unit.error = "offer creation failed"
        return
    unit.offer_id = offer_id
    unit.state = OFFERED


async def _fulfill_unit(job: BulkMintJob, unit: Unit) -> None:
    """Mint+offer one unit. Cap re-check first (a concurrent job may have
    consumed the tail); a cap-hit or exhausted unit becomes a mint credit
    rather than a loss. Cap-exempt (burn) entitlements skip the re-check.

    `on_mint` fires the instant mint_one_unit confirms the mint on-chain, so
    the unit is persisted as MINTED before the offer/XUMM steps run — closing
    the resume double-mint window (#215): a crash between mint and offer now
    resumes via _ensure_offer (re-offer only), never a re-mint."""
    cap_exempt = job.entitlement is not None and getattr(job.entitlement, "cap_exempt", False)

    async def _on_mint(nft_number: int, nft_id: str, image_url: str | None) -> None:
        unit.nft_number = nft_number
        unit.nft_id = nft_id
        unit.image_url = image_url
        unit.state = MINTED
        if not persist(job):
            # This is THE write the anti-double-mint invariant depends on: a
            # restart from the stale record re-mints this unit. Escalate
            # loudly with the nft_id so an operator can reconcile manually
            # even if the disk record never recovers (#228).
            logging.critical(
                "MINTED persist failed for bulk job %s unit %d nft_id %s — "
                "on-disk record is stale; double-mint risk if the process "
                "restarts before a later persist succeeds",
                job.id,
                unit.index,
                nft_id,
            )

    for _ in range(_UNIT_MAX_ATTEMPTS):
        if not cap_exempt and supply.current_supply(job.network) >= config.MAX_COLLECTION_SIZE:
            break  # cap hit -> credit below
        nft_number = await mint_flow._allocate_nft_number()
        res = await mint_flow.mint_one_unit(
            discord_id=job.discord_id,
            wallet_address=job.wallet_address,
            platform=job.platform,
            push_user_token=job.push_user_token,
            return_url=job.return_url,
            nft_number=nft_number,
            session_tag=f"{job.id}:{unit.index}",
            on_mint=_on_mint,
        )
        if res.nft_id:
            unit.nft_id = res.nft_id
            unit.nft_number = res.nft_number
            unit.image_url = res.image_url
            if res.offer_id:
                unit.offer_id = res.offer_id
                unit.state = OFFERED
            else:
                # minted but offer failed: NFT exists, do NOT re-mint. Leave
                # MINTED (not UNIT_FAILED) so a resume re-offers instead of
                # treating this as a dead/credited unit.
                unit.state = MINTED
                unit.error = res.error or "offer creation failed"
            return
        unit.error = res.error  # transient mint failure: retry
    # Never minted after retries (or cap-hit): durable credit, no money lost.
    unit.state = UNIT_FAILED
    mint_credits.add_credit(db_path.app_db_path(job.network), job.discord_id, job.network, 1)


async def run_bulk_mint_job(job: BulkMintJob) -> None:
    """Drive a bulk job to terminal state. Background task / resume entrypoint."""
    try:
        # Entry guard: a terminal job must never fulfill. Closes the
        # cancel-during-prepare race (cancel() succeeds while the start
        # handler awaits prepare_payment because task is still None; without
        # this guard the CANCELLED job would fall past the AWAITING_PAYMENT
        # gate below and mint every unit with no payment ever confirmed) and
        # defends the startup sweep against any terminal record.
        if job.state in TERMINAL_STATES:
            return
        if job.state == AWAITING_PAYMENT:
            p = job._payment_params()
            assert job.pay_amount is not None, "prepare_payment must run before waiting"
            # The payment window is anchored to created_at, NOT to this call
            # (#228): AWAITING_PAYMENT records are durable and resumed on
            # restart, and a resume must neither grant a fresh full window
            # nor — for an already-expired record — sit in a multi-minute
            # wait. It must also never re-request payment: the payload was
            # built once in prepare_payment; this only re-watches the ledger,
            # and the grace floor keeps the backfill check (bounded by the
            # preserved not_before) so an UNCLAIMED payment that landed
            # before the crash is still honoured. A payment the pre-crash
            # process already claimed is invisible to the re-watch
            # (payment_ledger dedups by tx hash — the claim commits before
            # PAID can reach disk), so a miss is reconciled against the
            # ledger by this job's exact claimant tag before terminalizing.
            remaining = int(job.created_at + config.PAYMENT_TIMEOUT_SECONDS - time.time())
            paid = await xrpl_ops.wait_for_payment(
                destination=p["destination"],
                expected_sender=job.wallet_address,
                expected_amount=job.pay_amount,
                timeout_seconds=max(remaining, _EXPIRED_PAYMENT_GRACE_SECONDS),
                not_before=job.created_at - 10,
                currency=p["currency"],
                issuer=p["issuer"],
                claimant=job.payment_claimant,
            )
            if not paid:
                claimed = payment_ledger.find_claimed(job.payment_claimant)
                if claimed:
                    logging.info(
                        "bulk job %s: payment already claimed by a pre-crash "
                        "process — honouring it instead of timing out",
                        job.id,
                    )
                    paid = True
                elif claimed is None:
                    # Ledger read failed: neither "paid" nor "unpaid" is
                    # provable. Never terminalize on an indeterminate
                    # reconciliation — leave the record awaiting_payment so
                    # the next restart retries it.
                    logging.warning(
                        "bulk job %s: claim reconciliation indeterminate "
                        "(ledger read failed); leaving job resumable",
                        job.id,
                    )
                    persist(job)
                    return
            if not paid:
                job.state = PAYMENT_TIMEOUT
                persist(job)
                return
            job.paid_at = time.time()
            job.state = PAID
            persist(job)

        job.state = FULFILLING
        persist(job)
        processed_any = False
        for unit in job.units:
            if unit.state in (OFFERED, UNIT_FAILED):
                continue  # resume: skip already-processed units
            if processed_any and job.persist_failed and not await _retry_persist(job):
                # Durability degraded (#228): the on-disk record no longer
                # reflects the units just delivered, so every FURTHER unit
                # started now would also be re-minted by a crash+restart
                # resume. Park the job FULFILLING (non-terminal, resumable —
                # a restart or the next sweep picks it back up) instead of
                # widening the double-mint exposure past the unit already at
                # risk.
                job.error = "durability degraded: fulfillment paused"
                logging.critical(
                    "bulk job %s: persist still failing — pausing fulfillment "
                    "before unit %d to cap double-mint exposure",
                    job.id,
                    unit.index,
                )
                return
            if unit.state == MINTED:
                # Already minted (possibly in a prior process) — re-offer
                # only, never re-mint.
                await _ensure_offer(job, unit)
            else:
                await _fulfill_unit(job, unit)
            processed_any = True
            persist(job)

        # Bounded final re-offer pass: a unit that minted but never reached
        # OFFERED (transient offer failure during the loop above) gets a
        # last chance to self-heal within this same run before we decide the
        # job's terminal state.
        for unit in job.units:
            if unit.state != MINTED:
                continue
            for _ in range(_UNIT_MAX_ATTEMPTS):
                await _ensure_offer(job, unit)
                persist(job)
                if unit.state == OFFERED:
                    break

        # Completion is conditional on every unit having reached a resolved
        # state (OFFERED or UNIT_FAILED). A unit stuck at MINTED means the
        # NFT exists on-chain but was never offered to the user -- DONE is
        # terminal and load_all_resumable only resumes PAID/FULFILLING, so
        # marking DONE here would strand that NFT forever. Instead stay
        # FULFILLING (non-terminal, resumable): the next restart's startup
        # sweep retries _ensure_offer on the still-MINTED unit, never
        # re-minting. Tradeoff: the active-job lock holds until it clears --
        # rare (offer failure right after a successful mint is uncommon), and
        # the NFT itself is safe (minted + journaled), just pending an offer.
        if all(u.state in (OFFERED, UNIT_FAILED) for u in job.units):
            job.state = DONE
        else:
            job.state = FULFILLING
        if not persist(job) and job.state == DONE:
            # DONE is terminal: it gets pruned from the live registry and is
            # never resumed. If the DONE record couldn't reach disk, the
            # stale on-disk state would already resume the job after a
            # restart — but only if the in-memory job doesn't terminalize
            # first. Stay FULFILLING (non-terminal, resumable, flagged
            # persist_failed): the resumed run finds every unit already
            # resolved and just retries this final persist.
            job.state = FULFILLING
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.error("bulk job %s failed: %s", job.id, e)
        job.state = FAILED
        job.error = str(e)
        persist(job)


def load_all_resumable() -> list[BulkMintJob]:
    """Load every resumable job record: AWAITING_PAYMENT, PAID, FULFILLING.

    AWAITING_PAYMENT is resumable (#228): the start handler persists the job
    once the payment payload exists, so a crash after the user was shown (and
    possibly signed) the payment request re-enters the ledger watch —
    bounded by the original created_at-anchored window, never re-requesting
    payment — instead of dropping the job. Terminal records (done/failed/
    payment_timeout/cancelled) are never resumed."""
    out: list[BulkMintJob] = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for name in os.listdir(JOBS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, name)) as f:
                data = json.load(f)
            if data.get("state") in (AWAITING_PAYMENT, PAID, FULFILLING):
                out.append(BulkMintJob.from_serialized(data))
        except Exception:
            logging.error("skipping unreadable bulk job record %s", name)
    return out
