# lfg_core/swap_flow.py
# Trait-swap session state machine. Since the Dynamic NFTs amendment there
# are two kinds of collection NFTs, and a swap handles both:
#   - mutable NFTs (lsfMutable): updated IN PLACE via NFTokenModify — the NFT
#     never leaves the user's wallet, so the swap fee is collected upfront
#     with a XUMM BRIX payment before anything touches the chain.
#   - legacy burnable NFTs: burned and reminted (as mutable, per NFT_FLAGS),
#     paying via the BRIX-priced replacement offer as before.
#
# Ordering is fail-safe for the user — reversible steps first, the
# irreversible one last:
#   compose/upload → collect modify fee (if any) →
#   mint replacements (revertible: burn them) →
#   modify mutables (revertible: modify back to the old URI) →
#   burn originals (IRREVERSIBLE) → offers.
# If anything fails before the original burns, replacements are burned back
# and modifies reverted, so the user keeps their originals untouched. Every
# on-chain step is journaled to SWAP_RECORDS_DIR so an administrator can
# recover a partial swap.

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from decimal import ROUND_UP, Decimal
from typing import Any

from xrpl.models import IssuedCurrencyAmount
from xrpl.utils import xrp_to_drops

from lfg_core import (
    cdn,
    config,
    image_archive,
    layer_store,
    memos,
    swap_compose,
    swap_meta,
    traits,
    xrpl_ops,
    xumm_ops,
)

AWAITING_PAYMENT = "awaiting_payment"
COMPOSING = "composing"
UPLOADING = "uploading"
MINTING = "minting"
MODIFYING = "modifying"
BURNING = "burning"
CREATING_OFFERS = "creating_offers"
OFFERS_READY = "offers_ready"
DONE = "done"
FAILED = "failed"
PAYMENT_TIMEOUT = "payment_timeout"
CANCELLED = "cancelled"  # user backed out of the fee-pay screen (mirror of mint #141)

TERMINAL_STATES = {DONE, FAILED, OFFERS_READY, PAYMENT_TIMEOUT, CANCELLED}


def swap_fee_total(modify_count: int) -> str:
    """Upfront BRIX fee for in-place (NFTokenModify) swaps: the same
    per-NFT price the burn path charges via its offers."""
    return str(Decimal(config.SWAP_OFFER_AMOUNT) * modify_count)


async def detect_swap_payment(wallet_address: str, brix_amount: str) -> tuple[str, str]:
    """Silent fee-path detection: wallets holding >= brix_amount BRIX pay in
    BRIX (burned); everyone else pays the live AMM XRP equivalent — the
    buyback is never surfaced to the user. Returns ("BRIX"|"XRP", amount);
    raises if the wallet holds no BRIX and the AMM can't quote a price."""
    balance = await xrpl_ops.get_trustline_balance(
        wallet_address, config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER
    )
    if balance is not None and balance >= Decimal(brix_amount):
        return "BRIX", brix_amount
    cost = await xrpl_ops.get_amm_xrp_cost(
        config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER, Decimal(brix_amount)
    )
    if cost is None:
        raise RuntimeError(
            "Swap fee pricing is unavailable right now — please try again in a moment."
        )
    xrp = cost * Decimal(config.SWAP_XRP_FEE_BUFFER)
    return "XRP", str(xrp.quantize(Decimal("0.000001"), rounding=ROUND_UP))


