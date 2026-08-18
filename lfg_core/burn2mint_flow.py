# Burn-to-mint (#220): burn M of your own live LFG NFTs in exchange for M
# freshly minted ones. Supply-neutral (-M burned, +M minted), so the mint side
# runs as a CAP-EXEMPT bulk job (entitlement.BurnEntitlement) that skips the
# MAX_COLLECTION_SIZE headroom clamp/reservation entirely (#226 seam).
#
# Burn authorization — the no-forced-burns principle: the app NEVER
# issuer-burns tokens out of a user's wallet. Every burn is a user-signed
# NFTokenBurn collected via a sequential XUMM signing loop (one payload per
# NFT; XLS-56 Batch would collapse this to one signature but the amendment is
# not enabled, so a loop it is). Ownership of every requested nft_id is
# verified on-ledger, FAIL-CLOSED (an indeterminate lookup refuses the burn —
# same posture as economy Deposit's owner verify), immediately before each
# signing request.
#
# Fail-safe ordering — burns are irreversible, so the invariant is:
# a VALIDATED burn is durably recorded before anything else happens, and from
# that moment the user is owed a mint no matter what fails afterwards.
#   1. Each burn is appended to the session record (atomic tmp+replace write,
#      same discipline as bulk_mint_flow.persist) the poll it is confirmed
#      validated + tesSUCCESS on-ledger.
#   2. Conversion into the mint job is IDEMPOTENT: the BulkMintJob id is
#      derived from the session id (b2m<session_id>), so a crash between "job
#      persisted" and "session updated" cannot double-create the job on
#      resume — the existing record wins.
#   3. The bulk job itself persists per unit (bulk_mint_flow) and resumes via
#      the same startup sweep as paid bulk jobs; a unit whose mint ultimately
#      fails converts into a durable mint_credits row (#226 pattern) — the
#      user keeps a redeemable credit, never loses a burn.
#   4. Startup resume (resume_all, wired from lfg_service.app) re-checks the
#      one in-flight burn payload (a burn signed just before a crash still
#      counts) and then converts whatever validated. Remaining UNSIGNED
#      NFTs are simply not burned — the user keeps them; only validated
#      burns ever convert, and every validated burn always converts.
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from lfg_core import bulk_mint_flow, config, entitlement, memos, xrpl_ops, xumm_ops

JOBS_DIR = os.getenv("BURN2MINT_JOBS_DIR", "burn2mint_jobs")

AWAITING_BURNS = "awaiting_burns"
FULFILLING = "fulfilling"  # all burns resolved; cap-exempt bulk job minting
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATES = {DONE, FAILED, CANCELLED}

# Per-burn states
B_PENDING = "pending"  # not yet offered for signing
B_AWAITING_SIGNATURE = "awaiting_signature"  # XUMM payload live
B_BURNED = "burned"  # validated + tesSUCCESS on-ledger — irreversible, owed a mint
B_FAILED = "failed"  # definitively NOT burned (expired/refused/tec) — no mint owed


@dataclass
class Burn:
    nft_id: str
    state: str = B_PENDING
    payload_uuid: str | None = None
    payload_link: str | None = None
    txid: str | None = None
    tx_hash: str | None = None  # validated burn tx — set only at B_BURNED
    error: str | None = None


