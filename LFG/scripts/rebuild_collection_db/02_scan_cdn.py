#!/usr/bin/env python3
"""Step 2: Scan the CDN for original-mint metadata by edition number.

The BunnyCDN holds the ORIGINAL mint metadata at
`<base>/<n>/<n>_0.json` for editions that were never trait-swapped (~half the
collection). This fast HTTP scan caches those traits so step 3 only has to fall
back to slow IPFS gateways for the re-minted remainder.

Output JSON: {edition: {status, desc, edition, name, image, attrs}}.

  python 02_scan_cdn.py --max 3535 --out work/cdn_scan.json
"""
import argparse
import json
import os
import requests
from concurrent.futures import ThreadPoolExecutor

DEFAULT_BASE = "https://lfgo.b-cdn.net/LFGO"


def main():
    """Scan the CDN by edition number and write present traits to JSON."""
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=DEFAULT_BASE, help="CDN base path (no trailing /)")
    p.add_argument("--max", type=int, default=3535, help="highest edition to probe")
    p.add_argument("--workers", type=int, default=40)
    p.add_argument("--out", default="work/cdn_scan.json")
    args = p.parse_args()

    sess = requests.Session()

    def fetch(n):
        """Fetch one edition's CDN metadata; return (n, trait/status dict)."""
        try:
            r = sess.get(f"{args.base}/{n}/{n}_0.json", timeout=15)
            if r.status_code != 200:
                return (n, {"status": r.status_code})
            md = r.json()
            attrs = {a.get("trait_type"): a.get("value")
                     for a in md.get("attributes", [])}
            return (n, {"status": 200, "desc": md.get("description"),
                        "edition": md.get("edition"), "name": md.get("name"),
                        "image": md.get("image"), "attrs": attrs})
        except Exception as e:
            return (n, {"status": type(e).__name__})

    results = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for n, data in ex.map(fetch, range(1, args.max + 1)):
            results[n] = data

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(results, open(args.out, "w"))
    ok = sum(1 for d in results.values() if d["status"] == 200)
    print(f"scanned 1..{args.max} | present {ok} | missing {args.max - ok}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