class SwapSession:
    def __init__(
        self,
        discord_id: str,
        wallet_address: str,
        nft1: dict[str, Any],
        nft2: dict[str, Any],
        traits_to_swap: list[str],
        return_url: dict[str, str] | None = None,
        platform: str = "discord",
        push_user_token: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.return_url = return_url  # XUMM return_url back into Discord
        self.discord_id = discord_id
        self.platform = platform
        self.wallet_address = wallet_address
        # #135: stored XUMM push token for this user, if any — threaded into
        # every sign request this session builds so a returning user gets a
        # push to Xaman instead of a QR. None falls back to the QR/deep link.
        self.push_user_token = push_user_token
        self.created_at = time.time()
        self.nft1 = nft1  # normalized records from swap_meta.normalize_nft
        self.nft2 = nft2
        self.traits_to_swap = traits_to_swap
        self.state = COMPOSING
        self.error: str | None = None
        self.results: list[dict[str, Any]] = []  # one dict per re-crafted NFT
        self.payment_link: str | None = None  # set when an upfront modify fee is due
        self.pay_with: str | None = None  # "BRIX" or "XRP", set at session start
        self.fee_per_nft: Decimal | None = None  # in pay_with units
        self.fee_amount: str | None = None
        # Fee-payload parameters, kept so regenerate_payment can rebuild the
        # sign request after the XUMM payload expires (mirror of mint #22).
        self.fee_destination: str | None = None
        self.fee_currency: str | None = None
        self.fee_issuer: str | None = None
        # Background run_swap_session task handle, so cancel() can stop the
        # payment wait (mirror of mint #141).
        self.task: asyncio.Task[Any] | None = None

    async def regenerate_payment(self) -> None:
        """Replace an expired/missed fee QR with a fresh XUMM payload without
        restarting the swap (mirror of mint issue #22). Keeps the old link if
        XUMM is down — the on-ledger payment wait doesn't care which payload
        (or the static detect link) actually delivers the fee."""
        if self.fee_destination is None or self.fee_amount is None or self.fee_currency is None:
            return  # fee not priced yet — nothing to rebuild
        payload = await xumm_ops.create_payment_payload(
            self.fee_destination,
            value=self.fee_amount,
            currency=self.fee_currency,
            issuer=self.fee_issuer,
            return_url=self.return_url,
            user_token=self.push_user_token,
            platform=memos.platform_for_surface(self.platform),
            action=memos.ACTION_TRAIT_SWAP_FEE,
        )
        if payload:
            self.payment_link = payload["xumm_url"]

    def cancel(self) -> bool:
        """Back out of the fee-pay screen (mirror of mint issue #141): mark
        the session terminal so the one-active-session lock releases, and
        cancel the background task so its payment wait stops. Only legal
        while still awaiting payment — past that the user has paid and the
        pipeline must run to completion or failure. Returns True if
        cancelled.

        The state check and CANCELLED assignment are synchronous (no await
        between them), so on the single event loop this cannot race the
        background task's own transitions — _collect_modify_fee leaves
        AWAITING_PAYMENT in the same synchronous step in which
        wait_for_payment reports the fee confirmed, so a confirmed (paid)
        session can never be cancelled."""
        if self.state != AWAITING_PAYMENT:
            return False
        self.state = CANCELLED
        if self.task is not None:
            # CancelledError is a BaseException, so run_swap_session's
            # `except Exception` cannot catch it and overwrite CANCELLED.
            self.task.cancel()
        return True

    def mark_published(self) -> None:
        """Mark the terminal firehose event as already published (or, for a
        deliberate user cancel, as suppressed) — kept as a method so callers
        never reach into the private guard attribute directly."""
        self._published = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "state": self.state,
            "error": self.error,
            "nft1": {"name": self.nft1["name"], "image": self.nft1["image"]},
            "nft2": {"name": self.nft2["name"], "image": self.nft2["image"]},
            "traits": self.traits_to_swap,
            "results": self.results,
            "payment_link": self.payment_link,
            "pay_with": self.pay_with,
            "fee_amount": self.fee_amount,
        }


async def _upload_swap_file(path_on_cdn: str, data: bytes, content_type: str) -> str:
    return await cdn.upload_to_bunny(config.SWAP_CDN_FOLDER, path_on_cdn, data, content_type)