class Burn2MintSession:
    def __init__(
        self,
        discord_id: str,
        wallet_address: str,
        nft_ids: list[str],
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
        self.network = config.XRPL_NETWORK
        self.created_at = time.time()
        self.state = AWAITING_BURNS
        self.error: str | None = None
        self.burns: list[Burn] = [Burn(nft_id=n) for n in nft_ids]
        self.current = 0  # index of the burn being signed
        self.bulk_job_id: str | None = None
        self.persist_failed = False
        # Poll serialization: concurrent GET polls must not double-advance.
        self.lock = asyncio.Lock()

    # -- durable record ----------------------------------------------------
    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "discord_id": self.discord_id,
            "wallet_address": self.wallet_address,
            "platform": self.platform,
            "push_user_token": self.push_user_token,
            "return_url": self.return_url,
            "network": self.network,
            "created_at": self.created_at,
            "state": self.state,
            "error": self.error,
            "burns": [asdict(b) for b in self.burns],
            "current": self.current,
            "bulk_job_id": self.bulk_job_id,
        }

    @classmethod
    def from_serialized(cls, d: dict[str, Any]) -> Burn2MintSession:
        s = cls(
            d["discord_id"],
            d["wallet_address"],
            [],
            platform=d.get("platform", "discord"),
            push_user_token=d.get("push_user_token"),
            return_url=d.get("return_url"),
        )
        s.id = d["id"]
        s.network = d["network"]
        s.created_at = d["created_at"]
        s.state = d["state"]
        s.error = d.get("error")
        s.burns = [Burn(**b) for b in d["burns"]]
        s.current = d.get("current", 0)
        s.bulk_job_id = d.get("bulk_job_id")
        return s

    def burned_ids(self) -> list[str]:
        return [b.nft_id for b in self.burns if b.state == B_BURNED]

    def to_dict(self) -> dict[str, Any]:
        cur = self.burns[self.current] if self.current < len(self.burns) else None
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "platform": self.platform,
            "network": self.network,
            "burns": [asdict(b) for b in self.burns],
            "burned": len(self.burned_ids()),
            "total": len(self.burns),
            "current_index": self.current if cur else None,
            "burn_link": cur.payload_link if cur and cur.state == B_AWAITING_SIGNATURE else None,
            "bulk_job_id": self.bulk_job_id,
            "persist_failed": self.persist_failed,
        }


def _record_path(session_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{session_id}.json")


def persist(session: Burn2MintSession) -> bool:
    """Atomic full-record write; never raises (same contract as
    bulk_mint_flow.persist — a raise mid-poll could strand a just-validated,
    irreversible burn in memory only)."""
    tmp: str | None = None
    try:
        os.makedirs(JOBS_DIR, exist_ok=True)
        data = session.serialize()
        fd, tmp = tempfile.mkstemp(dir=JOBS_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _record_path(session.id))
    except Exception as e:
        logging.error("failed to persist burn2mint session %s: %s", session.id, e)
        session.persist_failed = True
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False
    session.persist_failed = False
    return True


def delete_record(session_id: str) -> bool:
    try:
        os.remove(_record_path(session_id))
    except FileNotFoundError:
        pass
    except OSError as e:
        logging.error("failed to delete burn2mint record %s: %s", session_id, e)
        return False
    return True


def load_all_resumable() -> list[Burn2MintSession]:
    """Every non-terminal session record. AWAITING_BURNS records matter most:
    they may hold validated (irreversible) burns that never converted."""
    out: list[Burn2MintSession] = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for name in os.listdir(JOBS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, name)) as f:
                data = json.load(f)
            if data.get("state") in (AWAITING_BURNS, FULFILLING):
                out.append(Burn2MintSession.from_serialized(data))
        except Exception:
            logging.error("skipping unreadable burn2mint record %s", name)
    return out


# -- on-ledger ownership verify (fail-closed) ------------------------------


async def verify_ownership(
    wallet_address: str, nft_ids: list[str], *, nft_info: Any = None
) -> str | None:
    """Verify the caller owns every nft_id and each is one of OUR live
    character NFTs. Returns an error string, or None when all pass.

    FAIL-CLOSED: an indeterminate nft_info lookup (None) refuses — the burn
    request must never proceed on unprovable ownership (same posture as
    economy Deposit). Membership is issuer+taxon, never taxon-from-ID alone."""
    nft_info = nft_info or xrpl_ops.nft_info
    allowed_taxons = {config.NFT_TAXON, config.ASSEMBLE_TAXON}
    for nft_id in nft_ids:
        info = await nft_info(nft_id)
        if info is None:
            return f"ownership_unverifiable:{nft_id}"
        if info.get("is_burned"):
            return f"already_burned:{nft_id}"
        if info.get("owner") != wallet_address:
            return f"not_owner:{nft_id}"
        if info.get("issuer") != config.SIGNING_ACCOUNT or info.get("taxon") not in allowed_taxons:
            return f"not_collection:{nft_id}"
    return None


# -- signing loop ----------------------------------------------------------


