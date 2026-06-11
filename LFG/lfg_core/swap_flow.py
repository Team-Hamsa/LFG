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

import os
import json
import time
import uuid
import logging
import traceback
from decimal import Decimal

from lfg_core import config, cdn, xrpl_ops, xumm_ops, swap_meta, swap_compose, layer_store

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

TERMINAL_STATES = {DONE, FAILED, OFFERS_READY, PAYMENT_TIMEOUT}


def swap_fee_total(modify_count: int) -> str:
    """Upfront BRIX fee for in-place (NFTokenModify) swaps: the same
    per-NFT price the burn path charges via its offers."""
    return str(Decimal(config.SWAP_OFFER_AMOUNT) * modify_count)


class SwapSession:
    def __init__(self, discord_id: str, wallet_address: str,
                 nft1: dict, nft2: dict, traits_to_swap: list):
        self.id = uuid.uuid4().hex
        self.discord_id = discord_id
        self.wallet_address = wallet_address
        self.created_at = time.time()
        self.nft1 = nft1  # normalized records from swap_meta.normalize_nft
        self.nft2 = nft2
        self.traits_to_swap = traits_to_swap
        self.state = COMPOSING
        self.error = None
        self.results = []  # one dict per re-crafted NFT
        self.payment_link = None  # set when an upfront modify fee is due
        self.fee_amount = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "nft1": {"name": self.nft1["name"], "image": self.nft1["image"]},
            "nft2": {"name": self.nft2["name"], "image": self.nft2["image"]},
            "traits": self.traits_to_swap,
            "results": self.results,
            "payment_link": self.payment_link,
            "fee_amount": self.fee_amount,
        }


async def _upload_swap_file(path_on_cdn: str, data: bytes, content_type: str) -> str:
    return await cdn.upload_to_bunny(config.SWAP_CDN_FOLDER, path_on_cdn,
                                     data, content_type)


def _swap_metadata(nft: dict, attributes: list, image_url: str, video_url):
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


async def _build_and_upload(nft: dict, attributes: list, store):
    """Compose the re-crafted NFT and upload image/video; returns
    (image_url, video_url, new_burn_count)."""
    new_burn = nft["burn_count"] + 1
    path, is_video = await swap_compose.compose_nft(
        attributes, nft["gender"], store, f"{nft['number']}_{new_burn}")
    num = nft["number"]
    image_url, video_url = await swap_compose.upload_output(
        path, is_video, _upload_swap_file, f"{num}/{num}_{new_burn}")
    return image_url, video_url, new_burn


def _write_swap_record(session: SwapSession, items: list, status: str) -> None:
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
            "nfts": [{
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
            } for it in items],
        }
        with open(path, "w") as f:
            json.dump(record, f, indent=2)
    except Exception:
        logging.error(f"Failed to write swap record: {traceback.format_exc()}")


async def _burn_replacements(items: list) -> None:
    """Best-effort cleanup: burn re-minted replacements that will never be
    offered (they sit in the issuer wallet, so failure harms no user)."""
    for item in items:
        nft_id = item.get("new_nft_id")
        if not nft_id:
            continue
        if await xrpl_ops.burn_nft(nft_id):
            item["new_nft_id"] = None
        else:
            logging.error(f"Cleanup burn failed for replacement {nft_id}; "
                          "it remains in the issuer wallet")


async def _revert_modifies(items: list, owner: str) -> None:
    """Best-effort rollback: point already-modified NFTs back at their
    original URI. A failed revert leaves the NFT with the new traits — the
    user lost nothing of value, but the journal flags it for an admin."""
    for item in items:
        if not item.get("modify_hash"):
            continue
        old_uri_hex = item["nft"].get("uri_hex") or ""
        try:
            old_uri = bytes.fromhex(old_uri_hex).decode("ascii")
        except ValueError:
            logging.error(f"Cannot revert modify for {item['nft']['nft_id']}: "
                          "bad original URI hex")
            continue
        if await xrpl_ops.modify_nft(item["nft"]["nft_id"], owner, old_uri):
            item["modify_hash"] = None
            item["reverted"] = True
        else:
            logging.error(f"Revert modify failed for {item['nft']['nft_id']}; "
                          "it keeps the new URI")


async def _create_offer_and_accept(session: SwapSession, item: dict) -> bool:
    """Offer one replacement back to the user (BRIX-priced) and append the
    XUMM accept payload to session.results. Returns False on failure."""
    offer_id = await xrpl_ops.create_nft_offer(
        item["new_nft_id"], session.wallet_address,
        amount=xrpl_ops.swap_offer_amount())
    if not offer_id:
        session.error = (f"{item['nft']['name']} was reminted "
                         f"({item['new_nft_id']}) but the offer failed — "
                         "contact an administrator.")
        return False
    item["offer_id"] = offer_id
    accept = await xumm_ops.create_accept_offer_payload(offer_id)
    if not accept:
        session.error = (f"Offer {offer_id} created for {item['nft']['name']} "
                         "but the XUMM request failed — accept it manually.")
        return False
    session.results.append({
        "name": item["nft"]["name"],
        "nft_id": item["new_nft_id"],
        "image_url": item["image_url"],
        "video_url": item["video_url"],
        "metadata_url": item["metadata_url"],
        "modified": False,
        "accept_qr_url": accept["qr_url"],
        "accept_deeplink": accept["xumm_url"],
    })
    return True


