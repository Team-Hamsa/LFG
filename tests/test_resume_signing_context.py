# tests/test_resume_signing_context.py
# Task 7 (agent users PR C): a resumed bulk mint or burn2mint keeps the
# signing mode it started with (agent users §1, Amendment 1) — a
# client-signed session's payloads stay client-signed across a restart.
#
# The root conftest.py pins every default this module's imports need (#323)
# before any test module loads, so only the sys.path setup for the direct
# `lfg_service.app` / `lfg_core` imports below is needed here.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

import lfg_service.app as app  # noqa: E402
from lfg_core import bulk_mint_flow, burn2mint_flow  # noqa: E402
from lfg_core.signing import context  # noqa: E402

W = "rWALLETWALLETWALLETWALLETWALLET1"


def test_bulk_job_records_and_restores_its_signing_context():
    with context.use("agent", W):
        job = bulk_mint_flow.BulkMintJob("u", W, 2, platform="web")
    d = job.serialize()
    assert (d["sign_provider"], d["sign_wallet"]) == ("agent", W)
    again = bulk_mint_flow.BulkMintJob.from_serialized(d)
    assert (again.sign_provider, again.sign_wallet) == ("agent", W)
    d.pop("sign_provider")
    d.pop("sign_wallet")  # a record from before this change
    legacy = bulk_mint_flow.BulkMintJob.from_serialized(d)
    assert (legacy.sign_provider, legacy.sign_wallet) == ("xaman", None)


def test_burn2mint_session_records_and_restores_its_signing_context():
    with context.use("agent", W):
        s = burn2mint_flow.Burn2MintSession("u", W, ["N1"], platform="web")
    again = burn2mint_flow.Burn2MintSession.from_serialized(s.serialize())
    assert (again.sign_provider, again.sign_wallet) == ("agent", W)


def test_a_resumed_bulk_job_runs_inside_its_signing_context(monkeypatch):
    seen = {}

    async def fake_run(job):
        seen["ctx"] = (context.current_provider(), context.current_wallet())

    monkeypatch.setattr(bulk_mint_flow, "run_bulk_mint_job", fake_run)
    with context.use("agent", W):
        job = bulk_mint_flow.BulkMintJob("u", W, 1, platform="web")

    async def main():  # outside any request context, like startup
        app._launch_bulk_task(job)
        await job.task

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
    assert seen["ctx"] == ("agent", W)
