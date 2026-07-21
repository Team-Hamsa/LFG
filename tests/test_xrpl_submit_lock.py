"""_submit_and_confirm must serialize per signing account: concurrent
backend-signed txs otherwise autofill the same sequence and one dies
tefPAST_SEQ (fire-and-forget harvests spec 2026-07-21)."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")
os.environ.setdefault("LAYER_SOURCE", "local")

from lfg_core import xrpl_ops  # noqa: E402


def test_concurrent_submits_serialize_per_account():
    order: list[str] = []

    def fake_autofill_and_sign(tx, client, wallet):
        order.append(f"sign:{tx}")
        return SimpleNamespace(get_hash=lambda: f"H{tx}")

    def fake_submit_and_wait(signed, client, wallet, autofill):
        order.append(f"submit:{signed.get_hash()}")
        return SimpleNamespace(
            result={"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}
        )

    wallet = SimpleNamespace(classic_address="rISSUER")

    async def go():
        with (
            patch.object(xrpl_ops, "autofill_and_sign", fake_autofill_and_sign),
            patch.object(xrpl_ops, "submit_and_wait", fake_submit_and_wait),
        ):
            await asyncio.gather(
                xrpl_ops._submit_and_confirm("tx1", wallet, None, "a"),
                xrpl_ops._submit_and_confirm("tx2", wallet, None, "b"),
            )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
    # Serialized: each tx's sign is immediately followed by its own submit —
    # never sign,sign,submit,submit (the sequence-collision interleaving).
    assert order in (
        ["sign:tx1", "submit:Htx1", "sign:tx2", "submit:Htx2"],
        ["sign:tx2", "submit:Htx2", "sign:tx1", "submit:Htx1"],
    )
