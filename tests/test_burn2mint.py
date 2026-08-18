# tests/test_burn2mint.py
# Burn-to-mint (#220): user-signed burns -> cap-exempt bulk mint job.
# Covers: fail-closed ownership verify; only VALIDATED burns count; the
# entitlement seam's cap-exemption (job proceeds with zero headroom, no
# reservation taken); crash-resume keeps the burn credit; mint failure ->
# durable mint_credits row; feature flag off -> 403.
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

from lfg_core import (  # noqa: E402
    bulk_mint_flow,
    burn2mint_flow,
    config,
    headroom,
    mint_credits,
    mint_flow,
    supply,
)
from lfg_service import app as server  # noqa: E402

WALLET = "rUSERUSERUSERUSERUSERUSERUSERUSER"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(burn2mint_flow, "JOBS_DIR", str(tmp_path / "b2m"))
    monkeypatch.setattr(bulk_mint_flow, "JOBS_DIR", str(tmp_path / "bulk"))
    monkeypatch.setattr(
        bulk_mint_flow.db_path, "app_db_path", lambda net=None: str(tmp_path / "app.db")
    )
    monkeypatch.setattr(supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(headroom.nft_index, "index_db_path", lambda net: str(tmp_path / "idx.db"))
    yield


def _own_info(nft_id, **over):
    info = {
        "nft_id": nft_id,
        "owner": WALLET,
        "flags": 25,
        "uri_hex": "",
        "is_burned": False,
        "issuer": config.SIGNING_ACCOUNT,
        "taxon": config.NFT_TAXON,
    }
    info.update(over)
    return info


def _info_fn(**per_id):
    async def _inner(nft_id):
        return per_id.get(nft_id, _own_info(nft_id))

    return _inner


def _session(nft_ids=("A", "B")):
    return burn2mint_flow.Burn2MintSession(
        discord_id="u1", wallet_address=WALLET, nft_ids=list(nft_ids), platform="discord"
    )


# -- fail-closed ownership verify ------------------------------------------


def test_verify_ownership_fail_closed_on_indeterminate_lookup():
    err = _run(burn2mint_flow.verify_ownership(WALLET, ["A"], nft_info=_info_fn(A=None)))
    assert err == "ownership_unverifiable:A"


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"owner": "rSOMEONEELSE"}, "not_owner:A"),
        ({"is_burned": True}, "already_burned:A"),
        ({"issuer": "rFOREIGNISSUER"}, "not_collection:A"),
        ({"taxon": 999}, "not_collection:A"),
    ],
)
def test_verify_ownership_rejects(over, expected):
    err = _run(
        burn2mint_flow.verify_ownership(WALLET, ["A"], nft_info=_info_fn(A=_own_info("A", **over)))
    )
    assert err == expected


def test_verify_ownership_accepts_own_live_collection_nfts():
    assert _run(burn2mint_flow.verify_ownership(WALLET, ["A", "B"], nft_info=_info_fn())) is None


# -- only VALIDATED burns count --------------------------------------------


def _status(signed=True, account=WALLET, txid="TX1", expired=False):
    async def _inner(uuid):
        return {"signed": signed, "account": account, "txid": txid, "expired": expired}

    return _inner


def _tx(result="tesSUCCESS", nft_id="A", account=WALLET, validated=True):
    async def _inner(tx_hash):
        return {
            "validated": validated,
            "meta": {"TransactionResult": result},
            "tx_json": {"NFTokenID": nft_id, "Account": account},
        }

    return _inner


def _armed(nft_ids=("A",)):
    s = _session(nft_ids)
    b = s.burns[0]
    b.state = burn2mint_flow.B_AWAITING_SIGNATURE
    b.payload_uuid = "12345678-0000-0000-0000-000000000000"
    return s, b


def test_validated_tes_burn_is_recorded_durably():
    s, b = _armed()
    resolved = _run(
        burn2mint_flow._check_signed_burn(s, b, get_payload_status=_status(), get_tx=_tx())
    )
    assert resolved and b.state == burn2mint_flow.B_BURNED
    assert b.tx_hash == "TX1"
    # Durable BEFORE anything else: the record on disk already carries the burn.
    with open(burn2mint_flow._record_path(s.id)) as f:
        rec = json.load(f)
    assert rec["burns"][0]["state"] == burn2mint_flow.B_BURNED


