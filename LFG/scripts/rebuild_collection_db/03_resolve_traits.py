#!/usr/bin/env python3
"""Step 3: Resolve traits for every LIVE NFT.

Per NFT, in priority order:
  1. CDN scan cache (step 2 output) keyed by edition  — fast
  2. The NFT's own IPFS URI via public gateways       — slow, for re-mints

A persistent per-URI cache makes this fully resumable: re-running only fetches
what is still missing (handy because public IPFS gateways are slow/flaky for
poorly-seeded content — expect to run it 2-3 times to mop up timeouts).

Edition number comes from the metadata `name` ("...#N"); the `edition` field is
unreliable for Season 1. Output JSON: {results:{edition:rec}, errors, conflicts}.

  python 03_resolve_traits.py --onchain work/onchain.json \
      --cdn work/cdn_scan.json --out work/traits.json
"""
import argparse
import json
import os
import re
import time
import hashlib
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

GATEWAYS = [
    "https://nftstorage.link/ipfs/",
    "https://ipfs.io/ipfs/",
    "https://dweb.link/ipfs/",
    "https://w3s.link/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://flk-ipfs.xyz/ipfs/",
]

_sess = requests.Session()
_lock = threading.Lock()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--onchain", default="work/onchain.json")
    p.add_argument("--cdn", default="work/cdn_scan.json")
    p.add_argument("--cache", default="work/meta_cache")
    p.add_argument("--out", default="work/traits.json")
    p.add_argument("--progress", default="work/resolve_progress.txt")
    p.add_argument("--workers", type=int, default=32)
    args = p.parse_args()
    os.makedirs(args.cache, exist_ok=True)

    def cache_path(uri):
        return os.path.join(args.cache, hashlib.sha1(uri.encode()).hexdigest() + ".json")

    def fetch_ipfs(ipfs_path):
        """Try gateways, rotating the start by path hash so concurrent workers
        spread load across gateways (avoids one gateway's 429s)."""
        last = None
        off = int(hashlib.sha1(ipfs_path.encode()).hexdigest(), 16) % len(GATEWAYS)
        for attempt in range(6):
            for i in range(len(GATEWAYS)):
                gw = GATEWAYS[(off + i) % len(GATEWAYS)]
                try:
                    r = _sess.get(gw + ipfs_path, timeout=20)
                    if r.status_code == 200:
                        return r.json()
                    last = f"HTTP{r.status_code}"
                except Exception as e:
                    last = type(e).__name__
            time.sleep(1.0 * (attempt + 1))
        return {"_error": last}

    def edition_from(md, ipfs_path):
        m = re.search(r"#(\d+)", md.get("name") or "")
        if m:
            return int(m.group(1))
        m = re.search(r"/(\d+)\.json$", ipfs_path)
        if m:
            return int(m.group(1))
        ed = md.get("edition")
        return int(ed) if isinstance(ed, int) else None

    onchain = json.load(open(args.onchain))
    scan = json.load(open(args.cdn))
    cdn = {int(n): d for n, d in scan.items() if d.get("status") == 200}
    live = [x for x in onchain if not x["burned"]]
    state = {"done": 0, "fetched": 0}

    def handle(nft):
        uri = nft["uri"]
        ipfs_path = uri[7:] if uri.lower().startswith("ipfs://") else None
        fname_ed = None
        if ipfs_path:
            m = re.search(r"/(\d+)\.json$", ipfs_path)
            if m:
                fname_ed = int(m.group(1))

        if fname_ed is not None and fname_ed in cdn:
            d = cdn[fname_ed]
            rec = {"edition": fname_ed, "attrs": d["attrs"], "name": d.get("name"),
                   "desc": d.get("desc"), "image": d.get("image"), "source": "cdn"}
        else:
            cp = cache_path(uri)
            if os.path.exists(cp):
                md = json.load(open(cp))
            elif uri.lower().startswith("ipfs://"):
                md = fetch_ipfs(ipfs_path)
                if "_error" not in md:
                    json.dump(md, open(cp, "w"))
                    with _lock:
                        state["fetched"] += 1
            elif uri.startswith("https://"):
                try:
                    md = _sess.get(uri, timeout=30).json()
                    json.dump(md, open(cp, "w"))
                except Exception as e:
                    md = {"_error": type(e).__name__}
            else:
                md = {"_error": "unknown-scheme"}

            if "_error" not in md:
                rec = {"edition": edition_from(md, ipfs_path or ""),
                       "attrs": {a.get("trait_type"): a.get("value")
                                 for a in md.get("attributes", [])},
                       "name": md.get("name"), "desc": md.get("description"),
                       "image": md.get("image"), "source": "ipfs"}
            else:
                rec = {"edition": fname_ed, "error": md["_error"],
                       "source": "error", "uri": uri}

        rec["nft_id"] = nft["nft_id"]
        with _lock:
            state["done"] += 1
            if state["done"] % 50 == 0:
                open(args.progress, "w").write(
                    f"{state['done']}/{len(live)} done, {state['fetched']} fetched\n")
        return rec

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        recs = list(ex.map(handle, live))

    results, errors, conflicts = {}, [], []
    for rec in recs:
        if rec.get("source") == "error" or rec.get("edition") is None:
            errors.append(rec)
            continue
        ed = rec["edition"]
        if ed in results:
            conflicts.append(ed)
        results[ed] = rec

    json.dump({"results": results, "errors": errors,
               "conflicts": sorted(set(conflicts))}, open(args.out, "w"))
    print(f"resolved {len(results)} editions | errors {len(errors)} | "
          f"conflicts {len(set(conflicts))} | fetched {state['fetched']}")
    if errors:
        print("re-run to retry errors (transient gateway failures are common)")


if __name__ == "__main__":
    main()