def _swap_metadata(
    nft: dict[str, Any], attributes: list[dict[str, Any]], image_url: str, video_url: str | None
) -> dict[str, Any]:
    meta = {
        "schema": config.NFT_SCHEMA_URL,
        "name": nft["name"],
        "description": f"Season {nft['season']}",
        "image": image_url,
        "external_link": config.EXTERNAL_WEBSITE_URL,
        "collection": {
            "name": config.NFT_COLLECTION_NAME,
            "family": f"Season {nft['season']}",
            "image": config.NFT_COLLECTION_LOGO,
        },
        "edition": nft["number"],
        # Kept on the modify path too: it doubles as the revision counter
        # that keeps CDN filenames unique across successive swaps.
        "burnCount": nft["burn_count"] + 1,
        "attributes": attributes,
    }
    if video_url:
        meta["video"] = video_url
    return meta


async def _build_and_upload(
    nft: dict[str, Any], attributes: list[dict[str, Any]], store: Any, token: str
) -> tuple[str, str | None, int]:
    """Compose the re-crafted NFT and upload image/video; returns
    (image_url, video_url, new_burn_count)."""
    new_burn = nft["burn_count"] + 1
    path, is_video = await swap_compose.compose_nft(
        attributes, nft["gender"], store, f"{nft['number']}_{new_burn}"
    )
    num = nft["number"]
    image_url, video_url = await swap_compose.upload_output(
        path,
        is_video,
        _upload_swap_file,
        f"{num}/{num}_{new_burn}",
        keep_still=image_archive.pending_still_path(config.XRPL_NETWORK, num, token),
    )
    return image_url, video_url, new_burn


def _archive_stills(items: list[dict[str, Any]], token: str) -> None:
    """Promote each finalized item's staged still into the local image
    archive (#163) so /api/img serves the post-swap art immediately.
    Best-effort; runs after the on-chain outcome for the item is final."""
    for item in items:
        image_archive.promote_still(config.XRPL_NETWORK, item["nft"]["number"], token)


def _discard_stills(items: list[dict[str, Any]], token: str) -> None:
    """Drop staged stills for items whose swap did not finalize."""
    for item in items:
        image_archive.discard_still(config.XRPL_NETWORK, item["nft"]["number"], token)


def _write_swap_record(session: SwapSession, items: list[dict[str, Any]], status: str) -> None:
    """Journal the swap's on-chain progress to disk (survives restarts;
    the in-memory session does not)."""
    try:
        os.makedirs(config.SWAP_RECORDS_DIR, exist_ok=True)
        path = os.path.join(config.SWAP_RECORDS_DIR, f"{session.id}.json")
        record = {
            "session_id": session.id,
            "discord_id": session.discord_id,
            "wallet": session.wallet_address,
            "traits_swapped": session.traits_to_swap,
            "fee_amount": session.fee_amount,  # upfront BRIX fee, if charged
            "status": status,
            "updated_at": time.time(),
            "nfts": [
                {
                    "name": it["nft"]["name"],
                    "number": it["nft"]["number"],
                    "mode": "modify" if it["nft"].get("mutable") else "remint",
                    "old_nft_id": it["nft"]["nft_id"],
                    "old_uri_hex": it["nft"].get("uri_hex"),
                    "new_nft_id": it.get("new_nft_id"),
                    "burn_hash": it.get("burn_hash"),
                    "modify_hash": it.get("modify_hash"),
                    "reverted": it.get("reverted", False),
                    "offer_id": it.get("offer_id"),
                    "metadata_url": it["metadata_url"],
                    "image_url": it["image_url"],
                }
                for it in items
            ],
        }
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
    except Exception:
        logging.error(f"Failed to write swap record: {traceback.format_exc()}")


async def _burn_replacements(items: list[dict[str, Any]]) -> None:
    """Best-effort cleanup: burn re-minted replacements that will never be
    offered (they sit in the issuer wallet, so failure harms no user)."""
    for item in items:
        nft_id = item.get("new_nft_id")
        if not nft_id:
            continue
        if await xrpl_ops.burn_nft(nft_id):
            item["new_nft_id"] = None
        else:
            logging.error(
                f"Cleanup burn failed for replacement {nft_id}; it remains in the issuer wallet"
            )