async def start_next_burn(session: Burn2MintSession, *, nft_info: Any = None) -> bool:
    """Build the XUMM burn payload for the current pending burn. Re-verifies
    ownership fail-closed immediately before requesting the signature.
    Returns True when a payload is live; False marks the burn B_FAILED
    (nothing burned — safe) and leaves the caller to advance/convert."""
    burn = session.burns[session.current]
    err = await verify_ownership(session.wallet_address, [burn.nft_id], nft_info=nft_info)
    if err is not None:
        burn.state = B_FAILED
        burn.error = err
        persist(session)
        return False
    payload = await xumm_ops.create_burn_payload(
        session.wallet_address,
        burn.nft_id,
        return_url=session.return_url,
        user_token=session.push_user_token,
        platform=memos.platform_for_surface(session.platform),
    )
    if not payload:
        burn.state = B_FAILED
        burn.error = "burn_payload_failed"
        persist(session)
        return False
    burn.payload_uuid = payload.get("uuid")
    burn.payload_link = payload.get("xumm_url")
    burn.state = B_AWAITING_SIGNATURE
    # PERSIST-BEFORE-EXPOSE: a signable burn payload must never outlive the
    # process without a durable record of its uuid — if the user signed it
    # after a crash, recovery could not find the validated burn and the NFT
    # would be destroyed with no entitlement. If the record can't be written,
    # withdraw the payload (best-effort cancel) and refuse instead of
    # exposing it; nothing is burned, the user simply retries.
    if not persist(session):
        cancelled = False
        if burn.payload_uuid:
            try:
                cancelled = bool(await xumm_ops.cancel_xumm_payload(burn.payload_uuid))
            except Exception:
                pass
        burn.state = B_FAILED
        burn.error = "burn record not durable"
        burn.payload_link = None  # keep payload_uuid for manual reconciliation
        persist(session)
        if not cancelled:
            # The payload could not be confirmed withdrawn AND the session
            # record is (or was) not durable — and the start handler deletes
            # the session record on this failure path, so the session file
            # alone cannot be trusted to carry the uuid. Journal a dedicated
            # ORPHAN-PAYLOAD record (own file, never deleted by the session
            # lifecycle) so a signed-anyway burn stays discoverable and
            # reconcilable by payload uuid / wallet / nft_id.
            _journal_orphan_payload(session, burn)
        return False
    return True


def _orphan_record_path(payload_uuid: str) -> str:
    return os.path.join(JOBS_DIR, f"orphan-payload-{payload_uuid}.json")


def _journal_orphan_payload(session: Burn2MintSession, burn: Burn) -> bool:
    """Durable journal for a burn payload that may still be signed but whose
    session record could not be persisted (and whose XUMM cancel was not
    confirmed). Written as its own file so no session-record lifecycle
    (including the start handler's delete_record on a failed start) can drop
    it; load_all_resumable ignores it (state 'orphan_payload' is not
    resumable). If even this write fails (fully degraded disk), the CRITICAL
    log line with the uuid is the last resort."""
    record = {
        "state": "orphan_payload",
        "session_id": session.id,
        "wallet_address": session.wallet_address,
        "discord_id": session.discord_id,
        "network": session.network,
        "nft_id": burn.nft_id,
        "payload_uuid": burn.payload_uuid,
        "created_at": time.time(),
    }
    ok = False
    tmp: str | None = None
    try:
        os.makedirs(JOBS_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=JOBS_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, _orphan_record_path(burn.payload_uuid or session.id))
        ok = True
    except Exception:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    logging.critical(
        "burn2mint %s: unpersistable burn payload %s for %s could not be "
        "cancelled — orphan journal %s; reconcile manually if it gets signed",
        session.id,
        burn.payload_uuid,
        burn.nft_id,
        "written" if ok else "ALSO FAILED (log line is the only record)",
    )
    return ok


