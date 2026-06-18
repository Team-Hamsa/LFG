#!/usr/bin/env python3
"""Audit CDN layer coverage for every LIVE on-chain NFT in the collection.

A trait swap recomposes an NFT's image from the CDN layer tree
(``layers/<body>/<TraitType>/<Value>``). The swap aborts — fail-safe, before any
burn — when a trait value on the NFT has no backing layer file. This script
finds every NFT that currently *cannot* be swapped and the exact layer assets
that are missing, so they can be uploaded.

Why on-chain, not the DB: the ``LFG`` table is keyed one row per edition number,
but the chain holds multiple NFTokens per edition (duplicates / divergent
variants from prior swaps and reminting). The swap reads live on-chain metadata,
so only an on-chain enumeration sees what the swap actually sees. This audit
pages ``nfts_by_issuer`` (clio), fetches each live NFT's metadata, and checks it.

It performs NO layer downloads: existence is checked against cached directory
listings (``store.list_values``), never ``store.resolve``.

  python scripts/audit_layer_coverage.py --network testnet
  python scripts/audit_layer_coverage.py --network mainnet
  python scripts/audit_layer_coverage.py --issuer r... --taxon 1760 --clio wss://...

Exit code is non-zero when any coverage gap is found (CI-ready).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp
from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import Request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from lfg_core import layer_store, swap_meta  # noqa: E402

# Body classes that have their own layer subtree.
BODIES = ["male", "female", "ape", "skeleton"]

# Distinct layer trait-types we look up per body (the swappable + structural set).
TRAIT_TYPES = sorted(set(swap_meta.TRAIT_ORDER))

# Per-network enumeration defaults. testnet issuer is the SEED minter account,
# so it is taken from config (which derives it from SEED on a testnet process).
NETWORKS: dict[str, dict[str, Any]] = {
    "mainnet": {
        "issuer": "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ",
        "taxon": 1760,
        "clio": "wss://s2-clio.ripple.com",
    },
    "testnet": {
        "issuer": None,  # filled from config.SWAP_ISSUER_ADDRESS at runtime
        "taxon": 1760,
        "clio": "wss://clio.altnet.rippletest.net:51233",
    },
}

# Bound concurrent metadata fetches so a large collection can't open thousands
# of sockets at once.
FETCH_CONCURRENCY = 16


@dataclass(frozen=True)
class Missing:
    """One unbacked trait value on one NFT."""

    body: str
    trait_type: str
    value: str

    def asset(self) -> str:
        return f"{self.body}/{self.trait_type}/{self.value}"


@dataclass
class NftResult:
    nft_id: str
    number: int | None
    body: str
    missing: list[Missing] = field(default_factory=list)
    error: str | None = None  # metadata unfetchable / unparseable


async def build_available_sets(store: Any) -> dict[tuple[str, str], set[str]]:
    """One set of available values per (body, trait_type). Reads only the
    (cached) CDN directory listings; downloads nothing."""
    available: dict[tuple[str, str], set[str]] = {}
    for body in BODIES:
        for trait_type in TRAIT_TYPES:
            values = await store.list_values(body, trait_type)
            available[(body, trait_type)] = set(values)
    return available


def meta_attributes(metadata: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """Map NFT metadata to (body, normalized attributes), running it through the
    SAME normalization the swap path uses so results cannot drift."""
    raw = metadata.get("attributes")
    attributes = swap_meta.normalize_attributes(raw if isinstance(raw, list) else [])
    body = swap_meta.detect_body(attributes)
    return body, attributes


def audit_attributes(
    body: str, attributes: list[dict[str, str]], available: dict[tuple[str, str], set[str]]
) -> list[Missing]:
    """Missing layer files for one NFT. Pure — no I/O. 'None'/empty values are
    skipped (they need no layer file, exactly as the compose path skips them)."""
    missing: list[Missing] = []
    for attr in attributes:
        value = attr.get("value") or "None"
        if value == "None":
            continue
        trait_type = attr["trait_type"]
        if value not in available.get((body, trait_type), set()):
            missing.append(Missing(body, trait_type, value))
    return missing


async def enumerate_onchain(
    clio: str, issuer: str, taxon: int, limit: int = 400
) -> list[dict[str, Any]]:
    """Page nfts_by_issuer and return every LIVE (non-burned) NFT as
    {nft_id, uri_hex}. Burned tokens are skipped — they can't be swapped."""
    out: list[dict[str, Any]] = []
    marker: Any = None
    async with AsyncWebsocketClient(clio) as client:
        while True:
            req: dict[str, Any] = {
                "method": "nfts_by_issuer",
                "issuer": issuer,
                "nft_taxon": taxon,
                "limit": limit,
            }
            if marker:
                req["marker"] = marker
            r = await client.request(Request.from_dict(req))
            for x in r.result.get("nfts", []):
                if x.get("is_burned"):
                    continue
                out.append({"nft_id": x["nft_id"], "uri_hex": x.get("uri", "")})
            marker = r.result.get("marker")
            if not marker:
                break
    return out