def test_failed_onledger_burn_earns_no_entitlement():
    s, b = _armed()
    resolved = _run(
        burn2mint_flow._check_signed_burn(
            s, b, get_payload_status=_status(), get_tx=_tx(result="tecNO_ENTRY")
        )
    )
    assert resolved and b.state == burn2mint_flow.B_FAILED
    assert s.burned_ids() == []


def test_signer_mismatch_earns_no_entitlement():
    s, b = _armed()
    resolved = _run(
        burn2mint_flow._check_signed_burn(
            s, b, get_payload_status=_status(account="rATTACKER"), get_tx=_tx()
        )
    )
    assert resolved and b.state == burn2mint_flow.B_FAILED
    assert b.error == "signer_mismatch"


def test_wrong_nft_in_validated_tx_earns_no_entitlement():
    s, b = _armed()
    resolved = _run(
        burn2mint_flow._check_signed_burn(
            s, b, get_payload_status=_status(), get_tx=_tx(nft_id="OTHER")
        )
    )
    assert resolved and b.state == burn2mint_flow.B_FAILED


def test_expired_unsigned_payload_fails_burn():
    s, b = _armed()
    resolved = _run(
        burn2mint_flow._check_signed_burn(
            s, b, get_payload_status=_status(signed=False, expired=True), get_tx=_tx()
        )
    )
    assert resolved and b.state == burn2mint_flow.B_FAILED


def test_unvalidated_tx_keeps_polling():
    s, b = _armed()
    resolved = _run(
        burn2mint_flow._check_signed_burn(
            s, b, get_payload_status=_status(), get_tx=_tx(validated=False)
        )
    )
    assert not resolved
    assert b.state == burn2mint_flow.B_AWAITING_SIGNATURE


# -- cap-exemption at the seam ---------------------------------------------


def test_burn_job_proceeds_with_zero_headroom(tmp_path, monkeypatch):
    """The seam's cap-exemption: a burn entitlement mints even when the
    collection is at MAX_COLLECTION_SIZE — no reservation is ever taken."""
    monkeypatch.setattr(supply, "current_supply", lambda net: config.MAX_COLLECTION_SIZE)
    s = _session(("A", "B"))
    for b in s.burns:
        b.state = burn2mint_flow.B_BURNED
    job = burn2mint_flow.build_mint_job(s)  # would raise CollectionFull if not exempt
    assert job.quantity == 2
    assert job.entitlement.source == "burn"
    assert job.entitlement.cap_exempt is True
    assert job.state == bulk_mint_flow.PAID
    # No headroom reservation exists for a cap-exempt job.
    assert headroom.outstanding(str(tmp_path / "app.db")) == 0
    assert bulk_mint_flow.headroom_snapshot(job) is None


def test_payment_job_at_cap_still_raises(monkeypatch):
    """Control: the cap-exemption is per-entitlement, not a global bypass."""
    monkeypatch.setattr(supply, "current_supply", lambda net: config.MAX_COLLECTION_SIZE)
    j = bulk_mint_flow.BulkMintJob(
        discord_id="u1", wallet_address=WALLET, requested_qty=1, platform="discord"
    )
    with pytest.raises(bulk_mint_flow.CollectionFull):
        j.clamp_to_headroom()


def _fake_mint_ok():
    async def _inner(*, nft_number, **kwargs):
        return mint_flow.UnitResult(
            nft_number=nft_number,
            nft_id=f"nft-{nft_number}",
            image_url="https://cdn.example/img.png",
            offer_id=f"offer-{nft_number}",
            accept={"xumm_url": "x"},
            error=None,
        )

    return _inner


def _fake_mint_fail():
    async def _inner(*, nft_number, **kwargs):
        return mint_flow.UnitResult(
            nft_number=nft_number,
            nft_id=None,
            image_url=None,
            offer_id=None,
            accept=None,
            error="mint failed",
        )

    return _inner


def _fake_alloc(monkeypatch):
    counter = {"n": 4000}

    async def _inner():
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(bulk_mint_flow.mint_flow, "_allocate_nft_number", _inner)


