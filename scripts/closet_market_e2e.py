#!/usr/bin/env python3
"""Testnet end-to-end for the Closet Market ledger path (#443).

Covers what unit tests cannot: a real BRIX TokenEscrow created with our
PREIMAGE-SHA-256 condition, EscrowFinish with the sealed fulfillment, app
forward/refund Payments, memo lookups, EscrowCancel after CancelAfter.

The Xaman signature is replaced by signing the bidder's EscrowCreate locally,
the Closet mirror is a no-op (no real Closet token is touched), and orders and
fills live in a throwaway sqlite file — the staging order book is never used.

Prereqs: XRPL_NETWORK=testnet ECONOMY_NETWORK=testnet, run from a staging
checkout (SEED = testnet BRIX issuer = app wallet), and the issuer flag set:
  .venv/bin/python scripts/closet_market_setup.py --network testnet --apply-issuer-flag
Usage:
  .venv/bin/python scripts/closet_market_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from cryptography.fernet import Fernet  # noqa: E402
from xrpl.clients import JsonRpcClient  # noqa: E402
from xrpl.models import IssuedCurrencyAmount  # noqa: E402
from xrpl.models.transactions import EscrowCreate, Payment, TrustSet  # noqa: E402
from xrpl.transaction import submit_and_wait  # noqa: E402
from xrpl.wallet import Wallet, generate_faucet_wallet  # noqa: E402

from lfg_core import closet_market_flow as cmf  # noqa: E402
from lfg_core import closet_market_store as cms  # noqa: E402
from lfg_core import closet_token as ct  # noqa: E402
from lfg_core import config, crypto_condition, economy_store, memos, xrpl_ops  # noqa: E402

PRICE = "10"


def _brix(value: str) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(
        currency=config.BRIX_CURRENCY_HEX, issuer=config.BRIX_ISSUER, value=value
    )


def _fund_with_brix(client: JsonRpcClient, wallet: Wallet, amount: str) -> None:
    submit_and_wait(
        TrustSet(account=wallet.classic_address, limit_amount=_brix("1000000")), client, wallet
    )
    if Decimal(amount) > 0:
        issuer = Wallet.from_seed(config.SEED)
        submit_and_wait(
            Payment(
                account=config.SIGNING_ACCOUNT,
                destination=wallet.classic_address,
                amount=_brix(amount),
                source_tag=config.SOURCE_TAG,
                memos=memos.build_memo_models(
                    memos.INITIATOR_BACKEND, memos.PLATFORM_BACKEND, memos.ACTION_PAYMENT
                ),
            ),
            client,
            issuer,
        )


def _balance(address: str) -> Decimal:
    value = asyncio.run(
        xrpl_ops.get_trustline_balance(address, config.BRIX_CURRENCY_HEX, config.BRIX_ISSUER)
    )
    return value if value is not None else Decimal(0)


def _create_escrow(
    client: JsonRpcClient, bidder: Wallet, condition: str, cancel_after: int
) -> dict:
    tx = EscrowCreate(
        account=bidder.classic_address,
        destination=config.SIGNING_ACCOUNT,
        amount=_brix(PRICE),
        condition=condition,
        cancel_after=cancel_after,
        source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(
            memos.INITIATOR_USER, memos.PLATFORM_BACKEND, memos.ACTION_CLOSET_BID
        ),
    )
    result = submit_and_wait(tx, client, bidder).result
    assert result["meta"]["TransactionResult"] == "tesSUCCESS", result["meta"]["TransactionResult"]
    return result


def _deps(path: str, statuses: dict) -> cmf.ClosetMarketDeps:
    async def payload_status(uuid):
        return statuses.get(uuid)

    async def no_mirror(conn, owner):
        return None

    def factory():
        conn = sqlite3.connect(path)
        economy_store.init_economy_schema(conn)
        return conn

    return cmf.ClosetMarketDeps(
        conn_factory=factory,
        app_account=config.SIGNING_ACCOUNT,
        fee_bps=700,
        payload_status_fn=payload_status,
        get_tx_fn=xrpl_ops.get_tx,
        get_escrow_fn=xrpl_ops.get_escrow,
        escrow_finish_fn=xrpl_ops.escrow_finish,
        escrow_cancel_fn=xrpl_ops.escrow_cancel,
        payment_fn=xrpl_ops.app_brix_payment,
        find_txs_fn=xrpl_ops.find_app_txs_by_memo,
        ledger_index_fn=xrpl_ops.current_validated_ledger_index,
        mirror_fn=no_mirror,
        unseal_fn=crypto_condition.unseal,
        records_dir=os.path.dirname(path),
        ledger_margin=config.CLOSET_MARKET_LEDGER_MARGIN,
    )


def _open_bid(client, path, statuses, bidder: Wallet, cancel_after: int) -> str:
    pre = crypto_condition.new_preimage()
    condition = crypto_condition.condition_hex(pre)
    conn = sqlite3.connect(path)
    economy_store.init_economy_schema(conn)
    order = cms.create_pending_bid(
        conn,
        owner=bidder.classic_address,
        slot="Head",
        value="Crown",
        price_brix=PRICE,
        platform=None,
        condition=condition,
        fulfillment_enc=crypto_condition.seal(crypto_condition.fulfillment_hex(pre)),
        cancel_after=cancel_after,
        payload_uuid=f"e2e-{time.time_ns()}",
        xumm_url=None,
        qr_url=None,
        push=None,
    )
    conn.close()
    created = _create_escrow(client, bidder, condition, cancel_after)
    statuses[order["payload_uuid"]] = {
        "signed": True,
        "account": bidder.classic_address,
        "txid": created["hash"],
    }
    asyncio.run(cmf.advance_bid(order["id"], _deps(path, statuses)))
    conn = sqlite3.connect(path)
    state = cms.get_order(conn, order["id"])["state"]
    conn.close()
    assert state == cms.OPEN, f"bid did not open: {state}"
    return str(order["id"])


def main() -> int:
    if config.XRPL_NETWORK != "testnet" or config.ECONOMY_NETWORK != "testnet":
        print("refusing: testnet only")
        return 2
    config.CLOSET_MARKET_ENC_KEY = Fernet.generate_key().decode()
    client = JsonRpcClient(config.JSON_RPC_URL)
    bidder, seller = generate_faucet_wallet(client), generate_faucet_wallet(client)
    _fund_with_brix(client, bidder, "100")
    _fund_with_brix(client, seller, "0")
    path = os.path.join(tempfile.mkdtemp(prefix="closet-e2e-"), "onchain_e2e.db")
    conn = sqlite3.connect(path)
    economy_store.init_economy_schema(conn)
    for w in (bidder, seller):
        economy_store.set_closet_token(
            conn, w.classic_address, f"E2E-{w.classic_address}", "00", status=ct.ACTIVE
        )
    economy_store.set_closet_contents(conn, seller.classic_address, [("Head", "Crown", 1)], [])
    conn.commit()
    conn.close()
    statuses: dict = {}
    ripple_now = int(time.time()) - xrpl_ops.RIPPLE_EPOCH_OFFSET
    ok = True

    # 1. holder fill: escrow finish -> move -> forward 9.3 -> mirrored
    bid_id = _open_bid(client, path, statuses, bidder, ripple_now + 3600)
    before_b, before_s = _balance(bidder.classic_address), _balance(seller.classic_address)
    conn = sqlite3.connect(path)
    fill = cms.fill_bid(conn, bid_id, seller.classic_address, fee_bps=700, platform=None)
    conn.close()
    state = asyncio.run(cmf.settle_fill(fill["id"], _deps(path, statuses)))
    got_s = _balance(seller.classic_address) - before_s
    print(
        f"[fill] state={state} seller +{got_s} bidder {_balance(bidder.classic_address) - before_b}"
    )
    ok &= state == cms.MIRRORED and got_s == Decimal("9.3")

    # 2. user cancel before expiry: EscrowFinish + refund Payment
    bid_id = _open_bid(client, path, statuses, bidder, ripple_now + 3600)
    before_b = _balance(bidder.classic_address)
    conn = sqlite3.connect(path)
    cms.begin_cancel_bid(conn, bid_id, owner=bidder.classic_address, reason="user")
    conn.close()
    asyncio.run(cmf.advance_bid(bid_id, _deps(path, statuses)))
    conn = sqlite3.connect(path)
    state = cms.get_order(conn, bid_id)["state"]
    conn.close()
    print(f"[cancel] state={state} bidder +{_balance(bidder.classic_address) - before_b}")
    ok &= state == cms.CANCELLED and _balance(bidder.classic_address) - before_b == Decimal(PRICE)

    # 3. expiry: EscrowCancel after CancelAfter
    bid_id = _open_bid(
        client, path, statuses, bidder, int(time.time()) - xrpl_ops.RIPPLE_EPOCH_OFFSET + 30
    )
    before_b = _balance(bidder.classic_address)
    time.sleep(30 + cmf.CANCEL_SLACK_SECONDS + 10)
    asyncio.run(cmf.advance_bid(bid_id, _deps(path, statuses)))
    conn = sqlite3.connect(path)
    state = cms.get_order(conn, bid_id)["state"]
    conn.close()
    print(f"[expire] state={state} bidder +{_balance(bidder.classic_address) - before_b}")
    ok &= state == cms.EXPIRED and _balance(bidder.classic_address) - before_b == Decimal(PRICE)

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
