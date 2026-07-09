# lfg_core/swap_meta.py
# Trait Swapper metadata helpers (ported from Trait-Swapper/helpers.py +
# main.py): NFT URI decoding, metadata fetch, attribute normalization,
# gender / season detection, trait-swap merge logic. Pure / async; no Discord.

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

from lfg_core import config

# Layering / canonical attribute order. Body is structural and never swapped.
# Source of truth is trait_config.yaml (z_overrides / layers). Keep in sync;
# test_default_config_parity_with_legacy_constants enforces the parity.
TRAIT_ORDER = [
    "Background",
    "Back",
    "Body",
    "Clothing",
    "Mouth",
    "Eyebrows",
    "Eyes",
    "Head",
    "Accessory",
]
SWAPPABLE_TRAITS = [
    "Background",
    "Back",
    "Clothing",
    "Mouth",
    "Eyebrows",
    "Eyes",
    "Head",
    "Accessory",
]
# Values that belong on the Back layer even when stored under Accessory.
BACK_VALUES = ["Angel Wings", "Angel Wings Open"]


def decode_uri(uri_hex: str) -> str:
    """Decode an on-chain hex URI to an http(s) URL (resolving ipfs://)."""
    uri = bytes.fromhex(uri_hex).decode("ascii")
    return resolve_ipfs(uri)


def resolve_ipfs(uri: str) -> str:
    if uri.startswith("ipfs://"):
        parts = uri[len("ipfs://") :].split("/")
        if len(parts) >= 2:
            return f"https://{parts[0]}.ipfs.dweb.link/{'/'.join(parts[1:])}"
        return f"https://{parts[0]}.ipfs.dweb.link/"
    return uri


def normalize_attributes(attributes: list[Any]) -> list[dict[str, str]]:
    """Fix the 'Accesory' typo, fill missing trait types with 'None',
    relocate Back values stored under Accessory, and order canonically."""
    # Metadata is untrusted: drop entries that aren't {"trait_type": str, ...}
    attrs = [
        dict(a) for a in attributes if isinstance(a, dict) and isinstance(a.get("trait_type"), str)
    ]
    for a in attrs:
        a.setdefault("value", "None")
        if a.get("trait_type") == "Accesory":
            a["trait_type"] = "Accessory"
    present = {a["trait_type"] for a in attrs}
    for trait in TRAIT_ORDER:
        if trait not in present:
            attrs.append({"trait_type": trait, "value": "None"})
    for a in attrs:
        if a["trait_type"] == "Accessory" and a["value"] in BACK_VALUES:
            value = a["value"]
            a["value"] = "None"
            for b in attrs:
                if b["trait_type"] == "Back":
                    b["value"] = value
                    break
    attrs = [a for a in attrs if a["trait_type"] in TRAIT_ORDER]
    attrs.sort(key=lambda a: TRAIT_ORDER.index(a["trait_type"]))
    return attrs


def get_attr(attributes: list[dict[str, Any]], trait_type: str) -> str | None:
    for a in attributes:
        if a["trait_type"] == trait_type:
            return a["value"]  # type: ignore[no-any-return]
    return None


def none_swaps(
    attrs1: list[dict[str, Any]],
    attrs2: list[dict[str, Any]],
    traits_to_swap: list[str],
) -> list[str]:
    """Of the requested slots, those that can't be swapped because ONE side is
    empty ('None'/''/missing). The Trait Swapper is a peer exchange, so swapping
    an empty slot would hand emptiness to the other NFT — deleting a trait it
    has. You can't trade a slot you don't fill. Returns the offending trait
    types (order preserved) so the caller can reject them; empty list = all
    swappable.

    Emptiness must mirror swap_compose._canonical's predicate (falsy or the
    literal 'None'): ~34% of the live mainnet collection encodes an empty
    Accessory as '' rather than 'None', and _canonical silently drops falsy
    values, so nothing downstream would catch what this guard lets through."""
    blocked = []
    for trait in traits_to_swap:
        v1 = get_attr(attrs1, trait)
        v2 = get_attr(attrs2, trait)
        if not v1 or v1 == "None" or not v2 or v2 == "None":
            blocked.append(trait)
    return blocked


def detect_body(attributes: list[dict[str, Any]]) -> str:
    """Body class determines which layer directory set is used."""
    body_val = get_attr(attributes, "Body") or ""
    if "Straight" in body_val:
        return "male"
    if "Curved" in body_val:
        return "female"
    if "Ape" in body_val:
        return "ape"
    return "skeleton"


