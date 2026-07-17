# lfg_core/mint_flow.py
# Mint session state machine: payment → image generation → CDN upload →
# XRPL mint → offer → XUMM accept payload. Orchestrates the same pipeline as
# the bot's mint button, but exposes state for polling instead of sending
# Discord messages.

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from lfg_core import (
    cdn,
    config,
    db_path,
    headroom,
    image_archive,
    layer_store,
    memos,
    rarity,
    swap_compose,
    traits,
    xrpl_ops,
    xumm_ops,
)
from lfg_core.db_helpers import get_next_nft_number, record_nft_mint

# Session states
AWAITING_PAYMENT = "awaiting_payment"
GENERATING = "generating"
MINTING = "minting"
CREATING_OFFER = "creating_offer"
OFFER_READY = "offer_ready"
DONE = "done"
FAILED = "failed"
PAYMENT_TIMEOUT = "payment_timeout"
CANCELLED = "cancelled"  # user backed out of the pay screen (issue #141)

# offer_ready is the success end-state: the background task is finished and
# the user has the accept QR. It must be terminal or the one-active-session
# guard would block every subsequent mint.
TERMINAL_STATES = {OFFER_READY, DONE, FAILED, PAYMENT_TIMEOUT, CANCELLED}

# nft_numbers handed to in-flight sessions but not yet in the database.
# get_next_nft_number() is MAX+1, so without this two concurrent mints would
# get the same number and overwrite each other's CDN files.
_nft_number_lock = asyncio.Lock()
_reserved_numbers: set[int] = set()


