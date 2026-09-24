# tests/test_resume_signing_context.py
# Task 7 (agent users PR C): a resumed bulk mint or burn2mint keeps the
# signing mode it started with (agent users §1, Amendment 1) — a
# client-signed session's payloads stay client-signed across a restart.
#
# Env-guard preamble: importing lfg_service.app freezes lfg_core.config
# constants at import time; set the same defaults test_bulk_mint_flow.py /
# test_bulk_mint_service.py use so collection order can't strand them.
import os
import sys

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test seed
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

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

    asyncio.new_event_loop().run_until_complete(main())
    assert seen["ctx"] == ("agent", W)
