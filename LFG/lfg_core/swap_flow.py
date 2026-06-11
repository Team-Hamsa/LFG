# lfg_core/swap_flow.py
# Trait-swap session state machine: compose both re-crafted NFTs → upload to
# CDN → mint the replacements → burn the originals → create BRIX-priced
# offers → XUMM accept payloads. Same polling pattern as mint_flow.
#
# Ordering is fail-safe for the user: nothing is burned until both
# replacements are already minted (the issuer wallet has burn authority over
# the originals, so a user can't block the burn after receiving offers). If
# anything fails before the burns, the replacements are burned back and the
# user keeps both originals. Every on-chain step is journaled to
# SWAP_RECORDS_DIR so an administrator can recover a partial swap.

import os
import json
import time
import uuid
import logging
import traceback

from lfg_core import config, cdn, xrpl_ops, xumm_ops, swap_meta, swap_compose, layer_store

COMPOSING = "composing"
UPLOADING = "uploading"
MINTING = "minting"
BURNING = "burning"
CREATING_OFFERS = "creating_offers"
OFFERS_READY = "offers_ready"
DONE = "done"
FAILED = "failed"

TERMINAL_STATES = {DONE, FAILED, OFFERS_READY}


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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "nft1": {"name": self.nft1["name"], "image": self.nft1["image"]},
            "nft2": {"name": self.nft2["name"], "image": self.nft2["image"]},
            "traits": self.traits_to_swap,
            "results": self.results,
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
            "status": status,
            "updated_at": time.time(),
            "nfts": [{
                "name": it["nft"]["name"],
                "number": it["nft"]["number"],
                "old_nft_id": it["nft"]["nft_id"],
                "new_nft_id": it.get("new_nft_id"),
                "burn_hash": it.get("burn_hash"),
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
        "accept_qr_url": accept["qr_url"],
        "accept_deeplink": accept["xumm_url"],
    })
    return True


async def run_swap_session(session: SwapSession) -> None:
    """Drive a SwapSession to a terminal state. Run as a background task."""
    try:
        nft1, nft2 = session.nft1, session.nft2
        new_attrs1, new_attrs2 = swap_meta.swap_traits(
            nft1["attributes"], nft2["attributes"], session.traits_to_swap)

        # 0. Verify every layer exists in the store BEFORE touching anything
        #    on-chain (this also pre-warms the CDN download cache)
        store = layer_store.get_layer_store()
        missing = (await swap_compose.missing_layers(new_attrs1, nft1["gender"], store)
                   + await swap_compose.missing_layers(new_attrs2, nft2["gender"], store))
        if missing:
            session.state = FAILED
            session.error = f"Missing trait layer files: {', '.join(missing)}"
            return

        # 1–2. Compose and upload both new NFTs (images + metadata)
        uploaded = []
        for nft, attrs in ((nft1, new_attrs1), (nft2, new_attrs2)):
            image_url, video_url, new_burn = await _build_and_upload(nft, attrs, store)
            session.state = UPLOADING
            meta = _swap_metadata(nft, attrs, image_url, video_url)
            meta_url = await _upload_swap_file(
                f"{nft['number']}/{nft['number']}_{new_burn}.json",
                json.dumps(meta, indent=2).encode(), "application/json")
            uploaded.append({"nft": nft, "image_url": image_url,
                             "video_url": video_url, "metadata_url": meta_url})

        # 3. Mint the replacements FIRST — if this fails, the originals are
        #    untouched and nothing is lost.
        session.state = MINTING
        _write_swap_record(session, uploaded, "minting")
        for item in uploaded:
            nft_id = await xrpl_ops.mint_nft(
                metadata_cdn_url=item["metadata_url"],
                taxon=config.SWAP_TAXON,
                issuer=config.SWAP_ISSUER_ADDRESS,
            )
            if not nft_id:
                await _burn_replacements(uploaded)
                _write_swap_record(session, uploaded, "failed_minting")
                session.error = (f"Reminting {item['nft']['name']} failed. "
                                 "No NFTs were lost — your originals are "
                                 "untouched. Try again later.")
                session.state = FAILED
                return
            item["new_nft_id"] = nft_id
        _write_swap_record(session, uploaded, "minted")

        # 4. Burn the originals (replacements are already safely minted)
        session.state = BURNING
        for i, item in enumerate(uploaded):
            burn_hash = await xrpl_ops.burn_nft(item["nft"]["nft_id"],
                                                session.wallet_address)
            if burn_hash:
                item["burn_hash"] = burn_hash
                continue

            if i == 0:
                # Nothing burned yet: clean up the replacements; the user
                # keeps both originals.
                await _burn_replacements(uploaded)
                _write_swap_record(session, uploaded, "failed_burning")
                session.error = (f"Failed to burn {item['nft']['name']} "
                                 f"({item['nft']['nft_id']}). No NFTs were "
                                 "lost — contact an administrator.")
                session.state = FAILED
                return

            # Original #1 is already burned, so its replacement MUST reach
            # the user. Cancel only the second half of the swap.
            other = uploaded[0]
            await _burn_replacements([item])
            _write_swap_record(session, uploaded, "partial_burn_failure")
            if not await _create_offer_and_accept(session, other):
                _write_swap_record(session, uploaded, "partial_burn_failure_no_offer")
                session.state = FAILED
                return
            session.error = (f"{item['nft']['name']} could not be burned, so "
                             "its traits were not swapped (it is still in "
                             f"your wallet). {other['nft']['name']} was "
                             "re-crafted — accept it below, then contact an "
                             "administrator.")
            _write_swap_record(session, uploaded, "partial")
            session.state = FAILED
            return
        _write_swap_record(session, uploaded, "burned")

        # 5. Offers (priced in BRIX, as the original swapper) + XUMM accept links
        session.state = CREATING_OFFERS
        for item in uploaded:
            if not await _create_offer_and_accept(session, item):
                _write_swap_record(session, uploaded, "failed_offers")
                session.state = FAILED
                return

        _write_swap_record(session, uploaded, "complete")
        session.state = OFFERS_READY

    except Exception as e:
        logging.error(f"Swap session {session.id} failed: {traceback.format_exc()}")
        session.state = FAILED
        session.error = str(e)
