#!/usr/bin/env python
# Exclude a season's traits from minting on one network (#114).
#
#   .venv/bin/python scripts/disable_season_traits.py --network mainnet --season 3 [--apply]
#
# Flips trait_rarity.enabled=0 for every layers/seasons.json entry of the
# given season. Mint-only: swaps and rendering read the layer store directly
# and keep working; existing NFTs still render. Dry-run by default.
#
# Guarded: aborts with no changes if any (body, category) would be left with
# zero enabled traits (weighted_pick raises on an empty category).

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lfg_core import rarity, seasons  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Disable a season's traits for minting")
    parser.add_argument("--network", required=True, choices=["testnet", "mainnet"])
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--db", default="lfg_nfts.db")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()

    manifest = seasons.load_seasons()
    targets = sorted(k for k, s in manifest.items() if s == args.season)
    if not targets:
        sys.exit(
            f"no season-{args.season} entries in {seasons.manifest_path()} — "
            "run scripts/seed_seasons_from_csv.py first"
        )
    print(f"season {args.season}: {len(targets)} manifest entries -> network {args.network}")

    conn = rarity.connect(args.db)
    try:
        if not args.apply:
            for key in targets:
                body, category, trait = key.split("/", 2)
                row = conn.execute(
                    """SELECT enabled FROM trait_rarity
                       WHERE network=? AND body=? AND category=? AND trait=?""",
                    (args.network, body, category, trait),
                ).fetchone()
                state = (
                    "no rarity row"
                    if row is None
                    else ("enabled" if row[0] else "already disabled")
                )
                print(f"  would disable {key}  [{state}]")
            print("dry-run: pass --apply to write")
            return
        changed = seasons.disable_season(conn, manifest, season=args.season, network=args.network)
        for body, category, trait in changed:
            print(f"  disabled {body}/{category}/{trait}")
        print(f"disabled {len(changed)} traits on {args.network}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
