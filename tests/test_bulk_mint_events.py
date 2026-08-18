# #253: bulk-mint units publish mint.completed firehose events (option 1 —
# per-unit events, mirroring the single-mint server-side publish posture).
# Env guard: set before lfg_core imports so frozen config constants are sane
# when this file runs first (see test-env-guard convention).
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
import json  # noqa: E402

import pytest  # noqa: E402

from lfg_core import bulk_mint_flow, headroom, mint_flow, supply  # noqa: E402


@pytest.fixture(autouse=True)
def _headroom_env(tmp_path, monkeypatch):
    """Same isolation as test_bulk_mint_flow (#226): tmp job records + app DB,
    supply pinned at 0."""
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(
        bulk_mint_flow.db_path, "app_db_path", lambda net=None: str(tmp_path / "app.db")
    )
    monkeypatch.setattr(supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(headroom.nft_index, "index_db_path", lambda net: str(tmp_path / "idx.db"))


def _job(qty):
    return bulk_mint_flow.BulkMintJob(
        discord_id="u1", wallet_address="rUSER", requested_qty=qty, platform="discord"
    )


def _async_counter(start=0):
    counter = {"n": start}

    async def _inner(*args, **kwargs):
        n = counter["n"]
        counter["n"] += 1
        return n

    return _inner


def _fake_mint_ok():
    async def _inner(*, nft_number, **kwargs):
        return mint_flow.UnitResult(
            nft_number=nft_number,
            nft_id=f"nft-{nft_number}",
            image_url="https://cdn.example/img.png",
            offer_id=f"offer-{nft_number}",
            accept={"xumm_url": "x"},
            error=None,
            traits={"Background": "Blue", "Hat": "Wizard Hat"},
            body_type="milady",
        )

    return _inner


def _recorder(calls):
    async def _hook(job, unit):
        calls.append((job.id, unit.index, unit.nft_id))

    return _hook


def _run_paid_job(monkeypatch, qty=2):
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number", _async_counter(4000))
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _fake_mint_ok())
    j = _job(qty)
    j.clamp_to_headroom()
    j.state = bulk_mint_flow.PAID
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j))
    return j


def test_each_offered_unit_publishes_exactly_one_event(monkeypatch):
    calls = []
    monkeypatch.setattr(bulk_mint_flow, "unit_event_publisher", _recorder(calls))
    j = _run_paid_job(monkeypatch, qty=3)
    assert j.state == bulk_mint_flow.DONE
    assert sorted(c[1] for c in calls) == [0, 1, 2]
    assert all(u.published for u in j.units)
    # traits/body_type propagated from UnitResult onto the durable unit
    assert j.units[0].traits == {"Background": "Blue", "Hat": "Wizard Hat"}
    assert j.units[0].body_type == "milady"
    # published flag is durable (survives the persisted record)
    with open(os.path.join(bulk_mint_flow.JOBS_DIR, f"{j.id}.json")) as f:
        rec = json.load(f)
    assert all(u["published"] for u in rec["units"])


def test_resume_does_not_republish_already_published_units(monkeypatch):
    calls = []
    monkeypatch.setattr(bulk_mint_flow, "unit_event_publisher", _recorder(calls))
    j = _run_paid_job(monkeypatch, qty=2)
    assert len(calls) == 2
    # Simulate restart: reload from the durable record and resume.
    with open(os.path.join(bulk_mint_flow.JOBS_DIR, f"{j.id}.json")) as f:
        j2 = bulk_mint_flow.BulkMintJob.from_serialized(json.load(f))
    j2.state = bulk_mint_flow.FULFILLING
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j2))
    assert len(calls) == 2  # no second event for any unit
    assert all(u.published for u in j2.units)


def test_resume_publishes_offered_but_unpublished_units(monkeypatch):
    """Crash window: unit reached OFFERED durably but the publish never ran.
    The resume's final sweep must emit the missing event exactly once."""
    calls = []
    # Pin the hook off for the first run (lfg_service.app sets it globally on
    # import, so full-suite order would otherwise publish here already).
    monkeypatch.setattr(bulk_mint_flow, "unit_event_publisher", None)
    hookless = _run_paid_job(monkeypatch, qty=1)  # no hook -> unpublished
    assert not hookless.units[0].published
    monkeypatch.setattr(bulk_mint_flow, "unit_event_publisher", _recorder(calls))
    with open(os.path.join(bulk_mint_flow.JOBS_DIR, f"{hookless.id}.json")) as f:
        j2 = bulk_mint_flow.BulkMintJob.from_serialized(json.load(f))
    j2.state = bulk_mint_flow.FULFILLING
    asyncio.run(bulk_mint_flow.run_bulk_mint_job(j2))
    assert len(calls) == 1
    assert j2.units[0].published


def test_publish_failure_never_breaks_fulfillment(monkeypatch):
    async def _boom(job, unit):
        raise RuntimeError("bus down")

    monkeypatch.setattr(bulk_mint_flow, "unit_event_publisher", _boom)
    j = _run_paid_job(monkeypatch, qty=2)
    assert j.state == bulk_mint_flow.DONE
    assert all(u.state == bulk_mint_flow.OFFERED for u in j.units)
    # left unpublished so a later resume can retry the event
    assert not any(u.published for u in j.units)


def test_service_event_shape_matches_single_mint(monkeypatch):
    """_publish_bulk_unit emits mint.completed with the MintSession.to_dict
    field set the consumers read (nft_number/nft_id/image_url/traits/body_type)."""
    from lfg_service import app as service_app

    events = []

    async def _capture(type_, identity_obj, wallet, data):
        events.append((type_, identity_obj, wallet, data))

    monkeypatch.setattr(service_app, "publish_event", _capture)
    j = _job(1)
    unit = bulk_mint_flow.Unit(
        index=0,
        state=bulk_mint_flow.OFFERED,
        nft_number=4001,
        nft_id="nft-4001",
        image_url="https://cdn.example/img.png",
        offer_id="offer-4001",
        traits={"Background": "Blue"},
        body_type="ape",
    )
    asyncio.run(service_app._publish_bulk_unit(j, unit))
    (type_, identity_obj, wallet, data) = events[0]
    assert type_ == "mint.completed"
    assert wallet == "rUSER"
    assert identity_obj["platform"] == "discord"
    assert data["nft_number"] == 4001
    assert data["nft_id"] == "nft-4001"
    assert data["image_url"] == "https://cdn.example/img.png"
    assert data["traits"] == {"Background": "Blue"}
    assert data["body_type"] == "ape"
    assert data["state"] == mint_flow.OFFER_READY
    assert data["bulk_job_id"] == j.id
    assert data["unit_index"] == 0
    # the X poster's dedup key requires a non-empty nft_id
    from surfaces.x_bot import poster

    assert poster.should_post({"type": type_, "data": data}) == "mint:nft-4001"
