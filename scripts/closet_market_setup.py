#!/usr/bin/env python3
"""Closet Market (#443) ledger prerequisites. Read-only unless an --apply-* flag is given.

Outputs a status check (issuer lsfAllowTrustLineLocking + the app wallet's BRIX line) on every invocation.

Flags:
  --apply-issuer-flag      AccountSet SetFlag=17 (asfAllowTrustLineLocking) on BRIX_ISSUER.
                           ONE-WAY IN PRACTICE: it cannot be cleared while any BRIX is escrowed.
  --apply-app-limit        TrustSet the app wallet's BRIX line limit to BRIX_TRUSTLINE_LIMIT
                           (EscrowFinish into the app wallet fails past the limit)

Signing:
  mainnet: the BRIX issuer rLfgoBriX… has its master key disabled and its RegularKey
           is the distributor wallet -> BRIX_DISTRIBUTOR_SEED signs the AccountSet.
           The app wallet rLfgoMint… signs via SEED (its regkey), Account=SIGNING_ACCOUNT.
  testnet: the SEED account IS the BRIX issuer and the app wallet (no app limit needed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from xrpl.clients import JsonRpcClient  # noqa: E402
from xrpl.models import IssuedCurrencyAmount  # noqa: E402
from xrpl.models.requests import AccountInfo, AccountLines  # noqa: E402
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag, TrustSet  # noqa: E402
from xrpl.transaction import submit_and_wait  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

from lfg_core import config, memos, xrpl_ops  # noqa: E402

LSF_ALLOW_TRUSTLINE_LOCKING = 0x40000000


def flag_set(flags: int) -> bool:
    return bool(int(flags) & LSF_ALLOW_TRUSTLINE_LOCKING)


def signer_allowed(signer: str, account: str, account_data: dict[str, Any]) -> bool:
    return signer == account or signer == account_data.get("RegularKey")


def issuer_signer_seed(network: str) -> str:
    return str(config.BRIX_DISTRIBUTOR_SEED if network == "mainnet" else config.SEED)


def _account_data(client: JsonRpcClient, account: str) -> dict[str, Any]:
    result = client.request(AccountInfo(account=account, ledger_index="validated")).result
    if "account_data" not in result:
        raise SystemExit(f"account_info failed for {account}: {result}")
    return dict(result["account_data"])


def _app_line(client: JsonRpcClient) -> dict[str, Any] | None:
    result = client.request(
        AccountLines(
            account=config.SIGNING_ACCOUNT, peer=config.BRIX_ISSUER, ledger_index="validated"
        )
    ).result
    for line in result.get("lines", []):
        if str(line.get("currency", "")).upper() == str(config.BRIX_CURRENCY_HEX).upper():
            return dict(line)
    return None


def check(client: JsonRpcClient) -> dict[str, Any]:
    issuer = _account_data(client, config.BRIX_ISSUER)
    app_is_issuer = config.SIGNING_ACCOUNT == config.BRIX_ISSUER
    line = None if app_is_issuer else _app_line(client)
    return {
        "network": config.XRPL_NETWORK,
        "brix_issuer": config.BRIX_ISSUER,
        "allow_trustline_locking": flag_set(issuer.get("Flags", 0)),
        "issuer_regular_key": issuer.get("RegularKey"),
        "app_wallet": config.SIGNING_ACCOUNT,
        "app_is_issuer": app_is_issuer,
        "app_line_limit": line.get("limit") if line else None,
        "app_line_balance": line.get("balance") if line else None,
        "target_limit": str(config.BRIX_TRUSTLINE_LIMIT),
    }


def _confirm(network: str, what: str) -> None:
    typed = input(f"About to {what} on {network}. Type the network name to confirm: ").strip()
    if typed != network:
        raise SystemExit("aborted")


def apply_issuer_flag(client: JsonRpcClient, network: str) -> None:
    data = _account_data(client, config.BRIX_ISSUER)
    if flag_set(data.get("Flags", 0)):
        print("lsfAllowTrustLineLocking already set — nothing to do")
        return
    wallet = Wallet.from_seed(issuer_signer_seed(network))
    if not signer_allowed(wallet.classic_address, config.BRIX_ISSUER, data):
        raise SystemExit(
            f"{wallet.classic_address} is neither {config.BRIX_ISSUER} nor its RegularKey"
        )
    _confirm(network, f"set asfAllowTrustLineLocking on {config.BRIX_ISSUER} (one-way)")
    tx = AccountSet(
        account=config.BRIX_ISSUER,
        set_flag=AccountSetAsfFlag.ASF_ALLOW_TRUSTLINE_LOCKING,
        source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(
            memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_ACCOUNT_SET
        ),
    )
    print(
        json.dumps(
            submit_and_wait(tx, client, wallet).result.get("meta", {}).get("TransactionResult")
        )
    )


def apply_app_limit(client: JsonRpcClient, network: str) -> None:
    if config.SIGNING_ACCOUNT == config.BRIX_ISSUER:
        print("app wallet is the BRIX issuer — no trust line needed")
        return
    _confirm(
        network, f"raise {config.SIGNING_ACCOUNT}'s BRIX limit to {config.BRIX_TRUSTLINE_LIMIT}"
    )
    tx = TrustSet(
        account=config.SIGNING_ACCOUNT,
        limit_amount=IssuedCurrencyAmount(
            currency=config.BRIX_CURRENCY_HEX,
            issuer=config.BRIX_ISSUER,
            value=str(config.BRIX_TRUSTLINE_LIMIT),
        ),
        source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(
            memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_TRUSTSET
        ),
    )
    result = submit_and_wait(tx, client, Wallet.from_seed(config.SEED)).result
    print(json.dumps(result.get("meta", {}).get("TransactionResult")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--network", required=True, choices=["mainnet", "testnet"])
    parser.add_argument("--apply-issuer-flag", action="store_true")
    parser.add_argument("--apply-app-limit", action="store_true")
    args = parser.parse_args(argv)
    config.assert_cli_network_match(args.network)
    client = xrpl_ops.rpc_client()
    if args.apply_issuer_flag:
        apply_issuer_flag(client, args.network)
    if args.apply_app_limit:
        apply_app_limit(client, args.network)
    print(json.dumps(check(client), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
