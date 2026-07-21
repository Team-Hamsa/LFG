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
#   pre-fee stale-pointer check (nft_exists on the burnable originals; a
#     definitively-absent token fails free and self-heals the index — #211) →
#   compose/upload → collect modify fee (if any) →
#   mint replacements (revertible: burn them) →
#   modify mutables (revertible: modify back to the old URI) →
#   pre-burn stale-pointer guard (final arbiter of the same #211 check) →
#   burn originals (IRREVERSIBLE) →
#   persist ledger truth to the on-chain index (onchain_<net>.db: old token
#     burned, replacement live — best-effort but LOUD on failure; the flow's
#     second durable output besides the journal) →
#   offers (with a bounded on-ledger landed-offer recheck before declaring
#     failure).
# If anything fails before the original burns, replacements are burned back
# and modifies reverted, so the user keeps their originals untouched. Every
# on-chain step is journaled to SWAP_RECORDS_DIR so an administrator can
# recover a partial swap. Journal statuses distinguish outcomes one-to-one:
# "stale_pointer" always means a full unwind (nothing delivered), while
# "stale_pointer_partial"/"partial" mean the first edition's replacement WAS
# delivered (offer live/pending accept) — with "_no_offer" suffixes when the
# surviving offer itself failed.
#
# The #211 incident (2026-07-10) shapes the guards above: the listener was
# down while a replacement offer reported failure (it had actually landed and
# was accepted minutes later), so the on-chain index kept serving the burned
# old_nft_id; every later swap on that edition minted a replacement, hit
# tecNO_ENTRY on the burn, and reverted. Hence the existence checks (stale
# rows fail free and are healed), the post-burn index persist (the roster
# sees ledger truth even with the listener down), and the landed-offer
# recheck (a delivered offer is adopted, never stranded).

import asyncio
import json
import logging
import os
import sqlite3
import time
import traceback
import uuid
from decimal import ROUND_UP, Decimal
from typing import Any

from xrpl.models import IssuedCurrencyAmount
from xrpl.utils import xrp_to_drops