def test_burn_job_fulfills_at_full_collection(monkeypatch):
    monkeypatch.setattr(supply, "current_supply", lambda net: config.MAX_COLLECTION_SIZE)
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _fake_mint_ok())
    _fake_alloc(monkeypatch)
    s = _session(("A", "B"))
    for b in s.burns:
        b.state = burn2mint_flow.B_BURNED
    job = burn2mint_flow.build_mint_job(s)
    _run(bulk_mint_flow.run_bulk_mint_job(job))
    assert job.state == bulk_mint_flow.DONE
    assert all(u.state == bulk_mint_flow.OFFERED for u in job.units)


# -- mint failure -> durable mint credit, never a lost burn -----------------


def test_mint_failure_becomes_durable_credit(tmp_path, monkeypatch):
    monkeypatch.setattr(bulk_mint_flow.mint_flow, "mint_one_unit", _fake_mint_fail())
    _fake_alloc(monkeypatch)
    s = _session(("A",))
    s.burns[0].state = burn2mint_flow.B_BURNED
    job = burn2mint_flow.build_mint_job(s)
    _run(bulk_mint_flow.run_bulk_mint_job(job))
    assert job.units[0].state == bulk_mint_flow.UNIT_FAILED
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", job.network) == 1


# -- crash-resume keeps the burn credit ------------------------------------


def test_resume_converts_validated_burns(monkeypatch):
    """A crash mid-signing-loop: the validated burn converts into a mint job;
    the never-signed remainder is NOT burned and earns nothing."""
    s = _session(("A", "B"))
    s.burns[0].state = burn2mint_flow.B_BURNED
    s.burns[0].tx_hash = "TX1"
    s.current = 1
    assert burn2mint_flow.persist(s)

    launched = []

    async def _launch(job):
        launched.append(job)
        return job

    sessions = _run(burn2mint_flow.resume_all(_launch))
    assert len(sessions) == 1
    resumed = sessions[0]
    assert resumed.state == burn2mint_flow.FULFILLING
    assert resumed.bulk_job_id == burn2mint_flow.bulk_job_id_for(resumed)
    assert len(launched) == 1
    job = launched[0]
    assert job.quantity == 1
    assert job.entitlement.burn_nft_ids == ["A"]
    assert resumed.burns[1].state == burn2mint_flow.B_FAILED
    # The mint job record is durable — a second crash resumes it via the
    # normal bulk sweep (state PAID is resumable).
    assert job.state == bulk_mint_flow.PAID
    with open(bulk_mint_flow._record_path(job.id)) as f:
        assert json.load(f)["state"] == bulk_mint_flow.PAID


def test_resume_conversion_is_idempotent_by_job_id():
    """The job id derives from the session id, so re-converting after a crash
    between job-persist and session-persist targets the SAME record — the
    service's _launch_burn_mint_job adopts a registered job instead of
    re-creating it (no double mint)."""
    s = _session(("A",))
    s.burns[0].state = burn2mint_flow.B_BURNED
    job1 = burn2mint_flow.build_mint_job(s)
    job2 = burn2mint_flow.build_mint_job(s)
    assert job1.id == job2.id == burn2mint_flow.bulk_job_id_for(s)


def test_resume_with_no_validated_burns_fails_session():
    s = _session(("A",))
    assert burn2mint_flow.persist(s)

    async def _launch(job):  # pragma: no cover - must not be called
        raise AssertionError("no job should launch without validated burns")

    sessions = _run(burn2mint_flow.resume_all(_launch))
    assert sessions[0].state == burn2mint_flow.FAILED


# -- review hardening: durability around irreversible burns -----------------


def test_unpersistable_burn_payload_is_never_exposed(monkeypatch):
    """Persist-before-expose: if the session record can't be written after
    XUMM created the payload, the payload is withdrawn and the call fails —
    a signable burn must never exist without a durable record of its uuid."""
    real_persist = burn2mint_flow.persist
    calls = {"n": 0}

    def _flaky_persist(session):
        calls["n"] += 1
        if calls["n"] == 1:  # the persist-before-expose write
            return False
        return real_persist(session)

    monkeypatch.setattr(burn2mint_flow, "persist", _flaky_persist)

    async def _payload(*a, **k):
        return {"uuid": "u-1", "xumm_url": "https://xumm.app/sign/u-1"}

    cancelled = []

    async def _cancel(uuid):
        cancelled.append(uuid)
        return True

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "create_burn_payload", _payload)
    monkeypatch.setattr(burn2mint_flow.xumm_ops, "cancel_xumm_payload", _cancel)
    s = _session(("A",))
    ok = _run(burn2mint_flow.start_next_burn(s, nft_info=_info_fn()))
    assert ok is False
    assert s.burns[0].state == burn2mint_flow.B_FAILED
    assert s.burns[0].payload_link is None  # nothing signable exposed
    assert cancelled == ["u-1"]  # payload withdrawn at XUMM


