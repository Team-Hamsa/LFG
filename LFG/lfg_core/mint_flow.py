# lfg_core/mint_flow.py
# Mint session state machine: payment → image generation → CDN upload →
# XRPL mint → offer → XUMM accept payload. Orchestrates the same pipeline as
# the bot's mint button, but exposes state for polling instead of sending
# Discord messages.

import os
import json
import uuid
import asyncio
import logging
import traceback

import aiohttp

from lfg_core import config, traits, xrpl_ops, xumm_ops, layer_store, swap_compose
from db_helpers import get_next_nft_number, record_nft_mint

# Session states (terminal: done, failed, payment_timeout)
AWAITING_PAYMENT = "awaiting_payment"
GENERATING = "generating"
MINTING = "minting"
CREATING_OFFER = "creating_offer"
OFFER_READY = "offer_ready"
DONE = "done"
FAILED = "failed"
PAYMENT_TIMEOUT = "payment_timeout"

TERMINAL_STATES = {DONE, FAILED, PAYMENT_TIMEOUT}


class MintSession:
    def __init__(self, discord_id: str, wallet_address: str):
        self.id = uuid.uuid4().hex
        self.discord_id = discord_id
        self.wallet_address = wallet_address
        self.state = AWAITING_PAYMENT
        self.error = None
        self.payment_link = xumm_ops.generate_static_payment_link(config.TOKEN_ISSUER_ADDRESS)
        self.nft_number = None
        self.nft_id = None
        self.image_url = None
        self.accept_qr_url = None
        self.accept_deeplink = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "payment_link": self.payment_link,
            "nft_number": self.nft_number,
            "nft_id": self.nft_id,
            "image_url": self.image_url,
            "accept_qr_url": self.accept_qr_url,
            "accept_deeplink": self.accept_deeplink,
        }


async def _upload_to_bunny(path_on_cdn: str, data: bytes, content_type: str) -> str:
    """PUT bytes to BunnyCDN storage; returns the public CDN URL."""
    storage_url = (f"{config.BUNNY_CDN_BASE_URL}/{config.BUNNY_CDN_STORAGE_ZONE}/"
                   f"{config.BUNNY_CDN_FOLDER}/{path_on_cdn}")
    headers = {"AccessKey": config.BUNNY_CDN_ACCESS_KEY, "Content-Type": content_type}
    async with aiohttp.ClientSession() as session:
        resp = await session.put(storage_url, headers=headers, data=data)
        if resp.status not in (200, 201):
            raise Exception(f"BunnyCDN upload failed ({resp.status}) for {path_on_cdn}")
    return f"{config.BUNNY_CDN_PUBLIC_BASE}/{config.BUNNY_CDN_FOLDER}/{path_on_cdn}"


async def run_mint_session(session: MintSession) -> None:
    """Drive a MintSession to a terminal state. Run as a background task."""
    try:
        # 1. Wait for the sender-verified token payment
        paid = await xrpl_ops.wait_for_payment(
            destination=config.TOKEN_ISSUER_ADDRESS,
            expected_sender=session.wallet_address,
            expected_amount="1",
        )
        if not paid:
            session.state = PAYMENT_TIMEOUT
            return

        # 2. Compose a random NFT from the unified layer store (same tree
        #    the Trait Swapper uses: <gender>/<TraitType>/<Value>.ext)
        session.state = GENERATING
        session.nft_number = get_next_nft_number()
        store = layer_store.get_layer_store()
        gender, attributes = await traits.select_random_attributes(store)
        output_path, is_video = await swap_compose.compose_nft(
            attributes, gender, store, f"lfg_{session.nft_number}")

        # 3. Upload image (+ video) and metadata to BunnyCDN
        video_cdn_url = None
        try:
            if is_video:
                with open(output_path, 'rb') as f:
                    video_cdn_url = await _upload_to_bunny(
                        f"lfg_{session.nft_number}.mp4", f.read(), "video/mp4")
                thumb = await asyncio.to_thread(
                    swap_compose.extract_first_frame, output_path,
                    os.path.splitext(output_path)[0] + ".png")
                with open(thumb, 'rb') as f:
                    image_cdn_url = await _upload_to_bunny(
                        f"lfg_{session.nft_number}.png", f.read(), "image/png")
                os.remove(thumb)
            else:
                with open(output_path, 'rb') as f:
                    image_cdn_url = await _upload_to_bunny(
                        f"lfg_{session.nft_number}.png", f.read(), "image/png")
        finally:
            if os.path.exists(output_path):
                os.remove(output_path)
        session.image_url = image_cdn_url

        metadata = {
            "name": f"{config.NFT_COLLECTION_NAME} #{session.nft_number}",
            "image": image_cdn_url,
            "edition": session.nft_number,
            "attributes": attributes,
        }
        if video_cdn_url:
            metadata["video"] = video_cdn_url
        metadata_cdn_url = await _upload_to_bunny(
            f"metadata_{session.nft_number}.json",
            json.dumps(metadata, indent=2).encode(), "application/json")

        # 4. Mint on XRPL
        session.state = MINTING
        nft_id = await xrpl_ops.mint_nft(
            metadata_cdn_url=metadata_cdn_url,
            taxon=config.NFT_TAXON,
            issuer=config.TOKEN_ISSUER_ADDRESS,
        )
        if not nft_id:
            session.state = FAILED
            session.error = "Failed to mint NFT on XRPL. Please contact an administrator."
            return
        session.nft_id = nft_id

        traits_dict = {t["trait_type"]: t["value"] for t in metadata["attributes"]}
        # The LFG table's headwear column is named Hat (layer tree uses Head)
        if "Head" in traits_dict:
            traits_dict.setdefault("Hat", traits_dict["Head"])
        if not record_nft_mint(
            nft_number=session.nft_number,
            nft_id=nft_id,
            discord_id=session.discord_id,
            owner_address=session.wallet_address,
            metadata_url=metadata_cdn_url,
            image_url=image_cdn_url,
            traits=traits_dict,
        ):
            logging.error(f"Failed to record NFT #{session.nft_number} in database")

        # 5. Create the transfer offer and the XUMM accept payload
        session.state = CREATING_OFFER
        offer_id = await xrpl_ops.create_nft_offer(nft_id, session.wallet_address)
        if not offer_id:
            session.state = FAILED
            session.error = (f"NFT minted (ID: {nft_id}) but offer creation failed. "
                             "Please contact an administrator.")
            return

        accept = await xumm_ops.create_accept_offer_payload(offer_id)
        if not accept:
            session.state = FAILED
            session.error = (f"NFT minted and offer created ({offer_id}) but the XUMM "
                             "request failed. Please accept the offer manually.")
            return

        session.accept_qr_url = accept['qr_url']
        session.accept_deeplink = accept['xumm_url']
        session.state = OFFER_READY

    except Exception as e:
        logging.error(f"Mint session {session.id} failed: {traceback.format_exc()}")
        session.state = FAILED
        session.error = str(e)