async def _revert_modifies(items: list[dict[str, Any]], owner: str) -> None:
    """Best-effort rollback: point already-modified NFTs back at their
    original URI. A failed revert leaves the NFT with the new traits — the
    user lost nothing of value, but the journal flags it for an admin."""
    for item in items:
        if not item.get("modify_hash"):
            continue
        old_uri_hex = item["nft"].get("uri_hex") or ""
        if not old_uri_hex or not old_uri_hex.strip():
            logging.warning(
                f"Skipping revert for {item['nft']['nft_id']}: "
                "original URI hex is empty or whitespace"
            )
            continue
        try:
            old_uri = bytes.fromhex(old_uri_hex).decode("ascii")
        except ValueError:
            logging.error(f"Cannot revert modify for {item['nft']['nft_id']}: bad original URI hex")
            continue
        if await xrpl_ops.modify_nft(item["nft"]["nft_id"], owner, old_uri):
            item["modify_hash"] = None
            item["reverted"] = True
        else:
            logging.error(f"Revert modify failed for {item['nft']['nft_id']}; it keeps the new URI")


def _offer_amount(session: SwapSession) -> str | IssuedCurrencyAmount:
    """Replacement-offer price on the session's fee path: BRIX for holders,
    the AMM XRP equivalent (in drops) for everyone else."""
    if session.pay_with == "XRP":
        return xrp_to_drops(session.fee_per_nft or Decimal(0))
    return xrpl_ops.swap_offer_amount()


async def _create_offer_and_accept(session: SwapSession, item: dict[str, Any]) -> bool:
    """Offer one replacement back to the user (priced on the session's fee
    path) and append the XUMM accept payload to session.results. Returns
    False on failure."""
    # When the recipient IS the issuer/signing account, the reminted token was
    # minted straight into their wallet: mint_nft (xrpl_ops) always mints with
    # Account=config.SIGNING_ACCOUNT, so the new token is owned by
    # SIGNING_ACCOUNT regardless of the regular-key signer. A sell offer whose
    # Account == Destination is invalid (temREDUNDANT) and there is nothing to
    # accept — the token is already delivered (this is the common testnet case,
    # where the swapper registered the mint wallet itself as their own). Record
    # it as delivered instead of failing with the misleading "offer failed —
    # contact an administrator".
    if session.wallet_address == config.SIGNING_ACCOUNT:
        item["offer_id"] = None
        session.results.append(
            {
                "name": item["nft"]["name"],
                "nft_id": item["new_nft_id"],
                "image_url": item["image_url"],
                "video_url": item["video_url"],
                "metadata_url": item["metadata_url"],
                # No accept step: the issuer already holds the reminted token.
                # `modified` drives every surface's "already in your wallet —
                # no action needed" branch, which is the accurate UX here too.
                "modified": True,
            }
        )
        return True

    offer_id = await xrpl_ops.create_nft_offer(
        item["new_nft_id"],
        session.wallet_address,
        amount=_offer_amount(session),
        platform=memos.platform_for_surface(session.platform),
    )
    if not offer_id:
        session.error = (
            f"{item['nft']['name']} was reminted "
            f"({item['new_nft_id']}) but the offer failed — "
            "contact an administrator."
        )
        return False
    item["offer_id"] = offer_id
    accept = await xumm_ops.create_accept_offer_payload(
        offer_id,
        return_url=session.return_url,
        user_token=session.push_user_token,
        platform=memos.platform_for_surface(session.platform),
    )
    if not accept:
        session.error = (
            f"Offer {offer_id} created for {item['nft']['name']} "
            "but the XUMM request failed — accept it manually."
        )
        return False
    session.results.append(
        {
            "name": item["nft"]["name"],
            "nft_id": item["new_nft_id"],
            "image_url": item["image_url"],
            "video_url": item["video_url"],
            "metadata_url": item["metadata_url"],
            "modified": False,
            "accept_qr_url": accept["qr_url"],
            "accept_deeplink": accept["xumm_url"],
        }
    )
    return True