def test_orphan_payload_is_journaled_when_cancel_unconfirmed(monkeypatch):
    """If the session record can't be written AND XUMM doesn't confirm the
    cancel, the payload uuid is journaled to a dedicated orphan record that
    no session-record delete can drop — a signed-anyway burn stays
    discoverable and reconcilable."""
    monkeypatch.setattr(burn2mint_flow, "persist", lambda session: False)

    async def _payload(*a, **k):
        return {"uuid": "u-orphan", "xumm_url": "https://xumm.app/sign/u-orphan"}

    async def _cancel(uuid):
        return False  # cancel unconfirmed: the payload may still be signed

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "create_burn_payload", _payload)
    monkeypatch.setattr(burn2mint_flow.xumm_ops, "cancel_xumm_payload", _cancel)
    s = _session(("A",))
    ok = _run(burn2mint_flow.start_next_burn(s, nft_info=_info_fn()))
    assert ok is False
    # Dedicated durable journal — survives delete_record(session.id).
    with open(burn2mint_flow._orphan_record_path("u-orphan")) as f:
        rec = json.load(f)
    assert rec["payload_uuid"] == "u-orphan"
    assert rec["nft_id"] == "A"
    assert rec["wallet_address"] == WALLET
    burn2mint_flow.delete_record(s.id)  # the start handler's failure path
    assert json.load(open(burn2mint_flow._orphan_record_path("u-orphan")))
    # The orphan record is never picked up as a resumable session.
    assert burn2mint_flow.load_all_resumable() == []


def test_no_orphan_journal_when_cancel_confirmed(monkeypatch):
    monkeypatch.setattr(burn2mint_flow, "persist", lambda session: False)

    async def _payload(*a, **k):
        return {"uuid": "u-dead", "xumm_url": "https://xumm.app/sign/u-dead"}

    async def _cancel(uuid):
        return True  # confirmed withdrawn: nothing signable survives

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "create_burn_payload", _payload)
    monkeypatch.setattr(burn2mint_flow.xumm_ops, "cancel_xumm_payload", _cancel)
    s = _session(("A",))
    assert _run(burn2mint_flow.start_next_burn(s, nft_info=_info_fn())) is False
    assert not os.path.exists(burn2mint_flow._orphan_record_path("u-dead"))


def _write_orphan(uuid="u-orphan", nft_id="A", discord_id="u1"):
    s = _session((nft_id,))
    s.discord_id = discord_id
    b = s.burns[0]
    b.payload_uuid = uuid
    assert burn2mint_flow._journal_orphan_payload(s, b)
    return burn2mint_flow._orphan_record_path(uuid), s


def test_orphan_recovery_credits_validated_burn(tmp_path, monkeypatch):
    """A signed-anyway orphan burn that validated tesSUCCESS is recovered
    AUTOMATICALLY on the startup sweep: durable mint credit + journal
    retired — never operator-only reconciliation."""
    path, s = _write_orphan()
    _patch_ledger(monkeypatch)  # signed, validated tesSUCCESS, NFTokenID=A
    _run(burn2mint_flow.recover_orphan_payloads())
    assert not os.path.exists(path)  # retired
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", s.network) == 1


def test_orphan_recovery_retires_expired_unsigned(tmp_path, monkeypatch):
    path, s = _write_orphan(uuid="u-exp")

    async def _status(uuid):
        return {"signed": False, "expired": True, "account": None, "txid": None}

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "get_payload_status", _status)
    _run(burn2mint_flow.recover_orphan_payloads())
    assert not os.path.exists(path)  # dead payload, nothing burned
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", s.network) == 0


def test_orphan_recovery_keeps_indeterminate(tmp_path, monkeypatch):
    """Fail-closed: unknown payload status never deletes the journal."""
    path, s = _write_orphan(uuid="u-unk")

    async def _status(uuid):
        return None

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "get_payload_status", _status)
    _run(burn2mint_flow.recover_orphan_payloads())
    assert os.path.exists(path)  # kept for the next sweep
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", s.network) == 0