detect_gender = detect_body  # backward-compat alias


def extract_nft_number(name: str) -> int | None:
    match = re.search(r"#(\d+)", name or "")
    return int(match.group(1)) if match else None


def season_for_number(num: int) -> int:
    if num <= 707:
        return 1
    if num <= 2121:
        return 2
    return 3


def swap_traits(
    attrs1: list[dict[str, Any]],
    attrs2: list[dict[str, Any]],
    traits_to_swap: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exchange the selected trait values between two attribute lists.
    Returns (new_attrs1, new_attrs2), canonically ordered."""
    new1, new2 = [], []
    for trait in TRAIT_ORDER:
        a1 = {"trait_type": trait, "value": get_attr(attrs1, trait)}
        a2 = {"trait_type": trait, "value": get_attr(attrs2, trait)}
        if trait in traits_to_swap:
            a1, a2 = (
                {"trait_type": trait, "value": a2["value"]},
                {"trait_type": trait, "value": a1["value"]},
            )
        new1.append(a1)
        new2.append(a2)
    return new1, new2


async def fetch_metadata(
    uri_hex: str, http: aiohttp.ClientSession | None = None
) -> dict[str, Any] | None:
    """Fetch and parse the metadata JSON behind an on-chain hex URI.
    Pass `http` to reuse a session across many fetches."""
    try:
        url = decode_uri(uri_hex)
        if http is None:
            async with aiohttp.ClientSession() as session:
                return await fetch_metadata(uri_hex, session)
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            return json.loads(await resp.text())  # type: ignore[no-any-return]
    except Exception as e:
        logging.warning(f"fetch_metadata failed for {uri_hex[:24]}…: {e}")
        return None


# lsfMutable bit on the on-ledger NFToken (Dynamic NFTs amendment)
NFT_FLAG_MUTABLE = 0x0010


def normalize_nft(
    nft_id: str, metadata: dict[str, Any], flags: int = 0, uri_hex: str = ""
) -> dict[str, Any] | None:
    """Build the normalized NFT record used by the swap UI/flow, or None if
    the NFT isn't a swappable collection piece. `flags` are the on-ledger
    NFToken flags (mutable NFTs are swapped via NFTokenModify; legacy
    burnable ones via burn-and-remint); `uri_hex` is the current on-chain
    URI, kept so a modify can be reverted."""
    name = metadata.get("name", "")
    if not isinstance(name, str) or "#" not in name:
        return None
    num = extract_nft_number(name)
    if not num or num < 1 or num > config.SWAP_MAX_NFT_NUMBER:
        return None
    raw_attrs = metadata.get("attributes")
    attributes = normalize_attributes(raw_attrs if isinstance(raw_attrs, list) else [])
    try:
        burn_count = int(metadata.get("burnCount") or 0)
    except (TypeError, ValueError):
        burn_count = 0
    return {
        "nft_id": nft_id,
        "name": name,
        "number": num,
        "season": season_for_number(num),
        "image": resolve_ipfs(metadata.get("image", "")),
        "video": metadata.get("video"),
        "burn_count": burn_count,
        "gender": detect_gender(attributes),
        "attributes": attributes,
        "mutable": bool(flags & NFT_FLAG_MUTABLE),
        "uri_hex": uri_hex,
    }


async def load_wallet_nfts(wallet: str, get_account_nfts: Any) -> list[dict[str, Any]]:
    """List + normalize all swappable NFTs in a wallet. get_account_nfts is
    injected (xrpl_ops.get_account_nfts) to keep this module network-light."""
    raw = await get_account_nfts(wallet, config.SWAP_ISSUER_ADDRESS)
    async with aiohttp.ClientSession() as http:
        metas = await asyncio.gather(*[fetch_metadata(n["uri_hex"], http) for n in raw])
    nfts = []
    for nft, meta in zip(raw, metas, strict=False):
        if not isinstance(meta, dict):
            continue
        try:
            record = normalize_nft(
                nft["nft_id"], meta, flags=nft.get("flags", 0), uri_hex=nft.get("uri_hex", "")
            )
        except Exception as e:
            # One token with malformed metadata must not break the listing.
            logging.warning(f"Skipping NFT {nft['nft_id']}: bad metadata ({e})")
            continue
        if record:
            nfts.append(record)
    nfts.sort(key=lambda n: n["number"])
    return nfts