async def _collect_modify_fee(session: SwapSession, modify_count: int) -> bool:
    """Charge the upfront fee for in-place swaps via XUMM (in BRIX or its
    AMM XRP equivalent, per the session's detected path) and wait for the
    verified payment. Returns False on timeout/failure."""
    if session.pay_with == "XRP":
        fee = str((session.fee_per_nft or Decimal(0)) * modify_count)
        currency, issuer = "XRP", None
    else:
        fee = swap_fee_total(modify_count)
        currency, issuer = config.SWAP_OFFER_CURRENCY_HEX, config.SWAP_OFFER_ISSUER
    destination = xrpl_ops.bot_wallet_address()
    session.fee_amount = fee
    # Keep the payload parameters on the session so regenerate_payment can
    # rebuild an expired QR without restarting the swap (mirror of mint #22).
    session.fee_destination = destination
    session.fee_currency = currency
    session.fee_issuer = issuer
    await session.regenerate_payment()
    if session.payment_link is None:
        # Sign-request payload normally; raw detect link only if XUMM is down
        session.payment_link = xumm_ops.generate_static_payment_link(
            destination, value=fee, currency=currency, issuer=issuer
        )
    session.state = AWAITING_PAYMENT
    paid = await xrpl_ops.wait_for_payment(
        destination=destination,
        expected_sender=session.wallet_address,
        expected_amount=fee,
        not_before=session.created_at - 10,
        currency=currency,
        issuer=issuer,
    )
    if paid:
        # Leave AWAITING_PAYMENT in the SAME synchronous step the payment is
        # confirmed: buy_and_burn awaits below, and cancel() only guards on
        # state — a cancel landing in that window must not kill a PAID swap.
        # run_swap_session re-stamps the real next stage right after.
        session.state = COMPOSING
        # Burn the fee's BRIX: holders' BRIX is forwarded straight to the
        # issuer; XRP fees fund an AMM buy first (capped at the XRP just
        # collected). Best-effort — a failed burn must not block the swap.
        if not await xrpl_ops.buy_and_burn(
            config.SWAP_OFFER_CURRENCY_HEX,
            config.SWAP_OFFER_ISSUER,
            swap_fee_total(modify_count),
            max_xrp=fee if session.pay_with == "XRP" else None,
        ):
            logging.error(
                f"BRIX fee burn failed for swap session {session.id}; fee stays in the bot wallet"
            )
    return paid