def test_orphan_recovery_keeps_signed_but_unvalidated(tmp_path, monkeypatch):
    path, s = _write_orphan(uuid="u-pend")
    _patch_ledger(monkeypatch, validated=False)
    _run(burn2mint_flow.recover_orphan_payloads())
    assert os.path.exists(path)
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", s.network) == 0


def test_orphan_recovery_keeps_journal_on_credit_failure(monkeypatch):
    path, _s = _write_orphan(uuid="u-cred")
    _patch_ledger(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(burn2mint_flow.mint_credits, "add_credit", _boom)
    _run(burn2mint_flow.recover_orphan_payloads())
    assert os.path.exists(path)  # validated burn: journal kept until credited


def test_orphan_recovery_runs_inside_resume_all(tmp_path, monkeypatch):
    path, s = _write_orphan(uuid="u-resume")
    _patch_ledger(monkeypatch)

    async def _launch(job):  # pragma: no cover - no sessions to convert
        raise AssertionError("no resumable sessions in this test")

    _run(burn2mint_flow.resume_all(_launch))
    assert not os.path.exists(path)
    assert mint_credits.get_credits(str(tmp_path / "app.db"), "u1", s.network) == 1


def test_reconvert_adopts_existing_job_preserving_mint_progress():
    """Session write failed after the job record landed; the resumed job then
    made mint progress. Re-convert must ADOPT the existing record — never
    re-persist a fresh PAID job over minted units (re-mint risk)."""
    s = _session(("A",))
    s.burns[0].state = burn2mint_flow.B_BURNED
    job = burn2mint_flow.build_mint_job(s)
    # Simulate progress written by the resumed job before re-convert runs.
    job.state = bulk_mint_flow.FULFILLING
    job.units[0].state = bulk_mint_flow.OFFERED
    job.units[0].nft_id = "nft-minted-already"
    assert bulk_mint_flow.persist(job)
    launched = []

    async def _launch(j):
        launched.append(j)
        return j

    _run(burn2mint_flow.convert(s, _launch))
    assert s.state == burn2mint_flow.FULFILLING
    assert len(launched) == 1
    adopted = launched[0]
    # The adopted job carries the prior progress, and the on-disk record was
    # not overwritten back to a fresh PAID/PENDING shape.
    assert adopted.units[0].state == bulk_mint_flow.OFFERED
    assert adopted.units[0].nft_id == "nft-minted-already"
    with open(bulk_mint_flow._record_path(job.id)) as f:
        rec = json.load(f)
    assert rec["state"] == bulk_mint_flow.FULFILLING
    assert rec["units"][0]["state"] == bulk_mint_flow.OFFERED
    assert rec["units"][0]["nft_id"] == "nft-minted-already"


def test_reconvert_never_overwrites_unreadable_job_record(tmp_path):
    """Fail-closed: an existing-but-unreadable job record may hold mint
    progress — re-convert must not clobber it; the session stays retryable."""
    s = _session(("A",))
    s.burns[0].state = burn2mint_flow.B_BURNED
    path = bulk_mint_flow._record_path(burn2mint_flow.bulk_job_id_for(s))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{corrupt")

    async def _launch(job):  # pragma: no cover - must not be called
        raise AssertionError("must not launch over an unreadable record")

    _run(burn2mint_flow.convert(s, _launch))
    assert s.state == burn2mint_flow.AWAITING_BURNS
    with open(path) as f:
        assert f.read() == "{corrupt"  # untouched


def test_convert_requires_durable_job_before_fulfilling(monkeypatch):
    """Job-persist failure must NOT mark the session FULFILLING over an
    in-memory-only job: it stays AWAITING_BURNS so the next poll/restart
    re-converts (idempotent job id) — validated burns are never stranded."""
    s = _session(("A",))
    s.burns[0].state = burn2mint_flow.B_BURNED
    s.current = 1
    launched = []

    async def _launch(job):
        launched.append(job)
        return job

    monkeypatch.setattr(bulk_mint_flow, "persist", lambda job: False)
    _run(burn2mint_flow.convert(s, _launch))
    assert s.state == burn2mint_flow.AWAITING_BURNS  # NOT fulfilling
    assert s.bulk_job_id is None
    assert launched == []
    monkeypatch.undo()
    # Retry (what the next status poll / resume does) now converts fully.
    _run(burn2mint_flow.convert(s, _launch))
    assert s.state == burn2mint_flow.FULFILLING
    assert len(launched) == 1
    with open(bulk_mint_flow._record_path(launched[0].id)) as f:
        assert json.load(f)["state"] == bulk_mint_flow.PAID


def _patch_ledger(monkeypatch, *, signed=True, txid="TX1", validated=True, result="tesSUCCESS"):
    async def _status(uuid):
        return {"signed": signed, "account": WALLET, "txid": txid, "expired": False}

    async def _tx(tx_hash):
        return {
            "validated": validated,
            "meta": {"TransactionResult": result},
            "tx_json": {"NFTokenID": "A", "Account": WALLET},
        }

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "get_payload_status", _status)
    monkeypatch.setattr(burn2mint_flow.xrpl_ops, "get_tx", _tx)


def test_cancel_counts_burn_signed_just_before_cancel(monkeypatch):
    """Cancel after signing: the validated burn is counted and converted —
    a cancel can never orphan a burn."""
    _patch_ledger(monkeypatch)
    s, b = _armed(("A", "B"))
    launched = []

    async def _launch(job):
        launched.append(job)
        return job

    _run(burn2mint_flow.cancel(s, _launch))
    assert b.state == burn2mint_flow.B_BURNED
    assert s.state == burn2mint_flow.FULFILLING
    assert len(launched) == 1 and launched[0].entitlement.burn_nft_ids == ["A"]
    assert s.burns[1].state == burn2mint_flow.B_FAILED  # never-signed remainder


def test_cancel_parks_signed_but_unvalidated_burn(monkeypatch):
    """Cancel while the signed tx is still validating: the burn is NOT
    discarded — the session parks in AWAITING_BURNS until the tx resolves
    (it can still land on-ledger after the cancel)."""
    _patch_ledger(monkeypatch, validated=False)
    s, b = _armed(("A",))

    async def _launch(job):  # pragma: no cover - must not run while parked
        raise AssertionError("parked session must not convert")

    _run(burn2mint_flow.cancel(s, _launch))
    assert s.state == burn2mint_flow.AWAITING_BURNS
    assert b.state == burn2mint_flow.B_AWAITING_SIGNATURE
    assert b.txid == "TX1"


def test_cancel_parks_when_payload_cancel_unconfirmed(monkeypatch):
    """Unsigned in-flight payload whose XUMM cancel cannot be confirmed may
    still get signed — fail-closed: park, don't discard."""
    _patch_ledger(monkeypatch, signed=False, txid=None)

    async def _cancel(uuid):
        return False

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "cancel_xumm_payload", _cancel)
    s, b = _armed(("A",))

    async def _launch(job):  # pragma: no cover
        raise AssertionError("parked session must not convert")

    _run(burn2mint_flow.cancel(s, _launch))
    assert s.state == burn2mint_flow.AWAITING_BURNS
    assert b.state == burn2mint_flow.B_AWAITING_SIGNATURE