class MintSession:
    def __init__(
        self,
        discord_id: str,
        wallet_address: str,
        return_url: dict[str, str] | None = None,
        platform: str = "discord",
        push_user_token: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.discord_id = discord_id
        self.platform = platform
        self.wallet_address = wallet_address
        self.return_url = return_url  # XUMM return_url back into Discord
        # #135: stored XUMM push token for this user, if any. Threaded into
        # every sign request this session builds so a returning user gets a
        # push to Xaman instead of a QR. None falls back to the QR/deep link.
        self.push_user_token = push_user_token
        self.created_at = time.time()
        self.state = AWAITING_PAYMENT
        self.error: str | None = None
        self.pay_with: str | None = None  # "LFGO" or "XRP", set by prepare_payment
        self.pay_amount: str | None = None
        self.payment_link: str | None = None
        self.payment_uuid: str | None = None  # XUMM payload uuid for scan tracking
        # Whether the payment payload itself was signed in Xaman. Polling of
        # the payment payload stops here (NOT at qr_scanned — the signature,
        # and the rotated push token it carries, lands after the open).
        self.payment_signed = False
        # #212: per-payload push delivery state ("sent" | "failed" | None) so
        # the UI can say "check your Xaman app" instead of implying a QR scan
        # is the only path.
        self.payment_push: str | None = None
        self.accept_push: str | None = None
        # #212: a fresh push token observed on a signed payload of this
        # session, for the service to persist (tokens rotate; sign-in isn't
        # the only chance to capture one). Cleared by the service on persist.
        self.issued_user_token: str | None = None
        self.qr_scanned = False  # payment QR opened in Xaman (issue #22)
        self.nft_number: int | None = None
        self.nft_id: str | None = None
        self.image_url: str | None = None
        # MP4 URL for animated compositions (image_url is the PNG poster
        # frame); None for still NFTs.
        self.video_url: str | None = None
        # #41: the minted edition's traits (LFG-naming, e.g. Head -> Hat) and
        # body_type, set once mint_one_unit confirms the mint on-chain. None
        # pre-fulfillment, same None-handling style as image_url/nft_id --
        # lets the X poster compose tweet copy (and rank the rarest slot,
        # which is body-scoped) straight from the firehose event.
        self.traits: dict[str, str] | None = None
        self.body_type: str | None = None
        self.accept_qr_url: str | None = None
        self.accept_deeplink: str | None = None
        self.accept_uuid: str | None = None
        self.accept_scanned = False
        self.accept_signed = False
        # The run_mint_session background task, set by the service after it
        # spawns it, so cancel() can stop the payment wait promptly (#141).
        self.task: asyncio.Task[None] | None = None
        # #226: True once the service took this session's 1-unit headroom
        # reservation (claimant "mint:<id>"). settle_headroom is a strict
        # no-op while False, so sessions created outside the service (tests,
        # tooling) never touch the reservation store.
        self.headroom_reserved = False
        # Terminal-event publish guard, read/set by lfg_service.app when it
        # publishes mint.completed/mint.failed to the event firehose.
        self._published = False

    def _payment_params(self) -> dict[str, Any]:
        """Destination/amount for this session's payment path. LFGO goes to
        the issuer (= burned on arrival); XRP goes to the bot wallet, which
        buys and burns the LFGO off the DEX after payment."""
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
        """Detect the payment path (LFGO holders burn LFGO; everyone else
        pays XRP — never explained to the user beyond the pay pill) and
        create the XUMM sign-request payload. Xaman cannot parse the
        raw-JSON detect link, so the payload URL is the one that must end
        up in the payment QR (issue #8). On payload failure payment_uuid
        stays None — both callers (handle_mint_start and run_mint_session)
        gate on it and fail the session terminally rather than entering the
        payment wait with only the unparseable static detect link (#262)."""
        balance = await xrpl_ops.get_trustline_balance(
            self.wallet_address, config.TOKEN_CURRENCY_HEX, config.TOKEN_ISSUER_ADDRESS
        )
        if balance is not None and balance >= Decimal(config.MINT_PRICE_LFGO):
            self.pay_with, self.pay_amount = "LFGO", config.MINT_PRICE_LFGO
        else:
            self.pay_with, self.pay_amount = "XRP", config.MINT_PRICE_XRP
        p = self._payment_params()
        self.payment_link = xumm_ops.generate_static_payment_link(
            p["destination"], value=p["value"], currency=p["currency"], issuer=p["issuer"]
        )
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
            self.payment_push = payload.get("push")

    async def regenerate_payment(self) -> None:
        """Replace an expired/missed payment QR with a fresh XUMM payload
        without restarting the whole session (issue #22)."""
        self.qr_scanned = False
        self.payment_uuid = None
        self.payment_push = None
        self.payment_signed = False
        await self.prepare_payment()

    def cancel(self) -> bool:
        """Back out of the pay screen (issue #141): mark the session terminal
        so the one-active-session lock releases immediately, and cancel the
        background task so its payment wait stops polling. Only legal while
        still awaiting payment — past that the user has paid and the pipeline
        must run to completion or failure. Returns True if cancelled.

        The state check and CANCELLED assignment are synchronous (no await
        between them), so on the single event loop this cannot race the
        background task's own state transitions — and run_mint_session
        leaves AWAITING_PAYMENT in the same synchronous step in which
        wait_for_payment reports the payment confirmed, so a confirmed
        (paid) session can never be cancelled."""
        if self.state != AWAITING_PAYMENT:
            return False
        self.state = CANCELLED
        if self.task is not None:
            # CancelledError is a BaseException, so run_mint_session's
            # `except Exception` cannot catch it and overwrite CANCELLED.
            self.task.cancel()
        # #226: give the headroom reservation back right here — a task
        # cancelled before it ever started running skips run_mint_session's
        # finally, so the cancel path must settle directly. Idempotent: the
        # dying task's own finally sees headroom_reserved already False.
        settle_headroom(self)
        return True

    def mark_published(self) -> None:
        """Mark the terminal firehose event as already published (or, for a
        deliberate user cancel, as suppressed) — kept as a method so callers
        never reach into the private guard attribute directly."""
        self._published = True

    def ensure_payment_fallback(self) -> None:
        """If prepare_payment was cancelled or failed, default to the XRP
        path (any wallet can pay XRP; a trustline-less wallet could never
        complete an LFGO payment) with the static detect link."""
        if self.pay_with is None:
            self.pay_with, self.pay_amount = "XRP", config.MINT_PRICE_XRP
        if not self.payment_link:
            p = self._payment_params()
            self.payment_link = xumm_ops.generate_static_payment_link(
                p["destination"], value=p["value"], currency=p["currency"], issuer=p["issuer"]
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "pay_with": self.pay_with,
            "pay_amount": self.pay_amount,
            "payment_link": self.payment_link,
            "payment_push": self.payment_push,
            "qr_scanned": self.qr_scanned,
            "accept_scanned": self.accept_scanned,
            "accept_signed": self.accept_signed,
            "nft_number": self.nft_number,
            "nft_id": self.nft_id,
            "image_url": self.image_url,
            "video_url": self.video_url,
            "traits": self.traits,
            "body_type": self.body_type,
            "accept_qr_url": self.accept_qr_url,
            "accept_deeplink": self.accept_deeplink,
            "accept_push": self.accept_push,
        }


async def _upload_to_bunny(path_on_cdn: str, data: bytes, content_type: str) -> str:
    return await cdn.upload_to_bunny(config.BUNNY_CDN_FOLDER, path_on_cdn, data, content_type)


async def _allocate_nft_number() -> int:
    """Next NFT number, skipping numbers reserved by in-flight sessions."""
    async with _nft_number_lock:
        number = await asyncio.to_thread(get_next_nft_number)
        while number in _reserved_numbers:
            number += 1
        _reserved_numbers.add(number)
        return number


def _release_unused_number(session: MintSession) -> None:
    """Release a reserved number when the session fails before anything was
    minted on-chain with it. Numbers that reached the chain stay reserved
    until the DB record lands (or forever, if it never does)."""
    if session.nft_number is not None and session.nft_id is None:
        _reserved_numbers.discard(session.nft_number)


def settle_headroom(session: MintSession) -> None:
    """Settle the 1-unit headroom reservation the service took for this
    session (#226). If the session minted (nft_id set), the reservation is
    retired to the durable pending set — the mint is on-chain but invisible
    to supply.current_supply until the listener indexes it, so it must keep
    counting against headroom until then. Otherwise the unit will never mint
    and the reservation is released outright. Strict no-op unless the service
    reserved (headroom_reserved); idempotent — the flag drops before the
    store call, so the cancel-path and the task-finally can both call this.
    Never raises (headroom store contract)."""
    if not getattr(session, "headroom_reserved", False):
        return
    session.headroom_reserved = False
    db = db_path.app_db_path(config.XRPL_NETWORK)
    claimant = f"mint:{session.id}"
    if session.nft_id:
        headroom.retire_to_pending(db, claimant, session.nft_id)
    else:
        headroom.release(db, claimant)


def _save_recovery_record(record: dict[str, Any]) -> None:
    """If the DB insert fails after an on-chain mint, persist the record to
    disk so an administrator can backfill the LFG table."""
    try:
        os.makedirs("failed_db_records", exist_ok=True)
        path = os.path.join("failed_db_records", f"nft_{record['nft_number']}.json")
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
        logging.error(f"DB insert failed; recovery record written to {path}")
    except Exception:
        logging.error(f"Failed to write recovery record: {traceback.format_exc()}")


@dataclass
class UnitResult:
    """Result of minting one edition via mint_one_unit. On any failure
    `error` is set and the fields up to the point of failure are populated
    (e.g. `nft_id` set but `offer_id`/`accept` None = minted-but-offer-failed)
    so a bulk caller can record partial progress instead of losing it."""

    nft_number: int | None
    nft_id: str | None
    image_url: str | None
    offer_id: str | None
    accept: dict[str, Any] | None
    error: str | None
    # MP4 URL for animated compositions (image_url is the PNG poster frame);
    # defaulted so callers constructing results for still NFTs need not pass it.
    video_url: str | None = None
    # #41: LFG-naming traits dict + body_type, known only once the mint lands
    # on-chain (None on the earlier "mint never landed" failure paths).
    # Defaulted so every existing UnitResult(...) call site (this module's
    # earlier return statements, tests) stays valid unchanged.
    traits: dict[str, str] | None = None
    body_type: str | None = None


async def mint_one_unit(
    *,
    discord_id: str,
    wallet_address: str,
    platform: str,
    push_user_token: str | None,
    return_url: dict[str, str] | None,
    nft_number: int,
    session_tag: str,
    on_state: Callable[[str], None] | None = None,
    on_mint: Callable[[int, str, str | None], Awaitable[None]] | None = None,
) -> UnitResult:
    """Compose -> upload -> mint -> record (+ rarity) -> offer -> XUMM accept
    payload for a single edition, on a pre-allocated `nft_number`. Extracted
    from run_mint_session (#215) so single mint and bulk mint share one code
    path. Caller is responsible for `_allocate_nft_number()` beforehand.

    `session_tag` is a unique tag for image-archive staging (the single-mint
    caller uses its session id; bulk uses e.g. `job_id:index`).

    `on_state` is an optional callback invoked at the same points
    run_mint_session used to set session.state, so a single-mint caller can
    keep reporting its finer-grained MINTING/CREATING_OFFER UI states while
    bulk callers can ignore it entirely.

    `on_mint` is an optional async callback fired the instant the mint is
    confirmed on-chain (nft_number, nft_id, image_url), before the offer/XUMM
    steps run. A bulk caller uses it to durably persist MINTED immediately so
    a crash in the offer step can never trigger a re-mint on resume.
    """
    nft_id: str | None = None
    # Hoisted with None defaults so the catch-all below can return whatever
    # was already computed at the point of failure (#41 fix-wave, CodeRabbit
    # PR #245): an exception from on_mint/offer-creation/payload-creation
    # after a confirmed mint must not blank out traits/body_type that are
    # already known.
    traits_dict: dict[str, str] | None = None
    body: str | None = None
    try:
        # 1. Compose a random NFT from the unified layer store (same tree
        #    the Trait Swapper uses: <gender>/<TraitType>/<Value>.ext)
        store = layer_store.get_layer_store()
        body, attributes = await traits.select_random_attributes(store)
        output_path, is_video = await swap_compose.compose_nft(
            attributes, body, store, f"lfg_{nft_number}"
        )

        # 2. Upload image (+ video) and metadata to BunnyCDN. The still is
        #    staged for the local image archive (#163) and promoted below
        #    only once the mint is confirmed on-chain.
        image_cdn_url, video_cdn_url = await swap_compose.upload_output(
            output_path,
            is_video,
            _upload_to_bunny,
            # Foldered CDN layout, matching the swap convention: fresh mints
            # are <edition>/<edition>_0.* (metadata has no burnCount -> 0, so
            # the first swap writes _1 with no collision). Pre-2026-07-11
            # mints uploaded flat lfg_<n>.png / metadata_<n>.json — those
            # stay (on-chain URIs point there), with foldered copies added
            # for hygiene.
            f"{nft_number}/{nft_number}_0",
            keep_still=image_archive.pending_still_path(
                config.XRPL_NETWORK, nft_number, session_tag
            ),
        )

        metadata: dict[str, Any] = {
            "name": f"{config.NFT_COLLECTION_NAME} #{nft_number}",
            "image": image_cdn_url,
            "edition": nft_number,
            "attributes": attributes,
        }
        if video_cdn_url:
            metadata["video"] = video_cdn_url
        metadata_cdn_url = await _upload_to_bunny(
            f"{nft_number}/{nft_number}_0.json",
            json.dumps(metadata, indent=2).encode(),
            "application/json",
        )

        # 3. Mint on XRPL
        if on_state:
            on_state(MINTING)
        # Issuer is the NFT collection issuer (same account every other mint
        # path uses) — NOT the LFGO token issuer. On mainnet those differ, and
        # passing the token issuer makes mint_nft add an Issuer field = an
        # unauthorized mint-on-behalf, tecNO_PERMISSION on every attempt.
        nft_id = await xrpl_ops.mint_nft(
            metadata_cdn_url=metadata_cdn_url,
            taxon=config.NFT_TAXON,
            issuer=config.SWAP_ISSUER_ADDRESS,
            platform=memos.platform_for_surface(platform),
        )
        if not nft_id:
            image_archive.discard_still(config.XRPL_NETWORK, nft_number, session_tag)
            _reserved_numbers.discard(nft_number)
            return UnitResult(
                nft_number=nft_number,
                nft_id=None,
                image_url=image_cdn_url,
                video_url=video_cdn_url,
                offer_id=None,
                accept=None,
                error="Failed to mint NFT on XRPL. Please contact an administrator.",
            )
        # Mint confirmed — publish the new edition's art to the local archive
        # so /api/img serves it immediately (best-effort, #163).
        image_archive.promote_still(config.XRPL_NETWORK, nft_number, session_tag)

        # Computed here (synchronous, no await) rather than after on_mint below
        # so it's captured into the hoisted `traits_dict`/`body` locals before
        # any further awaits — an exception from on_mint or a later step must
        # still leave the catch-all able to return this already-known data
        # (#41 fix-wave, CodeRabbit PR #245).
        traits_dict = {t["trait_type"]: t["value"] for t in metadata["attributes"]}
        # The LFG table's headwear column is named Hat (layer tree uses Head)
        if "Head" in traits_dict:
            traits_dict["Hat"] = traits_dict.pop("Head")

        # Fire on_mint the instant the mint is confirmed on-chain, before any
        # further awaits (offer creation / XUMM accept payload). A bulk caller
        # uses this to persist the unit as MINTED immediately, so a crash in
        # the offer step can never cause a resume to re-mint a second edition
        # for the same unit (#215 double-mint window). Single mint passes
        # nothing -> no behavior change.
        if on_mint:
            await on_mint(nft_number, nft_id, image_cdn_url)

        record: dict[str, Any] = {
            "nft_number": nft_number,
            "nft_id": nft_id,
            "discord_id": discord_id,
            "owner_address": wallet_address,
            "metadata_url": metadata_cdn_url,
            "image_url": image_cdn_url,
            "traits": traits_dict,
            "network": config.XRPL_NETWORK,
            "body_type": body,
        }
        # The mint is on-chain at this point; a DB failure must not stop the
        # transfer offer from reaching the user.
        try:
            saved = await asyncio.to_thread(lambda: record_nft_mint(**record))
        except Exception:
            logging.error(f"record_nft_mint raised: {traceback.format_exc()}")
            saved = False
        if saved:
            _reserved_numbers.discard(nft_number)

            def _update_rarity() -> None:
                conn = rarity.connect()
                try:
                    for attr in metadata["attributes"]:
                        rarity.start_boost_clock(conn, body, attr["trait_type"], attr["value"])
                    rarity.start_boost_clock(conn, rarity.BODY_SENTINEL, rarity.BODY_CATEGORY, body)
                    rarity.recalculate_rarity(conn)
                finally:
                    conn.close()

            try:
                await asyncio.to_thread(_update_rarity)
            except Exception:
                logging.error(f"rarity update failed: {traceback.format_exc()}")
        else:
            # Keep the number reserved so it can't be reused this process,
            # and persist the record for manual recovery.
            _save_recovery_record(record)

        # 4. Create the transfer offer and the XUMM accept payload
        if on_state:
            on_state(CREATING_OFFER)
        offer_id = await xrpl_ops.create_nft_offer(
            nft_id, wallet_address, platform=memos.platform_for_surface(platform)
        )
        if not offer_id:
            return UnitResult(
                nft_number=nft_number,
                nft_id=nft_id,
                image_url=image_cdn_url,
                video_url=video_cdn_url,
                offer_id=None,
                accept=None,
                error=(
                    f"NFT minted (ID: {nft_id}) but offer creation failed. "
                    "Please contact an administrator."
                ),
                traits=traits_dict,
                body_type=body,
            )

        accept = await xumm_ops.create_accept_offer_payload(
            offer_id,
            return_url=return_url,
            user_token=push_user_token,
            platform=memos.platform_for_surface(platform),
        )
        if not accept:
            return UnitResult(
                nft_number=nft_number,
                nft_id=nft_id,
                image_url=image_cdn_url,
                video_url=video_cdn_url,
                offer_id=offer_id,
                accept=None,
                error=(
                    f"NFT minted and offer created ({offer_id}) but the XUMM "
                    "request failed. Please accept the offer manually."
                ),
                traits=traits_dict,
                body_type=body,
            )

        return UnitResult(
            nft_number=nft_number,
            nft_id=nft_id,
            image_url=image_cdn_url,
            video_url=video_cdn_url,
            offer_id=offer_id,
            accept=accept,
            error=None,
            traits=traits_dict,
            body_type=body,
        )

    except Exception as e:
        logging.error(f"mint_one_unit({nft_number}) failed: {traceback.format_exc()}")
        if nft_id is None:
            # Only if the mint never landed — a promoted still stays put.
            image_archive.discard_still(config.XRPL_NETWORK, nft_number, session_tag)
            _reserved_numbers.discard(nft_number)
        return UnitResult(
            nft_number=nft_number,
            nft_id=nft_id,
            image_url=None,
            video_url=None,
            offer_id=None,
            accept=None,
            error=str(e),
            traits=traits_dict,
            body_type=body,
        )


async def update_scan_state(session: MintSession) -> None:
    """Refresh the session's QR-scan flags from the XUMM payload status so
    the frontend can swap a scanned QR for a spinner (issue #22). Queries
    stop only once the relevant payload is seen SIGNED (not merely opened);
    API errors leave the flags untouched."""
    # Poll the payment payload until it is SIGNED (not merely opened): the
    # rotated push token only rides on the signature. Bounded by the session
    # leaving AWAITING_PAYMENT once the payment confirms on-ledger.
    if session.state == AWAITING_PAYMENT and session.payment_uuid and not session.payment_signed:
        s = await xumm_ops.get_payload_status(session.payment_uuid)
        if s:
            session.qr_scanned = s["opened"] or s["signed"]
            session.payment_signed = bool(s["signed"])
            _capture_issued_token(session, s)
    elif session.state == OFFER_READY and session.accept_uuid and not session.accept_signed:
        s = await xumm_ops.get_payload_status(session.accept_uuid)
        if s:
            session.accept_scanned = s["opened"] or s["signed"]
            session.accept_signed = s["signed"]
            _capture_issued_token(session, s)


def _capture_issued_token(session: MintSession, s: dict[str, Any]) -> None:
    """#212: stamp the push token XUMM issued on a signed payload so the
    service can persist it (tokens rotate; capturing on every signed payload
    — not just sign-in — keeps them fresh and self-heals an app-key swap).
    Only when the signer IS the session's wallet: a shared QR signed by a
    different account must never overwrite this user's stored token. The
    session's own push token is refreshed too, so a later payload in the SAME
    session (the accept offer) already uses the rotated token."""
    if s.get("signed") and s.get("user_token") and s.get("account") == session.wallet_address:
        session.issued_user_token = s["user_token"]
        session.push_user_token = s["user_token"]


async def run_mint_session(session: MintSession) -> None:
    """Drive a MintSession to a terminal state. Run as a background task."""
    if session.state in TERMINAL_STATES:
        # A cancel can land while handle_mint_start awaits prepare_payment;
        # running a terminal session would resurrect it (waiting for a
        # payment the user backed out of). Same guard bulk mint has.
        return
    try:
        # 1. Wait for the sender-verified payment on whichever path
        #    prepare_payment detected. not_before bounds the missed-payment
        #    backfill to this session's lifetime.
        session.ensure_payment_fallback()
        # #262 defense-in-depth (handle_mint_start already fails fast): with
        # no XUMM payload the session holds only the static detect link,
        # which Xaman cannot parse as a sign request — entering the payment
        # wait would show a dead pay screen for the full 300s. The
        # pay_with/pay_amount defaulting above must still run first. Known
        # tradeoff: this also defers #196 mint-credit redemption (normally
        # consumed by wait_for_payment's allow_credit backfill with no new
        # signature) until XUMM recovers — delay-only, the 30d credit TTL
        # far outlasts any outage.
        if session.payment_uuid is None:
            session.state = FAILED
            session.error = "signing service is busy — please try again shortly"
            return
        p = session._payment_params()
        paid = await xrpl_ops.wait_for_payment(
            destination=p["destination"],
            expected_sender=session.wallet_address,
            expected_amount=p["value"],
            not_before=session.created_at - 10,
            currency=p["currency"],
            # LFGO mints pay the issuer, which receives nothing else, so an
            # unconsumed earlier payment is always a mint credit (#196). The
            # XRP path pays the busier bot wallet - no credit window there.
            allow_credit=session.pay_with != "XRP",
        )
        if not paid:
            session.state = PAYMENT_TIMEOUT
            return

        # The payment is irrevocably confirmed on-ledger: leave
        # AWAITING_PAYMENT *now*, before any further await (the XRP-path
        # buy_and_burn below is a multi-second submit_and_wait), so a
        # concurrent cancel() can never land on a session whose money has
        # already been taken.
        session.state = GENERATING

        if session.pay_with == "XRP":
            # Buy the mint's LFGO off the DEX and burn it, spending at most
            # the XRP just collected. Best-effort: a failed buyback must
            # never cost the user their mint.
            if not await xrpl_ops.buy_and_burn(
                config.TOKEN_CURRENCY_HEX,
                config.TOKEN_ISSUER_ADDRESS,
                config.MINT_PRICE_LFGO,
                max_xrp=session.pay_amount,
            ):
                logging.error(
                    f"LFGO buy-and-burn failed for mint session "
                    f"{session.id}; XRP stays in the bot wallet"
                )

        # 2-5. Compose -> upload -> mint -> record -> offer -> accept payload,
        # extracted into mint_one_unit (#215) so bulk mint can share the same
        # path. on_state keeps the single-mint session's finer-grained
        # MINTING/CREATING_OFFER states for the UI.
        session.nft_number = await _allocate_nft_number()

        def _on_state(state: str) -> None:
            session.state = state

        async def _on_mint(nft_number: int, nft_id: str, image_url: str | None) -> None:
            # #226 (review): settle the headroom reservation the INSTANT the
            # mint lands, symmetric with bulk's _on_mint — waiting for the
            # session-end finally leaves a window (offer creation + XUMM
            # payload, seconds to tens of seconds) where a hard crash
            # uncounts an on-chain mint: the restart rebuild drops mint:*
            # rows and no pending row exists yet, so a cap-tail reserver
            # could over-admit while the listener lags. settle_headroom sees
            # nft_id set and retires the reservation to the durable pending
            # set; the finally below remains the release-only path for
            # sessions that never minted (idempotent — the flag drops before
            # the store write).
            session.nft_id = nft_id
            settle_headroom(session)

        res = await mint_one_unit(
            discord_id=session.discord_id,
            wallet_address=session.wallet_address,
            platform=session.platform,
            push_user_token=session.push_user_token,
            return_url=session.return_url,
            nft_number=session.nft_number,
            session_tag=session.id,
            on_state=_on_state,
            on_mint=_on_mint,
        )
        session.nft_id = res.nft_id
        session.image_url = res.image_url
        session.video_url = res.video_url
        session.traits = res.traits
        session.body_type = res.body_type
        if res.error or not res.offer_id or not res.accept:
            _release_unused_number(session)
            session.state = FAILED
            session.error = res.error or "mint failed"
            return

        session.accept_qr_url = res.accept["qr_url"]
        session.accept_deeplink = res.accept["xumm_url"]
        session.accept_uuid = res.accept.get("uuid")
        session.accept_push = res.accept.get("push")
        session.state = OFFER_READY

    except Exception as e:
        logging.error(f"Mint session {session.id} failed: {traceback.format_exc()}")
        if session.nft_number is not None and not session.nft_id:
            # Only if the mint never landed — a promoted still stays put.
            image_archive.discard_still(config.XRPL_NETWORK, session.nft_number, session.id)
        _release_unused_number(session)
        session.state = FAILED
        session.error = str(e)
    finally:
        # #226: every exit — OFFER_READY, FAILED, PAYMENT_TIMEOUT, and the
        # CancelledError a cancel() delivers (BaseException, uncatchable
        # above) — settles the session's headroom reservation: retire to
        # pending if the mint landed, release otherwise. For sessions whose
        # mint landed this is a no-op (idempotent): _on_mint already settled
        # at mint-land so a crash mid-offer can't uncount the mint.
        settle_headroom(session)