async def _check_signed_burn(
    session: Burn2MintSession,
    burn: Burn,
    *,
    get_payload_status: Any = None,
    get_tx: Any = None,
) -> bool:
    """Poll one in-flight burn. Returns True when the burn RESOLVED
    (B_BURNED or B_FAILED); False = still pending, poll again.

    B_BURNED is set only on a validated + tesSUCCESS NFTokenBurn whose signer
    matches the session wallet and whose NFTokenID matches the requested burn
    — and it is PERSISTED before returning (invariant #1: an irreversible
    burn is durable the instant it is known)."""
    get_payload_status = get_payload_status or xumm_ops.get_payload_status
    get_tx = get_tx or xrpl_ops.get_tx
    if burn.txid is None:
        s = await get_payload_status(burn.payload_uuid)
        if s is None:
            return False
        if s.get("expired") and not s.get("signed"):
            burn.state = B_FAILED
            burn.error = "signing request expired"
            persist(session)
            return True
        if not s.get("signed"):
            return False
        # The payload pins Account, so Xaman should refuse other wallets;
        # defense in depth: a foreign signer's burn earns no entitlement.
        if s.get("account") != session.wallet_address:
            burn.state = B_FAILED
            burn.error = "signer_mismatch"
            persist(session)
            return True
        burn.txid = s.get("txid")
        if not burn.txid:
            burn.state = B_FAILED
            burn.error = "signed but no txid reported"
            persist(session)
            return True
        persist(session)
    try:
        tx = await get_tx(burn.txid)
    except Exception:
        return False  # lookup blip: neither burned nor failed — keep polling
    if not tx.get("validated"):
        return False
    meta = tx.get("meta") or {}
    txf = tx.get("tx_json") or tx
    if meta.get("TransactionResult") != "tesSUCCESS":
        burn.state = B_FAILED
        burn.error = f"burn failed on-ledger: {meta.get('TransactionResult')}"
        persist(session)
        return True
    if txf.get("NFTokenID") != burn.nft_id or txf.get("Account") != session.wallet_address:
        # Validated tx that isn't OUR requested burn: no entitlement.
        burn.state = B_FAILED
        burn.error = "validated transaction does not match the requested burn"
        persist(session)
        return True
    burn.state = B_BURNED
    burn.tx_hash = burn.txid
    burn.error = None
    if not persist(session):
        # The burn already happened on-ledger — escalate; the in-memory
        # session keeps the credit and conversion still runs, but a crash
        # before a later persist would need manual reconciliation by tx hash.
        logging.critical(
            "burn2mint %s: VALIDATED burn %s (%s) failed to persist — "
            "irreversible burn recorded in memory only until a later persist",
            session.id,
            burn.nft_id,
            burn.txid,
        )
    return True


def bulk_job_id_for(session: Burn2MintSession) -> str:
    """Deterministic mint-job id: makes conversion idempotent across crashes
    (invariant #2) — a resume can never double-create the job."""
    return f"b2m{session.id}"


def build_mint_job(session: Burn2MintSession) -> bulk_mint_flow.BulkMintJob:
    """Convert this session's VALIDATED burns into a cap-exempt bulk mint
    job, persisted and ready to run. No payment: the job is created directly
    in PAID state (entitlement source='burn' is what it is owed for), and
    clamp_to_headroom() with a cap-exempt entitlement takes NO headroom
    reservation — the seam's cap-exemption (#226) asserted in tests."""
    burned = session.burned_ids()
    ent = entitlement.build_burn_entitlement(burned)
    job = bulk_mint_flow.BulkMintJob(
        discord_id=session.discord_id,
        wallet_address=session.wallet_address,
        requested_qty=ent.quantity,
        platform=session.platform,
        push_user_token=session.push_user_token,
        return_url=session.return_url,
    )
    job.id = bulk_job_id_for(session)
    job.entitlement = ent  # set BEFORE clamp so the clamp sees cap_exempt
    job.clamp_to_headroom()
    assert job.quantity == ent.quantity, "cap-exempt clamp must never shrink a burn entitlement"
    job.state = bulk_mint_flow.PAID
    job.paid_at = time.time()
    job.pay_with = "BURN"
    return job