from lfg_core import (
    brix_payment,
    cdn,
    config,
    image_archive,
    layer_store,
    memos,
    nft_index,
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
    raises if the wallet holds no BRIX and the AMM can't quote a price.
    Thin wrapper over the shared brix_payment.detect_payment_path (#238)."""
    return await brix_payment.detect_payment_path(wallet_address, brix_amount)


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
        # #212: push delivery state of the fee payment payload
        # ("sent" | "failed" | None) for honest client messaging.
        self.payment_push: str | None = None
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

    async def regenerate_payment(self) -> bool:
        """Replace an expired/missed fee QR with a fresh XUMM payload without
        restarting the swap (mirror of mint issue #22). Keeps the old link if
        XUMM is down — the on-ledger payment wait doesn't care which payload
        (or the static detect link) actually delivers the fee. Returns True
        only when a fresh payload was actually built, so the service can
        surface a failure instead of silently echoing the stale link."""
        if self.fee_destination is None or self.fee_amount is None or self.fee_currency is None:
            return False  # fee not priced yet — nothing to rebuild
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
            self.payment_push = payload.get("push")
            return True
        return False

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
            "payment_push": self.payment_push,
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
) -> tuple[str, str | None, int, str]:
    """Compose the re-crafted NFT and upload image/video; returns
    (image_url, video_url, new_burn_count, cdn_stem) — the caller uploads the
    metadata JSON under the same stem so image and metadata stay paired.

    The CDN stem carries a random suffix so every swap publishes at a URL
    that has never been served before. `burnCount` alone is NOT a safe
    revision counter: economy-written metadata (scripts/_economy_deps) omits
    the field, so the swap after an economy op reads 0 and would re-upload
    over the `<edition>_1.*` an earlier swap already published — after which
    every URL-keyed cache (the browser, the Bunny edge, and the listener's
    own metadata fetch, which then indexes pre-swap attributes) keeps
    serving the old art. Same reasoning as _economy_deps._compose_char /
    _upload_closet, which have always suffixed for exactly this reason."""
    new_burn = nft["burn_count"] + 1
    num = nft["number"]
    stem = f"{num}_{new_burn}_{uuid.uuid4().hex[:8]}"
    path, is_video = await swap_compose.compose_nft(attributes, nft["gender"], store, stem)
    image_url, video_url = await swap_compose.upload_output(
        path,
        is_video,
        _upload_swap_file,
        f"{num}/{stem}",
        keep_still=image_archive.pending_still_path(config.XRPL_NETWORK, num, token),
    )
    return image_url, video_url, new_burn, stem


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


def _open_index_db() -> sqlite3.Connection:
    return nft_index.init_db(nft_index.index_db_path(config.XRPL_NETWORK))


def _persist_remint_to_index(item: dict[str, Any]) -> None:
    """Write the post-burn ledger truth for one remint straight into the
    on-chain index (#211 — see the module docstring). The burn is the point
    of no return, and both stores are normally repointed only by the listener
    observing the txs — mirror what the listener would do, immediately: flip
    is_burned on the old token and upsert the replacement as live (owner =
    the issuer wallet until the offer is accepted — truthful; the listener
    updates it on accept, and later listener/backfill upserts fill in
    ledger_index).

    Failures are LOUD (both ids in one CRITICAL line, mirroring bulk's
    _on_mint) but never fail the session: the chain is truth and the
    listener/backfill self-heals the index."""
    old_id = item["nft"]["nft_id"]
    new_id = item["new_nft_id"]
    try:
        conn = _open_index_db()
        try:
            nft_index.mark_burned(conn, old_id)
            try:
                # The NFTokenID's first two bytes ARE the on-ledger flags
                # (same derivation as nft_index.to_token).
                flags = int(new_id[:4], 16)
            except ValueError:
                flags = 0
            nft_index.upsert(
                conn,
                nft_index.OnchainNft(
                    nft_id=new_id,
                    nft_number=item["nft"]["number"],
                    owner=config.SIGNING_ACCOUNT,
                    is_burned=False,
                    mutable=bool(flags & nft_index.NFT_FLAG_MUTABLE),
                    # .hex() is lowercase — the index's canonical case.
                    uri_hex=item["metadata_url"].encode("utf-8").hex(),
                    body=swap_meta.detect_body(item["attrs"]),
                    attributes=item["attrs"],
                    image=item["image_url"],
                    ledger_index=None,
                ),
            )
        finally:
            conn.close()
    except Exception:
        logging.critical(
            f"post-burn index persist FAILED for swap edition {item['nft']['number']} "
            f"(old={old_id} burned on-chain, new={new_id} live): the roster will serve "
            f"the stale old token until the listener/backfill catches up. "
            f"{traceback.format_exc()}"
        )


def _heal_stale_index_pointer(nft: dict[str, Any]) -> None:
    """The pre-burn existence check proved this session was built on a stale
    index row — the ledger definitively no longer has the token (#211: a
    prior remint the listener never observed). Mark it burned so the roster
    stops offering it. Best-effort: a failure here only delays the heal until
    the next listener/backfill pass."""
    try:
        conn = _open_index_db()
        try:
            nft_index.mark_burned(conn, nft["nft_id"])
            newer = nft_index.nft_by_number(conn, nft["number"])
            if newer is not None:
                logging.info(
                    f"stale swap pointer healed: edition {nft['number']} token "
                    f"{nft['nft_id']} marked burned; live token is {newer.nft_id} — "
                    "the next session picks it up from the roster"
                )
            else:
                logging.warning(
                    f"stale swap pointer healed: edition {nft['number']} token "
                    f"{nft['nft_id']} marked burned; no live token known at this "
                    "edition yet (listener/backfill will restore it)"
                )
        finally:
            conn.close()
    except Exception:
        logging.error(
            f"stale-pointer index heal failed for {nft['nft_id']}: {traceback.format_exc()}"
        )


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


# Bounded cadence for the landed-offer recheck: a tx forwarded just before a
# network blip can still validate 1-2 ledgers (~4-8s) after create_nft_offer
# gives up, so a single instant look would miss it. 3 passes 5s apart mirrors
# create_nft_offer's own inline confirm cadence (spans the LastLedgerSequence
# window). Tests shrink the delay to 0.
_LANDED_OFFER_ATTEMPTS = 3
_LANDED_OFFER_DELAY_SECONDS = 5.0


async def _find_landed_offer(nft_id: str, destination: str) -> str | None:
    """Look for a live sell offer of `nft_id` from the issuer wallet to
    `destination` and return its offer index, or None.

    #211 — see the module docstring: create_nft_offer collapses "landed but
    the short inline confirm loop gave up / submit raised after forwarding"
    into the same None as a genuine failure (it never confirms by hash the
    way _submit_and_confirm does), so a falsy create result is rechecked
    on-ledger — with the bounded retry above, since the offer may validate
    seconds after the create call gave up — before the session declares
    failed_offers. The match is owner (the issuer wallet) + destination (the
    swapper) only: unlike bulk mint's amount-0 gift offers, swap offers carry
    the fee price (_offer_amount) which can vary with the AMM quote, so
    amount is not part of the identity. A lookup failure is indeterminate —
    retry through it, then return None and let the caller fail as before."""
    for attempt in range(_LANDED_OFFER_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_LANDED_OFFER_DELAY_SECONDS)
        try:
            offers = await xrpl_ops.get_nft_sell_offers(nft_id, raise_on_error=True)
        except Exception as e:
            logging.warning(f"landed-offer recheck failed for {nft_id}: {e}")
            continue
        for offer in offers:
            if (
                offer.get("owner") == xrpl_ops.bot_wallet_address()
                and offer.get("destination") == destination
                and offer.get("offer_index")
            ):
                logging.warning(
                    f"create_nft_offer reported failure but offer {offer['offer_index']} "
                    f"for {nft_id} is live on-ledger — adopting it (#211)"
                )
                return str(offer["offer_index"])
    return None


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
                # #41 T9: the webapp share button needs an edition number to
                # build PUBLIC_SHARE_BASE_URL + /nft/<n> without regexing the
                # display name client-side; None when the name carries no
                # "#<digits>" (client falls back to the bithomp URL).
                "nft_number": swap_meta.extract_nft_number(item["nft"]["name"]),
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
        # #211: the offer may have landed despite the falsy return — adopt it
        # instead of stranding a delivered-but-unconfirmed replacement.
        offer_id = await _find_landed_offer(item["new_nft_id"], session.wallet_address)
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
            "nft_number": swap_meta.extract_nft_number(item["nft"]["name"]),  # #41 T9
            "image_url": item["image_url"],
            "video_url": item["video_url"],
            "metadata_url": item["metadata_url"],
            "modified": False,
            "accept_qr_url": accept["qr_url"],
            "accept_deeplink": accept["xumm_url"],
            "accept_push": accept.get("push"),
        }
    )
    return True