def test_cancel_discards_only_confirmed_dead_payload(monkeypatch):
    _patch_ledger(monkeypatch, signed=False, txid=None)

    async def _cancel(uuid):
        return True

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "cancel_xumm_payload", _cancel)
    s, b = _armed(("A",))

    async def _launch(job):  # pragma: no cover
        raise AssertionError("nothing burned; no job")

    _run(burn2mint_flow.cancel(s, _launch))
    assert s.state == burn2mint_flow.CANCELLED
    assert b.state == burn2mint_flow.B_FAILED


def test_resume_parks_signed_but_unvalidated_burn(monkeypatch):
    """Restart with a signed-but-unvalidated burn in flight: never discarded —
    the session stays AWAITING_BURNS (durable, resumable) until it resolves."""
    _patch_ledger(monkeypatch, validated=False)
    s, b = _armed(("A",))
    b.txid = "TX1"
    assert burn2mint_flow.persist(s)

    async def _launch(job):  # pragma: no cover
        raise AssertionError("parked session must not convert")

    sessions = _run(burn2mint_flow.resume_all(_launch))
    assert sessions[0].state == burn2mint_flow.AWAITING_BURNS
    assert sessions[0].burns[0].state == burn2mint_flow.B_AWAITING_SIGNATURE


# -- service layer: flag + endpoints ---------------------------------------