async def convert(session: Burn2MintSession, launch_job: Any) -> None:
    """All burns resolved: convert validated ones into the mint job (or fail
    the session if none validated — nothing was burned, nothing is owed).

    `launch_job(job)` is supplied by the service layer: it must register the
    job and create the run_bulk_mint_job task (idempotent — if a job with
    this id is already registered/resumed, it just returns it)."""
    burned = session.burned_ids()
    if not burned:
        session.state = FAILED
        session.error = session.error or "no burns validated"
        persist(session)
        return
    # ADOPT-BEFORE-CREATE: a job record for this session may already exist —
    # a prior convert persisted it but crashed (or failed) before the session
    # write landed, and by now the resumed job may carry REAL MINT PROGRESS
    # (minted/offered units). Re-persisting a fresh PAID job over it would
    # erase that progress and let a later restart re-mint units that already
    # landed on-ledger. So: load the existing record and only create+persist
    # when none exists. An existing-but-unreadable record is fail-closed —
    # never overwritten; the session stays AWAITING_BURNS and retries later.
    job_id = bulk_job_id_for(session)
    job: bulk_mint_flow.BulkMintJob | None = None
    job_path = bulk_mint_flow._record_path(job_id)
    if os.path.exists(job_path):
        try:
            with open(job_path) as f:
                job = bulk_mint_flow.BulkMintJob.from_serialized(json.load(f))
        except Exception as e:
            session.error = "existing mint-job record unreadable; conversion will retry"
            persist(session)
            logging.critical(
                "burn2mint %s: existing mint-job record %s unreadable (%s) — "
                "NOT overwriting (it may hold mint progress); retried later",
                session.id,
                job_id,
                e,
            )
            return
    if job is None:
        job = build_mint_job(session)
        # Persist the job record FIRST (durable owed-mints ledger); only a
        # durable job may flip the session to FULFILLING. If the job record
        # cannot be written, the session STAYS AWAITING_BURNS (all burns
        # already resolved), which advance()/resume_all() re-convert on the
        # next poll or restart — the deterministic job id makes that retry
        # target the same record, never a double-create. Marking FULFILLING
        # over an in-memory-only job would let a crash strand the validated
        # burns: startup would skip reconversion and the next status poll
        # would mark the session DONE with no job ever recorded.
        if not bulk_mint_flow.persist(job):
            session.error = "mint-job record not durable; conversion will retry"
            persist(session)
            logging.critical(
                "burn2mint %s: mint-job record failed to persist — validated "
                "burns %s stay convertible (session remains awaiting_burns); "
                "retried on next poll/restart",
                session.id,
                burned,
            )
            return
    session.bulk_job_id = job_id
    session.state = FULFILLING
    session.error = None
    persist(session)
    await launch_job(job)


async def advance(
    session: Burn2MintSession,
    launch_job: Any,
    *,
    get_payload_status: Any = None,
    get_tx: Any = None,
    nft_info: Any = None,
) -> None:
    """Drive the session one poll step. Called from the status handler under
    session.lock. Sequential signing loop: resolve the current burn, then
    offer the next; once every burn is resolved, convert."""
    if session.state != AWAITING_BURNS:
        return
    while session.current < len(session.burns):
        burn = session.burns[session.current]
        if burn.state == B_PENDING:
            await start_next_burn(session, nft_info=nft_info)
            burn = session.burns[session.current]
            if burn.state == B_AWAITING_SIGNATURE:
                return  # payload live; wait for the user
        if burn.state == B_AWAITING_SIGNATURE:
            resolved = await _check_signed_burn(
                session, burn, get_payload_status=get_payload_status, get_tx=get_tx
            )
            if not resolved:
                return  # still waiting on signature/validation
        # resolved (B_BURNED or B_FAILED): move on
        session.current += 1
        persist(session)
    await convert(session, launch_job)