async def _collect_modify_fee(session: SwapSession, modify_count: int) -> bool:
    """Charge the upfront fee for in-place swaps via XUMM (in BRIX or its
    AMM XRP equivalent, per the session's detected path) and wait for the
    verified payment. Returns False on failure. On a payload-creation
    failure (#262 fail-fast) the session is already FAILED with
    session.error set — callers must not overwrite it; on a payment-wait
    timeout the session is left non-terminal and the caller stamps
    PAYMENT_TIMEOUT."""
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
        # #262 fail-fast: the XUMM sign-request payload was never created
        # (429 backoff / outage). The static detect link is NOT parseable by
        # Xaman as a sign request, so entering the payment wait with only it
        # would strand the user on a dead fee screen for the full timeout.
        # Nothing has touched the chain yet — fee collection precedes every
        # burn/mint/modify — so failing here is free.
        session.state = FAILED
        session.error = (
            "The signing service is busy — please try again shortly. Your NFTs are untouched."
        )
        return False
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

        # #211 stale-pointer pre-check (see module docstring): the roster that
        # built this session reads the on-chain index, and a stale row feeds
        # an already-burned old_nft_id into a fresh session. The answer is
        # knowable before any payment or on-chain work — check now, while
        # failing is free (no fee charged, nothing composed), instead of
        # discovering it at burn time after a mixed swap's modify fee was
        # consumed. Tri-state: only a DEFINITIVE clio absence (False) trips
        # it; None (transient blip) assumes present. The burn-time guard
        # below remains the final arbiter for a token that vanishes
        # mid-session.
        stale_nfts = []
        for item in burn_items:
            if await xrpl_ops.nft_exists(item["nft"]["nft_id"]) is False:
                # Heal every stale row (both items can be stale after one
                # missed-listener window), then fail once.
                _heal_stale_index_pointer(item["nft"])
                stale_nfts.append(item["nft"]["name"])
        if stale_nfts:
            verb = "was" if len(stale_nfts) == 1 else "were"
            session.error = (
                f"{' and '.join(stale_nfts)} {verb} already swapped or "
                "replaced — refresh and try again."
            )
            session.state = FAILED
            return

        # 1. Compose and upload both new NFTs (images + metadata)
        session.state = COMPOSING
        for item in items:
            nft, attrs = item["nft"], item["attrs"]
            image_url, video_url, new_burn, stem = await _build_and_upload(
                nft, attrs, store, session.id
            )
            session.state = UPLOADING
            meta = _swap_metadata(nft, attrs, image_url, video_url)
            meta_url = await _upload_swap_file(
                f"{nft['number']}/{stem}.json",
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
                if session.state != FAILED:
                    # #262: the fee gate fails the session itself when the
                    # sign request could never be created; only a genuine
                    # payment-wait timeout lands here.
                    session.state = PAYMENT_TIMEOUT
                    session.error = (
                        "No swap fee payment was received in time. Your NFTs are untouched."
                    )
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
                # #211 stale-pointer guard (see module docstring) — the final
                # arbiter after the pre-fee check: the token can still vanish
                # mid-session. Tri-state: only a DEFINITIVE clio absence
                # (False) trips it; None (transient blip) assumes present and
                # proceeds to the burn, which is its own final arbiter —
                # never unwind on a blip.
                stale = await xrpl_ops.nft_exists(item["nft"]["nft_id"]) is False
                if stale:
                    _heal_stale_index_pointer(item["nft"])
                    burn_hash = None
                else:
                    burn_hash = await xrpl_ops.burn_nft(
                        item["nft"]["nft_id"],
                        session.wallet_address,
                        platform=memos.platform_for_surface(session.platform),
                    )
                if burn_hash:
                    item["burn_hash"] = burn_hash
                    # #211: the burn is the point of no return — persist the
                    # ledger truth (old burned, replacement live) into the
                    # on-chain index NOW, before the fallible offer/XUMM
                    # steps, so readers see the new token even if everything
                    # after this fails and the listener is down.
                    _persist_remint_to_index(item)
                    continue

                if i == 0:
                    # Nothing burned yet: unwind everything (replacements
                    # burned, modifies reverted); the user keeps both
                    # originals exactly as they were (on a stale pointer this
                    # first original was already gone before the session
                    # started — nothing of theirs was touched either way).
                    await _revert_modifies(modify_items, session.wallet_address)
                    await _burn_replacements(burn_items)
                    _discard_stills(items, session.id)
                    _write_swap_record(
                        session, items, "stale_pointer" if stale else "failed_burning"
                    )
                    if stale and session.fee_amount is not None:
                        # A mixed swap already collected (and consumed) the
                        # upfront modify fee, and the modify was reverted —
                        # inviting a plain retry would charge the fee again
                        # for one effective swap. Route to an admin instead.
                        session.error = (
                            f"{item['nft']['name']} was already swapped or "
                            "replaced. Your NFTs are untouched, but the swap "
                            "fee was charged — contact an administrator."
                        )
                    elif stale:
                        session.error = (
                            f"{item['nft']['name']} was already swapped or "
                            "replaced — refresh and try again."
                        )
                    else:
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
                # "stale_pointer" is reserved for the i==0 full unwind
                # (nothing delivered); the partial shape gets its own status
                # so a journal triager never mistakes a delivered-pending-
                # offer replacement for a no-action unwind.
                partial_status = "stale_pointer_partial" if stale else "partial_burn_failure"
                _write_swap_record(session, items, partial_status)
                if not await _create_offer_and_accept(session, other):
                    _write_swap_record(session, items, f"{partial_status}_no_offer")
                    session.state = FAILED
                    return
                if stale:
                    session.error = (
                        f"{item['nft']['name']} was already swapped or "
                        "replaced, so its traits were not swapped — refresh "
                        f"and try again. {other['nft']['name']} was "
                        "re-crafted — accept it below."
                    )
                else:
                    session.error = (
                        f"{item['nft']['name']} could not be burned, so "
                        "its traits were not swapped (it is still in "
                        f"your wallet). {other['nft']['name']} was "
                        "re-crafted — accept it below, then contact an "
                        "administrator."
                    )
                # For stale this repeats the pre-offer status string, but the
                # write is not redundant: it refreshes the (overwritten)
                # record with the offer_id the accept step just set.
                _write_swap_record(session, items, "stale_pointer_partial" if stale else "partial")
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
                    "nft_number": swap_meta.extract_nft_number(item["nft"]["name"]),  # #41 T9
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