class _PostRequest:
    headers: dict[str, str] = {}

    def __init__(self, body):
        self._body = body
        self._store = {}

    async def json(self):
        return self._body

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value


@pytest.fixture
def dev_auth(monkeypatch):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "bulk_sessions", {})
    monkeypatch.setattr(server, "burn2mint_sessions", {})
    return server.burn2mint_sessions


def test_burn2mint_routes_registered():
    app = server.create_app()
    paths = {getattr(r.resource, "canonical", "") for r in app.router.routes()}
    assert "/api/mint/burn2mint" in paths
    assert "/api/mint/burn2mint/active" in paths
    assert "/api/mint/burn2mint/{session_id}" in paths
    assert "/api/mint/burn2mint/{session_id}/cancel" in paths


def test_flag_default_is_off(monkeypatch):
    assert config.BURN_TO_MINT_ENABLED_DEFAULT == "0"
    # Remove the var so an ambient .env cannot mask the shipped default.
    monkeypatch.delenv("BURN_TO_MINT_ENABLED", raising=False)
    assert config.env_flag("BURN_TO_MINT_ENABLED", config.BURN_TO_MINT_ENABLED_DEFAULT) is False


def test_start_refused_when_flag_off(dev_auth, monkeypatch):
    monkeypatch.setattr(server.config, "BURN_TO_MINT_ENABLED", False)
    resp = _run(server.handle_burn2mint_start(_PostRequest({"nft_ids": ["A"]})))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["error"] == "burn_to_mint_disabled"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"nft_ids": []},
        {"nft_ids": "A"},
        {"nft_ids": ["A", "A"]},
        {"nft_ids": [1]},
        {"nft_ids": [""]},
    ],
)
def test_start_rejects_invalid_nft_ids(dev_auth, monkeypatch, body):
    monkeypatch.setattr(server.config, "BURN_TO_MINT_ENABLED", True)
    resp = _run(server.handle_burn2mint_start(_PostRequest(body)))
    assert resp.status == 400


def test_start_rejects_over_bulk_max(dev_auth, monkeypatch):
    monkeypatch.setattr(server.config, "BURN_TO_MINT_ENABLED", True)
    ids = [f"N{i}" for i in range(server.config.BULK_MINT_MAX + 1)]
    resp = _run(server.handle_burn2mint_start(_PostRequest({"nft_ids": ids})))
    assert resp.status == 400


def test_start_fail_closed_on_unverifiable_ownership(dev_auth, monkeypatch):
    monkeypatch.setattr(server.config, "BURN_TO_MINT_ENABLED", True)

    async def _none(nft_id):
        return None

    monkeypatch.setattr(burn2mint_flow.xrpl_ops, "nft_info", _none)
    resp = _run(server.handle_burn2mint_start(_PostRequest({"nft_ids": ["A"]})))
    assert resp.status == 409
    assert json.loads(resp.body.decode())["error"] == "ownership_failed"


def test_start_happy_path_offers_first_burn(dev_auth, monkeypatch):
    monkeypatch.setattr(server.config, "BURN_TO_MINT_ENABLED", True)

    async def _ok(wallet, nft_ids, nft_info=None):
        return None

    monkeypatch.setattr(burn2mint_flow, "verify_ownership", _ok)

    async def _payload(*a, **k):
        return {"uuid": "u-1", "xumm_url": "https://xumm.app/sign/u-1"}

    monkeypatch.setattr(burn2mint_flow.xumm_ops, "create_burn_payload", _payload)
    resp = _run(server.handle_burn2mint_start(_PostRequest({"nft_ids": ["A", "B"]})))
    assert resp.status == 200
    d = json.loads(resp.body.decode())
    assert d["state"] == burn2mint_flow.AWAITING_BURNS
    assert d["burn_link"] == "https://xumm.app/sign/u-1"
    assert d["total"] == 2 and d["burned"] == 0


def test_launch_burn_mint_job_adopts_registered_job(dev_auth):
    s = _session(("A",))
    s.burns[0].state = burn2mint_flow.B_BURNED
    job = burn2mint_flow.build_mint_job(s)
    job.task = object()  # already running elsewhere; adopter must not relaunch
    server.bulk_sessions[job.id] = job

    async def _go():
        return await server._launch_burn_mint_job(burn2mint_flow.build_mint_job(s))

    adopted = _run(_go())
    assert adopted is job  # same object: never re-created, never double-minted
