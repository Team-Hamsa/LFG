"""Inspect or explicitly provision issuer Tickets for XRPL Action Batches."""

from __future__ import annotations

import argparse
import asyncio

from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import TicketCreate
from xrpl.wallet import Wallet

from lfg_core import config, memos, xrpl_actions, xrpl_ops

MAX_TICKETS_PER_CREATE = 250


def _positive_target(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("target must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or provision XRPL Action issuer Tickets"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "provision"):
        child = subcommands.add_parser(command)
        child.add_argument(
            "--network",
            required=True,
            choices=("testnet", "mainnet"),
            help="must exactly match XRPL_NETWORK",
        )
    subcommands.choices["provision"].add_argument(
        "--target",
        type=_positive_target,
        default=config.XRPL_ACTIONS_TICKET_TARGET,
        help="desired number of issuer Tickets on the validated ledger",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    # This check occurs before constructing or signing a state-changing tx.
    config.assert_cli_network_match(args.network)
    client = JsonRpcClient(config.JSON_RPC_URL)
    tickets = await xrpl_actions.list_ticket_sequences(
        client, config.SIGNING_ACCOUNT
    )
    if args.command == "status":
        print(
            f"{args.network}: {len(tickets)} issuer Tickets"
            + (f" ({', '.join(map(str, tickets))})" if tickets else "")
        )
        return 0

    missing = max(0, args.target - len(tickets))
    if missing == 0:
        print(f"{args.network}: target already met ({len(tickets)}/{args.target})")
        return 0
    create_count = min(missing, MAX_TICKETS_PER_CREATE)
    transaction = TicketCreate(
        account=config.SIGNING_ACCOUNT,
        ticket_count=create_count,
        source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(
            memos.INITIATOR_BACKEND,
            memos.PLATFORM_BACKEND,
            memos.ACTION_BATCH_TICKET_CREATE,
        ),
    )
    result = await xrpl_ops.submit_backend_transaction(
        transaction,
        Wallet.from_seed(config.SEED),
        label="TicketCreate",
    )
    print(
        f"{args.network}: created {create_count} issuer Tickets;"
        f" transaction {result['hash']}"
    )
    if create_count < missing:
        print(
            f"{missing - create_count} Tickets remain to reach the target;"
            " rerun after validation"
        )
    return 0


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
