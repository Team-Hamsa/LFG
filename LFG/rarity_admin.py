#!/usr/bin/env python3
# Admin CLI for the variable rarity engine. Operates on the network
# selected by XRPL_NETWORK unless --network overrides it.
#
#   python rarity_admin.py seed [--mark-testnet 9001 9002]
#   python rarity_admin.py refresh
#   python rarity_admin.py odds --body '*' --category Background
#   python rarity_admin.py boost --body '*' --category Head --trait Crown \
#       [--initial 7] [--step-hours 24]
#   python rarity_admin.py set-floor 0.005 [--body B --category C --trait T]
#   python rarity_admin.py disable|enable --body B --category C --trait T

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from lfg_core import config, rarity  # noqa: E402


def scan_layer_values():
    """Scan the layer store and return {body: {trait_type: [values]}}."""
    from lfg_core.layer_store import get_layer_store

    async def _scan():
        store = get_layer_store()
        out = {}
        for body in await store.list_bodies():
            out[body] = {}
            for trait_type in await store.list_trait_types(body):
                values = await store.list_values(body, trait_type)
                if values:
                    out[body][trait_type] = values
        return out

    return asyncio.run(_scan())


def main():
    p = argparse.ArgumentParser(description="Rarity engine admin")
    p.add_argument("--network", default=None,
                   help="testnet|mainnet (default: XRPL_NETWORK env)")
    p.add_argument("--db", default=None, help="path to sqlite db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="bootstrap/backfill rarity from LFG table")
    s.add_argument("--mark-testnet", nargs="*", type=int, default=None,
                   metavar="NFT_NUMBER",
                   help="NFT numbers to retroactively mark as testnet")

    sub.add_parser("refresh", help="recount live_count from LFG table")

    o = sub.add_parser("odds", help="show effective odds for a body+category")
    o.add_argument("--body", required=True)
    o.add_argument("--category", required=True)

    for cmd, help_text in (
        ("boost", "arm a dormant boost on a trait"),
        ("disable", "disable a trait (excluded from picks)"),
        ("enable", "re-enable a disabled trait"),
    ):
        c = sub.add_parser(cmd, help=help_text)
        c.add_argument("--body", required=True)
        c.add_argument("--category", required=True)
        c.add_argument("--trait", required=True)
        if cmd == "boost":
            c.add_argument("--initial", type=float, default=None,
                           help="initial boost multiplier (default: RARITY_BOOST_INITIAL)")
            c.add_argument("--step-hours", type=int, default=None,
                           help="hours per step (default: RARITY_BOOST_STEP_HOURS)")

    f = sub.add_parser("set-floor", help="set floor_weight globally or per trait")
    f.add_argument("floor", type=float)
    f.add_argument("--body", default=None)
    f.add_argument("--category", default=None)
    f.add_argument("--trait", default=None)

    args = p.parse_args()
    net = args.network or config.XRPL_NETWORK
    conn = rarity.connect(args.db)
    try:
        rarity.ensure_schema(conn)
        if args.cmd == "seed":
            try:
                layer_values = scan_layer_values()
            except Exception as e:
                print(f"layer store scan skipped: {e}", file=sys.stderr)
                layer_values = None
            rarity.seed_from_collection(conn, network=net,
                                        mark_testnet=args.mark_testnet,
                                        layer_values=layer_values)
            print(f"seeded ({net})")
        elif args.cmd == "refresh":
            rarity.recalculate_rarity(conn, network=net)
            print(f"recounted ({net})")
        elif args.cmd == "odds":
            rows = rarity.get_odds(conn, args.body, args.category, network=net)
            if not rows:
                print(f"(no rows for {args.body}/{args.category} on {net})")
            for trait, count, share, weight, status in rows:
                print(f"{trait:30s}  n={count:5d}  share={share:6.2f}%  "
                      f"w={weight:.4f}  {status}")
        elif args.cmd == "boost":
            rarity.arm_boost(conn, args.body, args.category, args.trait,
                             network=net, boost_initial=args.initial,
                             boost_step_hours=args.step_hours)
            print(f"boost armed: {args.trait} (dormant until first mint)")
        elif args.cmd in ("disable", "enable"):
            rarity.set_enabled(conn, args.body, args.category, args.trait,
                               args.cmd == "enable", network=net)
            print(f"{args.trait}: {args.cmd}d")
        elif args.cmd == "set-floor":
            rarity.set_floor(conn, args.floor, network=net, body=args.body,
                             category=args.category, trait=args.trait)
            print("floor updated")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