async def _collect_modify_fee(session: SwapSession, modify_count: int) -> bool:
    """Charge the upfront BRIX fee for in-place swaps via XUMM and wait for
    the verified payment. Returns False on timeout/failure."""
    fee = swap_fee_total(modify_count)
    destination = xrpl_ops.bot_wallet_address()
    session.fee_amount = fee
    session.payment_link = xumm_ops.generate_static_payment_link(
        destination, value=fee,
        currency=config.SWAP_OFFER_CURRENCY_HEX,
        issuer=config.SWAP_OFFER_ISSUER)
    session.state = AWAITING_PAYMENT
    return await xrpl_ops.wait_for_payment(
        destination=destination,
        expected_sender=session.wallet_address,
        expected_amount=fee,
        not_before=session.created_at - 10,
        currency=config.SWAP_OFFER_CURRENCY_HEX,
        issuer=config.SWAP_OFFER_ISSUER)


async def run_swap_session(session: SwapSession) -> None:
    """Drive a SwapSession to a terminal state. Run as a background task."""
    try:
        nft1, nft2 = session.nft1, session.nft2
        new_attrs1, new_attrs2 = swap_meta.swap_traits(
            nft1["attributes"], nft2["attributes"], session.traits_to_swap)

        # 0. Verify every layer exists in the store BEFORE taking payment or
        #    touching anything on-chain (also pre-warms the download cache)
        store = layer_store.get_layer_store()
        missing = (await swap_compose.missing_layers(new_attrs1, nft1["gender"], store)
                   + await swap_compose.missing_layers(new_attrs2, nft2["gender"], store))
        if missing:
            session.state = FAILED
            session.error = f"Missing trait layer files: {', '.join(missing)}"
            return

        items = [{"nft": nft1, "attrs": new_attrs1},
                 {"nft": nft2, "attrs": new_attrs2}]
        modify_items = [it for it in items if it["nft"].get("mutable")]
        burn_items = [it for it in items if not it["nft"].get("mutable")]

        # 1. Compose and upload both new NFTs (images + metadata)
        session.state = COMPOSING
        for item in items:
            nft, attrs = item["nft"], item["attrs"]
            image_url, video_url, new_burn = await _build_and_upload(nft, attrs, store)
            session.state = UPLOADING
            meta = _swap_metadata(nft, attrs, image_url, video_url)
            meta_url = await _upload_swap_file(
                f"{nft['number']}/{nft['number']}_{new_burn}.json",
                json.dumps(meta, indent=2).encode(), "application/json")
            item.update(image_url=image_url, video_url=video_url,
                        metadata_url=meta_url)

        # 2. In-place swaps have no priced offer to accept, so their fee is
        #    a verified upfront BRIX payment — collected after the off-chain
        #    work but before anything touches the chain, so the window where
        #    a user has paid for a failed swap is as small as possible.
        if modify_items:
            if not await _collect_modify_fee(session, len(modify_items)):
                session.state = PAYMENT_TIMEOUT
                session.error = ("No swap fee payment was received in time. "
                                 "Your NFTs are untouched.")
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
                )
                if not nft_id:
                    await _burn_replacements(burn_items)
                    _write_swap_record(session, items, "failed_minting")
                    session.error = (f"Reminting {item['nft']['name']} failed. "
                                     "No NFTs were lost — your originals are "
                                     "untouched. Try again later.")
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
                    item["nft"]["nft_id"], session.wallet_address,
                    item["metadata_url"])
                if not modify_hash:
                    await _revert_modifies(modify_items, session.wallet_address)
                    await _burn_replacements(burn_items)
                    _write_swap_record(session, items, "failed_modifying")
                    session.error = (f"Updating {item['nft']['name']} on-chain "
                                     "failed. No NFTs were lost — your "
                                     "originals are untouched. Try again later.")
                    session.state = FAILED
                    return
                item["modify_hash"] = modify_hash
            _write_swap_record(session, items, "modified")

        # 5. Burn the burnable originals — the irreversible step, done last.
        if burn_items:
            session.state = BURNING
            for i, item in enumerate(burn_items):
                burn_hash = await xrpl_ops.burn_nft(item["nft"]["nft_id"],
                                                    session.wallet_address)
                if burn_hash:
                    item["burn_hash"] = burn_hash
                    continue

                if i == 0:
                    # Nothing burned yet: unwind everything (replacements
                    # burned, modifies reverted); the user keeps both
                    # originals exactly as they were.
                    await _revert_modifies(modify_items, session.wallet_address)
                    await _burn_replacements(burn_items)
                    _write_swap_record(session, items, "failed_burning")
                    session.error = (f"Failed to burn {item['nft']['name']} "
                                     f"({item['nft']['nft_id']}). No NFTs were "
                                     "lost — contact an administrator.")
                    session.state = FAILED
                    return

                # Both originals were burnable (no modifies in play) and the
                # first is already gone, so its replacement MUST reach the
                # user. Cancel only the second half of the swap.
                other = burn_items[0]
                await _burn_replacements([item])
                _write_swap_record(session, items, "partial_burn_failure")
                if not await _create_offer_and_accept(session, other):
                    _write_swap_record(session, items, "partial_burn_failure_no_offer")
                    session.state = FAILED
                    return
                session.error = (f"{item['nft']['name']} could not be burned, so "
                                 "its traits were not swapped (it is still in "
                                 f"your wallet). {other['nft']['name']} was "
                                 "re-crafted — accept it below, then contact an "
                                 "administrator.")
                _write_swap_record(session, items, "partial")
                session.state = FAILED
                return
            _write_swap_record(session, items, "burned")

        # The modifies are final now that the burns are through.
        for item in modify_items:
            session.results.append({
                "name": item["nft"]["name"],
                "nft_id": item["nft"]["nft_id"],  # unchanged by NFTokenModify
                "image_url": item["image_url"],
                "video_url": item["video_url"],
                "metadata_url": item["metadata_url"],
                "modified": True,
            })

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

    except Exception as e:
        logging.error(f"Swap session {session.id} failed: {traceback.format_exc()}")
        session.state = FAILED
        session.error = str(e)
