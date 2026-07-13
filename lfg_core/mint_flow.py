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
from decimal import Decimal
from typing import Any

from lfg_core import (
    cdn,
    config,
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
        self.qr_scanned = False  # payment QR opened in Xaman (issue #22)
        self.nft_number: int | None = None
        self.nft_id: str | None = None
        self.image_url: str | None = None
        self.accept_qr_url: str | None = None
        self.accept_deeplink: str | None = None
        self.accept_uuid: str | None = None
        self.accept_scanned = False
        self.accept_signed = False
        # The run_mint_session background task, set by the service after it
        # spawns it, so cancel() can stop the payment wait promptly (#141).
        self.task: asyncio.Task[None] | None = None
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
        up in the payment QR (issue #8)."""
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

    async def regenerate_payment(self) -> None:
        """Replace an expired/missed payment QR with a fresh XUMM payload
        without restarting the whole session (issue #22)."""
        self.qr_scanned = False
        self.payment_uuid = None
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
            "qr_scanned": self.qr_scanned,
            "accept_scanned": self.accept_scanned,
            "accept_signed": self.accept_signed,
            "nft_number": self.nft_number,
            "nft_id": self.nft_id,
            "image_url": self.image_url,
            "accept_qr_url": self.accept_qr_url,
            "accept_deeplink": self.accept_deeplink,
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


async def update_scan_state(session: MintSession) -> None:
    """Refresh the session's QR-scan flags from the XUMM payload status so
    the frontend can swap a scanned QR for a spinner (issue #22). Queries
    stop once a payload is seen opened/signed; API errors leave the flags
    untouched."""
    if session.state == AWAITING_PAYMENT and session.payment_uuid and not session.qr_scanned:
        s = await xumm_ops.get_payload_status(session.payment_uuid)
        if s:
            session.qr_scanned = s["opened"] or s["signed"]
    elif session.state == OFFER_READY and session.accept_uuid and not session.accept_signed:
        s = await xumm_ops.get_payload_status(session.accept_uuid)
        if s:
            session.accept_scanned = s["opened"] or s["signed"]
            session.accept_signed = s["signed"]


async def run_mint_session(session: MintSession) -> None:
    """Drive a MintSession to a terminal state. Run as a background task."""
    try:
        # 1. Wait for the sender-verified payment on whichever path
        #    prepare_payment detected. not_before bounds the missed-payment
        #    backfill to this session's lifetime.
        session.ensure_payment_fallback()
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

        # 2. Compose a random NFT from the unified layer store (same tree
        #    the Trait Swapper uses: <gender>/<TraitType>/<Value>.ext)
        session.nft_number = await _allocate_nft_number()
        store = layer_store.get_layer_store()
        body, attributes = await traits.select_random_attributes(store)
        output_path, is_video = await swap_compose.compose_nft(
            attributes, body, store, f"lfg_{session.nft_number}"
        )

        # 3. Upload image (+ video) and metadata to BunnyCDN. The still is
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
            f"{session.nft_number}/{session.nft_number}_0",
            keep_still=image_archive.pending_still_path(
                config.XRPL_NETWORK, session.nft_number, session.id
            ),
        )
        session.image_url = image_cdn_url

        metadata: dict[str, Any] = {
            "name": f"{config.NFT_COLLECTION_NAME} #{session.nft_number}",
            "image": image_cdn_url,
            "edition": session.nft_number,
            "attributes": attributes,
        }
        if video_cdn_url:
            metadata["video"] = video_cdn_url
        metadata_cdn_url = await _upload_to_bunny(
            f"{session.nft_number}/{session.nft_number}_0.json",
            json.dumps(metadata, indent=2).encode(),
            "application/json",
        )

        # 4. Mint on XRPL
        session.state = MINTING
        # Issuer is the NFT collection issuer (same account every other mint
        # path uses) — NOT the LFGO token issuer. On mainnet those differ, and
        # passing the token issuer makes mint_nft add an Issuer field = an
        # unauthorized mint-on-behalf, tecNO_PERMISSION on every attempt.
        nft_id = await xrpl_ops.mint_nft(
            metadata_cdn_url=metadata_cdn_url,
            taxon=config.NFT_TAXON,
            issuer=config.SWAP_ISSUER_ADDRESS,
            platform=memos.platform_for_surface(session.platform),
        )
        if not nft_id:
            image_archive.discard_still(config.XRPL_NETWORK, session.nft_number, session.id)
            _release_unused_number(session)
            session.state = FAILED
            session.error = "Failed to mint NFT on XRPL. Please contact an administrator."
            return
        session.nft_id = nft_id
        # Mint confirmed — publish the new edition's art to the local archive
        # so /api/img serves it immediately (best-effort, #163).
        image_archive.promote_still(config.XRPL_NETWORK, session.nft_number, session.id)

        traits_dict = {t["trait_type"]: t["value"] for t in metadata["attributes"]}
        # The LFG table's headwear column is named Hat (layer tree uses Head)
        if "Head" in traits_dict:
            traits_dict["Hat"] = traits_dict.pop("Head")
        record: dict[str, Any] = {
            "nft_number": session.nft_number,
            "nft_id": nft_id,
            "discord_id": session.discord_id,
            "owner_address": session.wallet_address,
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
            _reserved_numbers.discard(session.nft_number)

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

        # 5. Create the transfer offer and the XUMM accept payload
        session.state = CREATING_OFFER
        offer_id = await xrpl_ops.create_nft_offer(
            nft_id, session.wallet_address, platform=memos.platform_for_surface(session.platform)
        )
        if not offer_id:
            session.state = FAILED
            session.error = (
                f"NFT minted (ID: {nft_id}) but offer creation failed. "
                "Please contact an administrator."
            )
            return

        accept = await xumm_ops.create_accept_offer_payload(
            offer_id,
            return_url=session.return_url,
            user_token=session.push_user_token,
            platform=memos.platform_for_surface(session.platform),
        )
        if not accept:
            session.state = FAILED
            session.error = (
                f"NFT minted and offer created ({offer_id}) but the XUMM "
                "request failed. Please accept the offer manually."
            )
            return

        session.accept_qr_url = accept["qr_url"]
        session.accept_deeplink = accept["xumm_url"]
        session.accept_uuid = accept.get("uuid")
        session.state = OFFER_READY

    except Exception as e:
        logging.error(f"Mint session {session.id} failed: {traceback.format_exc()}")
        if session.nft_number is not None and not session.nft_id:
            # Only if the mint never landed — a promoted still stays put.
            image_archive.discard_still(config.XRPL_NETWORK, session.nft_number, session.id)
        _release_unused_number(session)
        session.state = FAILED
        session.error = str(e)