async def run_audit(
    enumerate_fn: Callable[[], Awaitable[list[dict[str, Any]]]],
    fetch_meta_fn: Callable[[str], Awaitable[dict[str, Any] | None]],
    store: Any,
    concurrency: int = FETCH_CONCURRENCY,
) -> list[NftResult]:
    """Enumerate live NFTs, fetch each one's metadata, and audit its traits.
    enumerate_fn/fetch_meta_fn are injected to keep this unit-testable."""
    available = await build_available_sets(store)
    tokens = await enumerate_fn()
    sem = asyncio.Semaphore(concurrency)

    async def audit_one(token: dict[str, Any]) -> NftResult:
        nft_id = token["nft_id"]
        uri_hex = token.get("uri_hex") or ""
        if not uri_hex:
            return NftResult(nft_id=nft_id, number=None, body="", error="no URI on token")
        async with sem:
            metadata = await fetch_meta_fn(uri_hex)
        if not isinstance(metadata, dict):
            return NftResult(nft_id=nft_id, number=None, body="", error="metadata unfetchable")
        number = swap_meta.extract_nft_number(str(metadata.get("name", "")))
        body, attributes = meta_attributes(metadata)
        return NftResult(
            nft_id=nft_id,
            number=number,
            body=body,
            missing=audit_attributes(body, attributes, available),
        )

    return await asyncio.gather(*(audit_one(t) for t in tokens))


def format_reports(results: list[NftResult], timestamp: str, network: str) -> str:
    """Markdown report: per-NFT failures + aggregated upload worklist."""
    failures = [r for r in results if r.missing]
    errors = [r for r in results if r.error]

    blocked_by: Counter[str] = Counter()
    for r in failures:
        for asset in {m.asset() for m in r.missing}:
            blocked_by[asset] += 1

    lines: list[str] = []
    lines.append(f"# Layer Coverage Audit ({network}) — {timestamp}")
    lines.append("")
    lines.append(f"- Live NFTs audited: **{len(results)}**")
    lines.append(f"- NFTs that cannot be swapped: **{len(failures)}**")
    lines.append(f"- Distinct missing layer assets: **{len(blocked_by)}**")
    lines.append(f"- NFTs with unreadable metadata: **{len(errors)}**")
    lines.append("")

    lines.append("## Missing layer assets (upload worklist)")
    lines.append("")
    if blocked_by:
        lines.append("| Asset (body/TraitType/Value) | NFTs blocked |")
        lines.append("| --- | --- |")
        for asset, count in sorted(blocked_by.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| `{asset}` | {count} |")
    else:
        lines.append("_None — every live NFT's traits are fully backed._")
    lines.append("")

    lines.append("## NFTs that cannot be swapped")
    lines.append("")
    if failures:
        lines.append("| # | body | nft_id | missing traits |")
        lines.append("| --- | --- | --- | --- |")
        for r in sorted(failures, key=lambda r: (r.number or 0, r.nft_id)):
            traits = ", ".join(f"{m.trait_type}={m.value}" for m in r.missing)
            lines.append(f"| {r.number} | {r.body} | `{r.nft_id}` | {traits} |")
    else:
        lines.append("_None._")
    lines.append("")

    if errors:
        lines.append("## NFTs with unreadable metadata (could not be audited)")
        lines.append("")
        lines.append("| nft_id | error |")
        lines.append("| --- | --- |")
        for r in sorted(errors, key=lambda r: r.nft_id):
            lines.append(f"| `{r.nft_id}` | {r.error} |")
        lines.append("")
    return "\n".join(lines)


async def _amain() -> int:
    from lfg_core import config

    parser = argparse.ArgumentParser(description="Audit CDN layer coverage for live on-chain NFTs.")
    parser.add_argument("--network", choices=sorted(NETWORKS), default=config.XRPL_NETWORK)
    parser.add_argument("--issuer", help="override issuer address")
    parser.add_argument("--taxon", type=int, help="override taxon")
    parser.add_argument("--clio", help="override clio websocket URL")
    parser.add_argument(
        "--report-dir", default=os.path.join(REPO_ROOT, "reports"), help="where to write the report"
    )
    args = parser.parse_args()

    net = NETWORKS[args.network]
    issuer = args.issuer or net["issuer"] or config.SWAP_ISSUER_ADDRESS
    taxon = args.taxon if args.taxon is not None else net["taxon"]
    clio = args.clio or net["clio"]

    store = layer_store.get_layer_store()

    async def enum() -> list[dict[str, Any]]:
        return await enumerate_onchain(clio, issuer, taxon)

    async with aiohttp.ClientSession() as http:

        async def fetch(uri_hex: str) -> dict[str, Any] | None:
            return await swap_meta.fetch_metadata(uri_hex, http)

        results = await run_audit(enum, fetch, store)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    report = format_reports(results, timestamp, args.network)
    os.makedirs(args.report_dir, exist_ok=True)
    report_path = os.path.join(args.report_dir, f"layer-coverage-{args.network}-{timestamp}.md")
    with open(report_path, "w") as f:
        f.write(report)

    failures = [r for r in results if r.missing]
    errors = [r for r in results if r.error]
    assets = {m.asset() for r in failures for m in r.missing}
    print(f"Network: {args.network}  issuer: {issuer}  taxon: {taxon}")
    print(f"Audited {len(results)} live NFTs.")
    print(f"  Cannot be swapped: {len(failures)}")
    print(f"  Distinct missing layer assets: {len(assets)}")
    print(f"  Unreadable metadata: {len(errors)}")
    print(f"  Report: {report_path}")
    return 1 if failures else 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
