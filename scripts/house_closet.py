#!/usr/bin/env python3
"""House Closet (#548): move the project's trait stock into the Closet market.

  python scripts/house_closet.py --network mainnet                    # status (read-only)
  python scripts/house_closet.py --network mainnet --apply-setup      # one-time house setup
  python scripts/house_closet.py --network mainnet --migrate          # dry run: the plan
  python scripts/house_closet.py --network mainnet --migrate --apply  # burn -> house Closet -> relist

Needs CLOSET_HOUSE_WALLET (and, for --apply-setup, CLOSET_HOUSE_SEED) in .env.
The plan file is reports/house_migration_<network>.json. Runbook:
docs/ops/closet-market.md; design:
docs/superpowers/specs/2026-09-18-house-closet-migration-design.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import _economy_deps as deps  # noqa: E402
from xrpl.asyncio.transaction import submit_and_wait  # noqa: E402
from xrpl.models.requests import AccountInfo, AccountLines  # noqa: E402
from xrpl.models.transactions import Transaction  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

from lfg_core import config, xrpl_ops  # noqa: E402
from lfg_core import house_closet as hc  # noqa: E402


def plan_path(network: str) -> str:
    return os.path.join(REPO_ROOT, "reports", f"house_migration_{network}.json")


async def _account(address: str) -> dict[str, Any] | None:
    client = xrpl_ops.async_rpc_client()
    result = (await client.request(AccountInfo(account=address, ledger_index="validated"))).result
    if result.get("error") == "actNotFound":
        return None
    if "account_data" not in result:
        raise SystemExit(f"account_info failed for {address}: {result}")
    return dict(result["account_data"])


async def _brix_line(address: str) -> dict[str, Any] | None:
    client = xrpl_ops.async_rpc_client()
    request = AccountLines(account=address, peer=config.BRIX_ISSUER, ledger_index="validated")
    for line in (await client.request(request)).result.get("lines", []):
        if str(line.get("currency", "")).upper() == str(config.BRIX_CURRENCY_HEX).upper():
            return dict(line)
    return None


async def _submit(tx: Transaction, wallet: Wallet) -> str:
    response = await submit_and_wait(tx, xrpl_ops.async_rpc_client(), wallet)
    return str(response.result.get("meta", {}).get("TransactionResult"))


def _confirm(network: str, what: str) -> None:
    typed = input(f"About to {what} on {network}. Type the network name to confirm: ").strip()
    if typed != network:
        raise SystemExit("aborted")


def _counts(state: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in state["items"].values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts


def _plan_summary(items: list[hc.PlanItem]) -> dict[str, Any]:
    listed = [i for i in items if i.action == hc.LIST]
    total = sum((Decimal(i.price_brix) for i in listed), Decimal(0))
    return {
        "list": len(listed),
        "hold": len(items) - len(listed),
        "list_brix_total": format(total, "f"),
    }


async def _status(conn: Any, network: str, show_items: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"network": network, "app_wallet": config.SIGNING_ACCOUNT}
    house: str | None = None
    try:
        house = hc.house_address()
        out["house"] = house
    except hc.HouseConfigError as e:
        out["house_error"] = str(e)
    if house:
        out["house_funded"] = await _account(house) is not None
        line = await _brix_line(house) if out["house_funded"] else None
        out["house_brix_line"] = (
            {"limit": line.get("limit"), "balance": line.get("balance")} if line else None
        )
        out["house_closet_active"] = hc.cms.closet_active(conn, house)
    items = hc.build_plan(conn, config.SIGNING_ACCOUNT)
    out["plan"] = _plan_summary(items)
    out["crossing_bids"] = hc.crossing_bids(conn, items, house or "")
    if show_items:
        out["items"] = [
            {
                "nft_id": i.nft_id,
                "slot": i.slot,
                "value": i.value,
                "price_brix": i.price_brix,
                "action": i.action,
            }
            for i in items
        ]
    state = hc.load_state(plan_path(network))
    if state["items"]:
        out["plan_file"] = {"house": state["house"], "statuses": _counts(state)}
    return out


async def _setup(conn: Any, network: str) -> dict[str, str]:
    wallet = hc.house_wallet()
    _confirm(network, f"set up {wallet.classic_address} as the house wallet (trust line + Closet)")
    setup_deps = hc.SetupDeps(
        economy=deps.build_economy_deps(conn),
        account_fn=_account,
        brix_line_fn=_brix_line,
        submit_fn=_submit,
    )
    return await hc.setup_house(wallet, setup_deps, limit=str(config.BRIX_TRUSTLINE_LIMIT))


async def _migrate(conn: Any, network: str) -> int:
    house = hc.house_address()
    path = plan_path(network)
    state = hc.load_state(path)
    hc.check_ready(
        conn,
        house,
        state,
        brix_line=await _brix_line(house),
        limit=str(config.BRIX_TRUSTLINE_LIMIT),
    )
    hc.merge_plan(state, hc.build_plan(conn, config.SIGNING_ACCOUNT), house)
    todo = sum(1 for e in state["items"].values() if e["status"] in (hc.PLANNED, hc.FAILED))
    _confirm(network, f"burn {todo} project trait tokens into {house}'s Closet and relist them")
    hc.save_state(path, state)
    mdeps = hc.MigrationDeps(
        economy=deps.build_economy_deps(conn),
        app_wallet=config.SIGNING_ACCOUNT,
        house=house,
        fee_bps=config.CLOSET_MARKET_FEE_BPS,
        plan_path=path,
    )
    done = await hc.deposit_phase(state, mdeps) and hc.list_phase(state, mdeps)
    print(json.dumps({"plan_file": path, "statuses": _counts(state), "complete": done}, indent=2))
    return 0 if done else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--network", required=True, choices=["mainnet", "testnet"])
    parser.add_argument("--apply-setup", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--apply", action="store_true", help="with --migrate: run it")
    args = parser.parse_args(argv)
    if args.apply and not args.migrate:
        parser.error("--apply only goes with --migrate")
    conn = deps.open_index(args.network)
    try:
        if args.apply_setup:
            print(json.dumps(asyncio.run(_setup(conn, args.network)), indent=2))
            return 0
        if args.migrate and args.apply:
            return asyncio.run(_migrate(conn, args.network))
        print(json.dumps(asyncio.run(_status(conn, args.network, args.migrate)), indent=2))
        return 0
    except (hc.HouseConfigError, hc.MigrationRefused) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