async def _discard_or_park_inflight(session: Burn2MintSession, cur: Burn) -> bool:
    """Resolve/withdraw the in-flight burn during cancel/resume. Returns True
    when the burn is RESOLVED (B_BURNED/B_FAILED); False when it must be
    PARKED — still awaiting, because the signed (or unwithdrawable) tx may
    yet validate on-ledger. FAIL-CLOSED: a burn is discarded only when it
    provably cannot land — never-signed AND confirmed cancelled at XUMM. A
    signed-but-unvalidated burn, an indeterminate payload status, or an
    unconfirmed cancel all park (the tx could still burn the NFT after we
    forgot it — the exact hole this closes)."""
    await _check_signed_burn(session, cur)
    if cur.state != B_AWAITING_SIGNATURE:
        return True
    if cur.txid is not None:
        return False  # signed: it can still validate — never discard
    cancelled = False
    if cur.payload_uuid:
        try:
            cancelled = bool(await xumm_ops.cancel_xumm_payload(cur.payload_uuid))
        except Exception:
            pass
    if not cancelled:
        # Cancel unconfirmed (XUMM error, or the payload already resolved —
        # possibly SIGNED — between our status check and the cancel): the
        # burn may still land. Park it.
        return False
    cur.state = B_FAILED
    cur.error = "cancelled"
    persist(session)
    return True


async def cancel(session: Burn2MintSession, launch_job: Any) -> None:
    """Back out of the signing loop. Burns already validated are IRREVERSIBLE
    and still convert into mints (a cancel can never orphan a burn); with no
    validated burns the session simply cancels. An in-flight payload that is
    signed (or cannot be confirmed withdrawn) PARKS the session in
    AWAITING_BURNS instead of being discarded — the tx can still validate
    after a cancel, and forgetting it would burn the NFT with no entitlement;
    the next poll / startup resume resolves it and then converts/cancels."""
    if session.state != AWAITING_BURNS:
        return
    for b in session.burns:
        if b.state == B_PENDING:
            b.state = B_FAILED
            b.error = "cancelled"
    if session.current < len(session.burns):
        cur = session.burns[session.current]
        if cur.state == B_AWAITING_SIGNATURE and cur.payload_uuid:
            if not await _discard_or_park_inflight(session, cur):
                session.error = "cancel pending: in-flight burn must resolve first"
                persist(session)
                return  # parked: still AWAITING_BURNS, resolved by poll/resume
    if session.burned_ids():
        await convert(session, launch_job)
        return
    session.state = CANCELLED
    if not delete_record(session.id):
        persist(session)  # tombstone so a restart can't resurrect it


async def resume_all(launch_job: Any) -> list[Burn2MintSession]:
    """Startup recovery (invariant #4), called from lfg_service.app AFTER
    resume_bulk_jobs (so an already-converted mint job is resumed by the
    bulk sweep first and launch_job can adopt it by id).

    - FULFILLING sessions just re-register (their job resumes via the bulk
      sweep; if the job record is already terminal/pruned, the poll handler
      marks the session done).
    - AWAITING_BURNS sessions re-check the one in-flight payload (a burn
      signed just before the crash still counts), then convert whatever
      validated. Unsigned remainders are NOT re-solicited — no client is
      attached — the user simply keeps those NFTs. An in-flight burn that is
      signed-but-unvalidated (or whose payload cannot be confirmed dead) is
      NEVER discarded: the session PARKS in AWAITING_BURNS — still durable,
      still resumable, still advanceable by a status poll — until the tx
      resolves (an unsigned payload self-resolves via its 15-min expiry, so
      a parked session converts on a later poll/restart at the latest).
      Runs regardless of BURN_TO_MINT_ENABLED: validated burns are owed
      their mints even if the feature was flipped off."""
    sessions = load_all_resumable()
    for session in sessions:
        try:
            if session.state == FULFILLING:
                continue
            for b in session.burns:
                if b.state == B_PENDING:
                    b.state = B_FAILED
                    b.error = "service restarted before signing completed"
            parked = False
            for b in session.burns:
                if b.state != B_AWAITING_SIGNATURE:
                    continue
                if not b.payload_uuid:
                    b.state = B_FAILED
                    b.error = "service restarted before signing completed"
                    continue
                if not await _discard_or_park_inflight(session, b):
                    parked = True
            if parked:
                persist(session)
                logging.warning(
                    "burn2mint %s: in-flight burn unresolved at resume — session "
                    "parked awaiting_burns until the tx/payload resolves",
                    session.id,
                )
                continue
            await convert(session, launch_job)
        except Exception:
            logging.exception("burn2mint resume failed for session %s", session.id)
    return sessions
