#!/usr/bin/env python3
"""Step 1: Enumerate the on-chain collection.

Pages through clio's `nfts_by_issuer` for the given issuer + taxon and records
every NFT (live and burned) with its decoded metadata URI. The response carries
`uri` inline, so no per-NFT `nft_info` round trip is needed.

Output JSON: list of {serial, nft_id, burned, uri}.

  python 01_enumerate_onchain.py --out work/onchain.json
"""

import argparse
import asyncio
import json

from xrpl.asyncio.clients import AsyncWebsocketClient
from xrpl.models.requests import Request

# LFG mainnet collection (see memory: project-lfg-collection-data)
DEFAULT_ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
DEFAULT_TAXON = 1760
DEFAULT_WS = "wss://s2-clio.ripple.com"


async def enumerate_collection(ws, issuer, taxon, limit):
    """Page through nfts_by_issuer and return every NFT (live + burned)."""
    out = []
    marker = None
    async with AsyncWebsocketClient(ws) as client:
        while True:
            req = {"method": "nfts_by_issuer", "issuer": issuer, "nft_taxon": taxon, "limit": limit}
            if marker:
                req["marker"] = marker
            r = await client.request(Request.from_dict(req))
            for x in r.result.get("nfts", []):
                uri = bytes.fromhex(x["uri"]).decode("utf8") if x.get("uri") else ""
                out.append(
                    {
                        "serial": x.get("nft_serial"),
                        "nft_id": x["nft_id"],
                        "burned": bool(x.get("is_burned")),
                        "uri": uri,
                    }
                )
            marker = r.result.get("marker")
            if not marker:
                break
    return out


def main():
    """Parse args, enumerate the collection, and write the JSON output."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--issuer", default=DEFAULT_ISSUER)
    p.add_argument("--taxon", type=int, default=DEFAULT_TAXON)
    p.add_argument("--ws", default=DEFAULT_WS, help="XRPL clio websocket")
    p.add_argument("--limit", type=int, default=400)
    p.add_argument("--out", default="work/onchain.json")
    args = p.parse_args()

    nfts = asyncio.run(enumerate_collection(args.ws, args.issuer, args.taxon, args.limit))
    import os

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(nfts, open(args.out, "w"))
    live = sum(1 for x in nfts if not x["burned"])
    print(f"total minted {len(nfts)} | live {live} | burned {len(nfts) - live}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
