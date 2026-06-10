# lfg_core/swap_flow.py
# Trait-swap session state machine: compose both re-crafted NFTs → upload to
# CDN → burn the originals → remint → create BRIX-priced offers → XUMM accept
# payloads. Same polling pattern as mint_flow. Nothing is burned until both
# new images and metadata are safely on the CDN.

import os
import json
import uuid
import asyncio
import logging
import traceback

import aiohttp

from lfg_core import config, xrpl_ops, xumm_ops, swap_meta, swap_compose, layer_store

COMPOSING = "composing"
UPLOADING = "uploading"
BURNING = "burning"
MINTING = "minting"
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
    storage_url = (f"{config.BUNNY_CDN_BASE_URL}/{config.BUNNY_CDN_STORAGE_ZONE}/"
                   f"{config.SWAP_CDN_FOLDER}/{path_on_cdn}")
    headers = {"AccessKey": config.BUNNY_CDN_ACCESS_KEY, "Content-Type": content_type}
    async with aiohttp.ClientSession() as session:
        resp = await session.put(storage_url, headers=headers, data=data)
        if resp.status not in (200, 201):
            raise Exception(f"BunnyCDN upload failed ({resp.status}) for {path_on_cdn}")
    return f"{config.BUNNY_CDN_PUBLIC_BASE}/{config.SWAP_CDN_FOLDER}/{path_on_cdn}"


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
    video_url = None
    try:
        if is_video:
            with open(path, "rb") as f:
                video_url = await _upload_swap_file(
                    f"{num}/{num}_{new_burn}.mp4", f.read(), "video/mp4")
            thumb = await asyncio.to_thread(
                swap_compose.extract_first_frame, path,
                os.path.splitext(path)[0] + ".png")
            with open(thumb, "rb") as f:
                image_url = await _upload_swap_file(
                    f"{num}/{num}_{new_burn}.png", f.read(), "image/png")
            os.remove(thumb)
        else:
            with open(path, "rb") as f:
                image_url = await _upload_swap_file(
                    f"{num}/{num}_{new_burn}.png", f.read(), "image/png")
    finally:
        if os.path.exists(path):
            os.remove(path)
    return image_url, video_url, new_burn


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

        # 3. Burn the originals (only now that replacements are safe on CDN)
        session.state = BURNING
        for item in uploaded:
            if not await xrpl_ops.burn_nft(item["nft"]["nft_id"], session.wallet_address):
                session.state = FAILED
                session.error = (f"Failed to burn {item['nft']['name']} "
                                 f"({item['nft']['nft_id']}). No NFTs were lost — "
                                 "contact an administrator.")
                return

        # 4. Remint with the new metadata
        session.state = MINTING
        for item in uploaded:
            nft_id = await xrpl_ops.mint_nft(
                metadata_cdn_url=item["metadata_url"],
                taxon=config.SWAP_TAXON,
                issuer=config.SWAP_ISSUER_ADDRESS,
            )
            if not nft_id:
                session.state = FAILED
                session.error = (f"Originals were burned but reminting "
                                 f"{item['nft']['name']} failed. Metadata is saved at "
                                 f"{item['metadata_url']} — contact an administrator.")
                return
            item["new_nft_id"] = nft_id

        # 5. Offers (priced in BRIX, as the original swapper) + XUMM accept links
        session.state = CREATING_OFFERS
        for item in uploaded:
            offer_id = await xrpl_ops.create_nft_offer(
                item["new_nft_id"], session.wallet_address,
                amount=xrpl_ops.swap_offer_amount())
            if not offer_id:
                session.state = FAILED
                session.error = (f"{item['nft']['name']} was reminted "
                                 f"({item['new_nft_id']}) but the offer failed — "
                                 "contact an administrator.")
                return
            accept = await xumm_ops.create_accept_offer_payload(offer_id)
            if not accept:
                session.state = FAILED
                session.error = (f"Offer {offer_id} created for {item['nft']['name']} "
                                 "but the XUMM request failed — accept it manually.")
                return
            session.results.append({
                "name": item["nft"]["name"],
                "nft_id": item["new_nft_id"],
                "image_url": item["image_url"],
                "video_url": item["video_url"],
                "metadata_url": item["metadata_url"],
                "accept_qr_url": accept["qr_url"],
                "accept_deeplink": accept["xumm_url"],
            })

        session.state = OFFERS_READY

    except Exception as e:
        logging.error(f"Swap session {session.id} failed: {traceback.format_exc()}")
        session.state = FAILED
        session.error = str(e)