async def run_swap_session(session: SwapSession) -> None:
    """Drive a SwapSession to a terminal state. Run as a background task."""
    try:
        nft1, nft2 = session.nft1, session.nft2
        new_attrs1, new_attrs2 = swap_meta.swap_traits(
            nft1["attributes"], nft2["attributes"], session.traits_to_swap
        )

        # Legacy ape faces (#168): fill any still-empty face slot via the
        # rarity engine — after swap application (a real face moved in by
        # the swap is never overwritten), before the layer pre-check (rolled
        # art gets the same existence check as everything else). Skeletons
        # and other bodies are a no-op inside the helper.
        store = layer_store.get_layer_store()
        await traits.fill_missing_face_traits(store, nft1["gender"], new_attrs1)
        await traits.fill_missing_face_traits(store, nft2["gender"], new_attrs2)

        # 0. Verify every layer exists in the store BEFORE taking payment or
        #    touching anything on-chain (also pre-warms the download cache)
        missing = await swap_compose.missing_layers(
            new_attrs1, nft1["gender"], store
        ) + await swap_compose.missing_layers(new_attrs2, nft2["gender"], store)
        if missing:
            session.state = FAILED
            session.error = f"Missing trait layer files: {', '.join(missing)}"
            return

        # Detect the fee path up front: it prices the modify fee AND the
        # replacement offers, so even burn-only swaps need it.
        session.pay_with, total = await detect_swap_payment(
            session.wallet_address, swap_fee_total(2)
        )
        # Re-quantize: XRP amounts must not exceed 6 decimal places (drops)
        session.fee_per_nft = (Decimal(total) / 2).quantize(Decimal("0.000001"), rounding=ROUND_UP)

        items: list[dict[str, Any]] = [
            {"nft": nft1, "attrs": new_attrs1},
            {"nft": nft2, "attrs": new_attrs2},
        ]
        modify_items = [it for it in items if it["nft"].get("mutable")]
        burn_items = [it for it in items if not it["nft"].get("mutable")]

        # 1. Compose and upload both new NFTs (images + metadata)
        session.state = COMPOSING
        for item in items:
            nft, attrs = item["nft"], item["attrs"]
            image_url, video_url, new_burn = await _build_and_upload(nft, attrs, store, session.id)
            session.state = UPLOADING
            meta = _swap_metadata(nft, attrs, image_url, video_url)
            meta_url = await _upload_swap_file(
                f"{nft['number']}/{nft['number']}_{new_burn}.json",
                json.dumps(meta, indent=2).encode(),
                "application/json",
            )
            item.update(image_url=image_url, video_url=video_url, metadata_url=meta_url)

        # 2. In-place swaps have no priced offer to accept, so their fee is
        #    a verified upfront BRIX payment — collected after the off-chain
        #    work but before anything touches the chain, so the window where
        #    a user has paid for a failed swap is as small as possible.
        if modify_items:
            if not await _collect_modify_fee(session, len(modify_items)):
                _discard_stills(items, session.id)
                session.state = PAYMENT_TIMEOUT
                session.error = "No swap fee payment was received in time. Your NFTs are untouched."
                return
            _write_swap_record(session, items, "fee_paid")

        # 3. Mint replacements for the burnable originals FIRST — if this
        #    fails, nothing user-visible has changed.
        if burn_items:
            session.state = MINTING
            _write_swap_record(session, items, "minting")
            for item in burn_items:
                nft_id = await xrpl_ops.mint_nft(
                    metadata_cdn_url=item["metadata_url"],
                    taxon=config.SWAP_TAXON,
                    issuer=config.SWAP_ISSUER_ADDRESS,
                    platform=memos.platform_for_surface(session.platform),
                )
                if not nft_id:
                    await _burn_replacements(burn_items)
                    _discard_stills(items, session.id)
                    _write_swap_record(session, items, "failed_minting")
                    session.error = (
                        f"Reminting {item['nft']['name']} failed. "
                        "No NFTs were lost — your originals are "
                        "untouched. Try again later."
                    )
                    session.state = FAILED
                    return
                item["new_nft_id"] = nft_id
            _write_swap_record(session, items, "minted")

        # 4. Modify the mutable NFTs in place. Still revertible: on failure,
        #    completed modifies are pointed back at their old URI and any
        #    minted replacements are burned — the user keeps their originals.
        if modify_items:
            session.state = MODIFYING
            _write_swap_record(session, items, "modifying")
            for item in modify_items:
                modify_hash = await xrpl_ops.modify_nft(
                    item["nft"]["nft_id"],
                    session.wallet_address,
                    item["metadata_url"],
                    platform=memos.platform_for_surface(session.platform),
                )
                if not modify_hash:
                    await _revert_modifies(modify_items, session.wallet_address)
                    await _burn_replacements(burn_items)
                    _discard_stills(items, session.id)
                    _write_swap_record(session, items, "failed_modifying")
                    session.error = (
                        f"Updating {item['nft']['name']} on-chain "
                        "failed. No NFTs were lost — your "
                        "originals are untouched. Try again later."
                    )
                    session.state = FAILED
                    return
                item["modify_hash"] = modify_hash
            _write_swap_record(session, items, "modified")

        # 5. Burn the burnable originals — the irreversible step, done last.
        if burn_items:
            session.state = BURNING
            for i, item in enumerate(burn_items):
                burn_hash = await xrpl_ops.burn_nft(
                    item["nft"]["nft_id"],
                    session.wallet_address,
                    platform=memos.platform_for_surface(session.platform),
                )
                if burn_hash:
                    item["burn_hash"] = burn_hash
                    continue

                if i == 0:
                    # Nothing burned yet: unwind everything (replacements
                    # burned, modifies reverted); the user keeps both
                    # originals exactly as they were.
                    await _revert_modifies(modify_items, session.wallet_address)
                    await _burn_replacements(burn_items)
                    _discard_stills(items, session.id)
                    _write_swap_record(session, items, "failed_burning")
                    session.error = (
                        f"Failed to burn {item['nft']['name']} "
                        f"({item['nft']['nft_id']}). No NFTs were "
                        "lost — contact an administrator."
                    )
                    session.state = FAILED
                    return

                # Both originals were burnable (no modifies in play) and the
                # first is already gone, so its replacement MUST reach the
                # user. Cancel only the second half of the swap.
                other = burn_items[0]
                await _burn_replacements([item])
                # `other` is final (original burned, replacement live) even if
                # its offer fails below; `item` reverted.
                _archive_stills([other], session.id)
                _discard_stills([item], session.id)
                _write_swap_record(session, items, "partial_burn_failure")
                if not await _create_offer_and_accept(session, other):
                    _write_swap_record(session, items, "partial_burn_failure_no_offer")
                    session.state = FAILED
                    return
                session.error = (
                    f"{item['nft']['name']} could not be burned, so "
                    "its traits were not swapped (it is still in "
                    f"your wallet). {other['nft']['name']} was "
                    "re-crafted — accept it below, then contact an "
                    "administrator."
                )
                _write_swap_record(session, items, "partial")
                session.state = FAILED
                return
            _write_swap_record(session, items, "burned")

        # Everything is final on-chain now (a failed offer below doesn't
        # change which token/art is live) — publish the new stills locally.
        _archive_stills(items, session.id)

        # The modifies are final now that the burns are through.
        for item in modify_items:
            session.results.append(
                {
                    "name": item["nft"]["name"],
                    "nft_id": item["nft"]["nft_id"],  # unchanged by NFTokenModify
                    "image_url": item["image_url"],
                    "video_url": item["video_url"],
                    "metadata_url": item["metadata_url"],
                    "modified": True,
                }
            )

        # 6. Offers for the reminted NFTs (BRIX-priced, as the original
        #    swapper) + XUMM accept links
        if burn_items:
            session.state = CREATING_OFFERS
            for item in burn_items:
                if not await _create_offer_and_accept(session, item):
                    _write_swap_record(session, items, "failed_offers")
                    session.state = FAILED
                    return

        _write_swap_record(session, items, "complete")
        session.state = OFFERS_READY

    except asyncio.CancelledError:
        # User backed out at the fee screen (cancel() already set CANCELLED
        # and this task was cancelled before any chain mutation). Compose ran
        # before the fee screen, so clean up the pending stills like any
        # other unfinished swap, then let the cancellation propagate.
        for nft in (session.nft1, session.nft2):
            image_archive.discard_still(config.XRPL_NETWORK, nft["number"], session.id)
        raise
    except Exception as e:
        logging.error(f"Swap session {session.id} failed: {traceback.format_exc()}")
        # Already-promoted stills are untouched (discard only sees pending/).
        for nft in (session.nft1, session.nft2):
            image_archive.discard_still(config.XRPL_NETWORK, nft["number"], session.id)
        session.state = FAILED
        session.error = str(e)
