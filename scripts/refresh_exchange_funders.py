#!/usr/bin/env python3
"""Regenerate ``lfg_core/exchange_funders.json`` from XRPScan's well-known names.

``funding.EXCHANGES`` grants an exemption: two wallets activated by the same
listed account are NOT treated as sybil siblings (sponsored-mint admission),
NOT treated as linked (fee-cover), and NOT collapsed into one actor
(``sourcetag_metrics.unique_actors``). An entry that is not a genuinely shared
custodial hot wallet would therefore weaken all three, so the snapshot is not a
verbatim copy of a third party's classification: it is XRPScan's well-known
list **filtered to the operators reviewed below**, and nothing else.

``REVIEWED_OPERATORS`` is the whole review surface. Each name is an exchange or
custodian whose hot wallets aggregate unrelated customers; adding one is a
deliberate change a reviewer can judge by the name alone, and
``tests/test_funding.py`` fails if the snapshot ever carries a name that is not
on it. Individual addresses need no case-by-case review because they are only
ever whatever XRPScan currently labels for a reviewed operator.

Usage::

    python scripts/refresh_exchange_funders.py            # report drift only
    python scripts/refresh_exchange_funders.py --write    # rewrite the snapshot

``--write`` prints every added and removed account so the diff is reviewable.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WELL_KNOWN_URL = "https://api.xrpscan.com/api/v1/names/well-known"
SNAPSHOT = Path(__file__).resolve().parent.parent / "lfg_core" / "exchange_funders.json"

# Operators whose XRPScan-labelled accounts may fund an LFG user. Centralized
# exchanges and custodial brokers only — never a project treasury, validator,
# AMM, personal wallet or unlabelled account, none of which aggregate unrelated
# people the way a withdrawal hot wallet does.
REVIEWED_OPERATORS = frozenset(
    {
        "BigONE",
        "Binance",
        "Bitbuy",
        "Bitfinex",
        "Bithumb",
        "Bitpanda",
        "Bitrue",
        "Bitso",
        "Bitstamp",
        "Bitvavo",
        "Bybit",
        "CEX.IO",
        "ChangeNOW",
        "CoinSpot",
        "Coinbase",
        "Coincheck",
        "Coins.ph",
        "Crypto.com",
        "Gate.io",
        "Gemini",
        "HTX",
        "HitBTC",
        "Huobi",
        "Indodax",
        "Kraken",
        "KuCoin",
        "MEXC",
        "Netcoins",
        "Nexo",
        "OKX",
        "Poloniex",
        "Revolut",
        "Robinhood",
        "StormGain",
        "Uphold",
        "WhiteBIT",
        # Xaman's shared service account: a custodial intermediary that moves
        # funds for unrelated Xaman users, same aggregation property.
        "Xaman Service Fee",
        "eToro",
    }
)


def fetch_well_known(url: str = WELL_KNOWN_URL) -> list[dict[str, str]]:
    # XRPScan 403s a bare urllib UA.
    request = urllib.request.Request(url, headers={"User-Agent": "lfg-exchange-funders/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https only
        payload = json.load(response)
    if not isinstance(payload, list) or not payload:
        raise SystemExit(f"unexpected well-known payload from {url}")
    return payload


def select(entries: list[dict[str, str]]) -> dict[str, str]:
    """XRPScan accounts belonging to a reviewed operator, address -> operator."""
    return {
        entry["account"]: entry["name"]
        for entry in entries
        if entry.get("name") in REVIEWED_OPERATORS and entry.get("account")
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the snapshot in place")
    parser.add_argument("--url", default=WELL_KNOWN_URL)
    args = parser.parse_args(argv)

    current = json.loads(SNAPSHOT.read_text())["accounts"]
    fresh = select(fetch_well_known(args.url))

    added = {a: n for a, n in fresh.items() if a not in current}
    removed = {a: n for a, n in current.items() if a not in fresh}
    renamed = {a: (current[a], fresh[a]) for a in fresh if a in current and current[a] != fresh[a]}

    print(f"snapshot {len(current)} accounts, upstream {len(fresh)} for reviewed operators")
    for account, name in sorted(added.items(), key=lambda kv: kv[1]):
        print(f"  + {account}  {name}")
    for account, name in sorted(removed.items(), key=lambda kv: kv[1]):
        print(f"  - {account}  {name}")
    for account, (was, now) in sorted(renamed.items()):
        print(f"  ~ {account}  {was} -> {now}")
    if not (added or removed or renamed):
        print("  no drift")
        return 0
    if not args.write:
        print("re-run with --write to apply")
        return 1

    SNAPSHOT.write_text(
        json.dumps(
            {
                "accounts": dict(sorted(fresh.items(), key=lambda kv: (kv[1], kv[0]))),
                "source": (
                    f"XRPScan well-known names ({args.url}), fetched "
                    f"{datetime.now(timezone.utc).date().isoformat()}; accounts of the "
                    "operators reviewed in scripts/refresh_exchange_funders.py only"
                ),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
