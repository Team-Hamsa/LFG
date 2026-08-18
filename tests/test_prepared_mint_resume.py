# #336: pin the sponsored prepared-mint resume path.
#
# `_resume_prepared_mint_one_unit` replays a journaled, already-signed
# NFTokenMint after a crash/relaunch instead of composing and signing a second
# transaction. Before this file the suite never executed its body end-to-end
# through `run_mint_session` (every sponsored session test monkeypatched
# `mint_one_unit` wholesale), and its post-mint tail — the part that shipped
# with two silent omissions (rarity + promote_still, restored in 4c485d3) —
# had no assertions. These tests drive the real function via
# `mint_one_unit(resume_prepared=...)` and via `run_mint_session` with a
# fully-populated claim journal, and pin the full tail so the next omission
# fails CI.
#
# Env-guard preamble (verbatim pattern from tests/test_mint_one_unit.py):
# importing lfg_core.config freezes its constants at import time.
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402

from lfg_core import config, mint_flow, sponsored_mint  # noqa: E402

_METADATA = {
    "name": "LFG #4000",
    "image": "https://cdn.example/4000/4000_0.png",
    "video": "https://cdn.example/4000/4000_0.mp4",
    "edition": 4000,
    "attributes": [
        {"trait_type": "Body", "value": "Alien"},
        {"trait_type": "Head", "value": "Crown"},
    ],
}


def _claim(**overrides) -> sponsored_mint.Claim:
    """A fully-populated resumable claim: every prepared-journal field set,
    exactly as `record_mint_prepared` + `rebind_reservation` leave it."""
    fields: dict[str, Any] = {
        "id": "claim-1",
        "network": "mainnet",
        "wallet": "rNEW",
        "campaign_id": "camp-1",
        "session_id": "sess-new",
        "status": "reserved",
        "reserved_at": 101,
        "reservation_expires_at": None,
        "released_at": None,
        "mint_signed_tx_hash": "A" * 64,
        "mint_signed_tx_blob": "BLOB:" + "A" * 64,
        "mint_signed_ledger_floor": 500,
        "mint_forwarded_at": None,
        "mint_nft_number": 4000,
        "mint_metadata_url": "https://cdn.example/4000/4000_0.json",
        "mint_metadata_json": json.dumps(_METADATA),
        "mint_body_type": "Alien",
        "mint_still_token": "sess-old",
        "mint_tx_hash": None,
        "nft_id": None,
        "offer_id": None,
        "accept_tx_hash": None,
        "tagged_at": None,
        "last_error": None,
        "created_at": 100,
        "updated_at": 101,
    }
    fields.update(overrides)
    return sponsored_mint.Claim(**fields)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reserved_numbers_guard():
    """The resume path adds the journaled number to the module-level in-process
    guard; failure paths deliberately KEEP it there. Restore between tests."""
    before = set(mint_flow._reserved_numbers)
    yield
    mint_flow._reserved_numbers.clear()
    mint_flow._reserved_numbers.update(before)


@pytest.fixture
def _resume_mocks(monkeypatch):
    """Stub every network/DB boundary the resume tail crosses, capturing the
    calls so the tail's obligations can be asserted one by one."""
    captured: dict[str, Any] = {"boost_clocks": [], "recalculated": 0, "archive": []}

    async def fake_submit(**kwargs):
        captured["submit_kwargs"] = kwargs
        return SimpleNamespace(state="validated", nft_id="NFTID1", error=None, tx_hash="A" * 64)

    async def fake_create_nft_offer(*args, **kwargs):
        captured["offer_args"] = args
        captured["offer_kwargs"] = kwargs
        return "OFFER1"

    async def fake_accept_payload(*args, **kwargs):
        captured["accept_args"] = args
        captured["accept_kwargs"] = kwargs
        return {"qr_url": "q", "xumm_url": "x", "uuid": "u"}

    def fake_record_nft_mint(**kwargs):
        captured["record_kwargs"] = kwargs
        return True

    fake_rarity = SimpleNamespace(
        connect=lambda: SimpleNamespace(close=lambda: None),
        start_boost_clock=lambda conn, body, ttype, value: captured["boost_clocks"].append(
            (body, ttype, value)
        ),
        recalculate_rarity=lambda conn: captured.__setitem__(
            "recalculated", captured["recalculated"] + 1
        ),
        BODY_SENTINEL="_body",
        BODY_CATEGORY="Body",
    )

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", fake_submit)
    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", fake_create_nft_offer)
    monkeypatch.setattr(mint_flow.xumm_ops, "create_accept_offer_payload", fake_accept_payload)
    monkeypatch.setattr(mint_flow, "record_nft_mint", fake_record_nft_mint)
    monkeypatch.setattr(mint_flow, "rarity", fake_rarity)
    monkeypatch.setattr(
        mint_flow.image_archive,
        "promote_still",
        lambda *args: captured["archive"].append(("promote", args)) or True,
    )
    monkeypatch.setattr(
        mint_flow.image_archive,
        "discard_still",
        lambda *args: captured["archive"].append(("discard", args)),
    )
    return captured


class _Callbacks:
    """Async sponsored-persistence callbacks, recording their invocations."""

    def __init__(self):
        self.forwarded: list[str] = []
        self.confirmed: list[tuple] = []
        self.offers: list[tuple] = []
        self.states: list[str] = []

    async def on_mint_forwarded(self, tx_hash):
        self.forwarded.append(tx_hash)

    async def on_mint_confirmed(self, nft_number, nft_id, tx_hash, image_url):
        self.confirmed.append((nft_number, nft_id, tx_hash, image_url))

    async def on_offer_created(self, offer_id, error):
        self.offers.append((offer_id, error))

    def on_state(self, state):
        self.states.append(state)


def _run_resume(claim, cb: _Callbacks):
    return _run(
        mint_flow.mint_one_unit(
            discord_id="dev",
            wallet_address="rNEW",
            platform="discord",
            push_user_token=None,
            return_url=None,
            nft_number=4000,
            session_tag="sess-new",
            resume_prepared=claim,
            on_state=cb.on_state,
            on_mint_forwarded=cb.on_mint_forwarded,
            on_mint_confirmed=cb.on_mint_confirmed,
            on_offer_created=cb.on_offer_created,
        )
    )


# --- happy path: the full post-mint tail --------------------------------------


def test_resume_happy_path_runs_the_full_post_mint_tail(_resume_mocks, monkeypatch):
    cb = _Callbacks()
    res = _run_resume(_claim(), cb)

    # The persisted signed identity is replayed, never re-signed.
    assert _resume_mocks["submit_kwargs"] == {
        "signed_tx_blob": "BLOB:" + "A" * 64,
        "signed_tx_hash": "A" * 64,
    }
    assert cb.forwarded == ["A" * 64]
    assert cb.states == [mint_flow.MINTING, mint_flow.CREATING_OFFER]

    # promote_still uses the COMPOSING session's persisted token (#330),
    # never the current session's tag.
    assert _resume_mocks["archive"] == [("promote", (config.XRPL_NETWORK, 4000, "sess-old"))]

    # on_mint_confirmed fires with the validated identity before offer work.
    assert cb.confirmed == [(4000, "NFTID1", "A" * 64, _METADATA["image"])]

    # The projected LFG row: traits carry the Head -> Hat column remap.
    assert _resume_mocks["record_kwargs"] == {
        "nft_number": 4000,
        "nft_id": "NFTID1",
        "discord_id": "dev",
        "owner_address": "rNEW",
        "metadata_url": "https://cdn.example/4000/4000_0.json",
        "image_url": _METADATA["image"],
        "traits": {"Body": "Alien", "Hat": "Crown"},
        "network": config.XRPL_NETWORK,
        "body_type": "Alien",
        "referrer": None,
    }

    # Rarity accounting (restored in 4c485d3 — the omission this pins): one
    # boost clock per attribute plus the body sentinel, then a recalc.
    assert _resume_mocks["boost_clocks"] == [
        ("Alien", "Body", "Alien"),
        ("Alien", "Head", "Crown"),
        ("_body", "Body", "Alien"),
    ]
    assert _resume_mocks["recalculated"] == 1

    # Offer + accept payload, pinned to the claim wallet.
    assert _resume_mocks["offer_args"][0] == "NFTID1"
    assert _resume_mocks["offer_args"][1] == "rNEW"
    assert cb.offers == [("OFFER1", None)]
    assert _resume_mocks["accept_kwargs"]["account"] == "rNEW"

    # Full UnitResult contract.
    assert res.error is None
    assert res.nft_number == 4000
    assert res.nft_id == "NFTID1"
    assert res.offer_id == "OFFER1"
    assert res.accept == {"qr_url": "q", "xumm_url": "x", "uuid": "u"}
    assert res.image_url == _METADATA["image"]
    assert res.video_url == _METADATA["video"]
    assert res.traits == {"Body": "Alien", "Hat": "Crown"}
    assert res.body_type == "Alien"
    assert res.mint_tx_hash == "A" * 64
    assert not res.mint_definitively_failed

    # Saved -> the in-process reservation is released.
    assert 4000 not in mint_flow._reserved_numbers


def test_resume_path_is_actually_executed_not_just_mocked(_resume_mocks):
    """#336 acceptance criterion 5: prove the tests reach the real function
    body (a settrace spot-check), not merely a mock of it."""
    code = mint_flow._resume_prepared_mint_one_unit.__code__
    hit: set[int] = set()

    def tracer(frame, event, arg):
        if frame.f_code is code:
            hit.add(frame.f_lineno)
            return tracer
        return tracer if event == "call" else None

    sys.settrace(tracer)
    try:
        res = _run_resume(_claim(), _Callbacks())
    finally:
        sys.settrace(None)
    assert res.error is None
    first, last = code.co_firstlineno, max(code.co_lines(), key=lambda t: t[2] or 0)[2]
    body_lines = {ln for ln in hit if ln > first}
    # Dozens of distinct body lines spanning the tail must have executed.
    assert len(body_lines) > 30, f"resume body barely executed: {sorted(body_lines)}"
    assert max(body_lines) > first + 100, (first, last, max(body_lines))


# --- failure paths -------------------------------------------------------------


def test_resume_validated_failure_is_definitive_and_keeps_confirmed_data(
    _resume_mocks, monkeypatch
):
    async def submit(**kwargs):
        return SimpleNamespace(state="failed", nft_id=None, error="tec failure", tx_hash=None)

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", submit)

    res = _run_resume(_claim(), _Callbacks())

    assert res.mint_definitively_failed
    assert res.error == "tec failure"
    assert res.nft_id is None
    assert res.nft_number == 4000
    # traits/body/tx-hash stay populated so the caller can restore the promise.
    assert res.traits == {"Body": "Alien", "Hat": "Crown"}
    assert res.body_type == "Alien"
    assert res.mint_tx_hash == "A" * 64
    assert res.image_url == _METADATA["image"]
    assert res.video_url == _METADATA["video"]
    # The signed identity is retired for good: the staged still is discarded
    # with the persisted composing token.
    assert _resume_mocks["archive"] == [("discard", (config.XRPL_NETWORK, 4000, "sess-old"))]


def test_resume_indeterminate_submission_never_sets_definitive_failure(_resume_mocks, monkeypatch):
    async def submit(**kwargs):
        return SimpleNamespace(state="indeterminate", nft_id=None, error="timeout", tx_hash=None)

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", submit)

    res = _run_resume(_claim(), _Callbacks())

    # IndeterminateResultError is raised internally; the except branch turns
    # it into an error result WITHOUT the definitive-failure marker, keeps the
    # reservation, and never discards the staged still (a later resume may
    # still confirm and promote it).
    assert res.error == "timeout"
    assert not res.mint_definitively_failed
    assert res.nft_id is None
    assert res.mint_tx_hash == "A" * 64
    assert _resume_mocks["archive"] == []
    assert 4000 in mint_flow._reserved_numbers


def test_resume_offer_creation_failure_reports_the_minted_nft(_resume_mocks, monkeypatch):
    async def no_offer(*args, **kwargs):
        return None

    monkeypatch.setattr(mint_flow.xrpl_ops, "create_nft_offer", no_offer)

    cb = _Callbacks()
    res = _run_resume(_claim(), cb)

    assert res.nft_id == "NFTID1"
    assert res.offer_id is None
    assert res.accept is None
    assert res.error is not None and "offer creation failed" in res.error
    assert res.traits == {"Body": "Alien", "Hat": "Crown"}
    assert res.mint_tx_hash == "A" * 64
    # on_offer_created is told about the failure so it can be persisted.
    assert cb.offers == [(None, res.error)]


def test_resume_exception_after_confirmation_keeps_derived_data_and_reservation(
    _resume_mocks, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("xumm exploded")

    monkeypatch.setattr(mint_flow.xumm_ops, "create_accept_offer_payload", boom)

    res = _run_resume(_claim(), _Callbacks())

    # The mint landed: the except branch (deliberately opposite to
    # mint_one_unit's) must return everything already derived, keep the
    # reserved number, and leave the promoted still alone.
    assert res.error == "xumm exploded"
    assert res.nft_id == "NFTID1"
    assert res.traits == {"Body": "Alien", "Hat": "Crown"}
    assert res.body_type == "Alien"
    assert res.image_url == _METADATA["image"]
    assert res.video_url == _METADATA["video"]
    assert res.mint_tx_hash == "A" * 64
    assert ("discard", (config.XRPL_NETWORK, 4000, "sess-old")) not in _resume_mocks["archive"]
    # The DB save succeeded before the explosion, so the saved branch already
    # released the in-process reservation; the except branch itself performs
    # no discard/release of its own (unlike mint_one_unit's).
    assert 4000 not in mint_flow._reserved_numbers
    # record + rarity already ran before the explosion.
    assert _resume_mocks["record_kwargs"]["nft_id"] == "NFTID1"
    assert _resume_mocks["recalculated"] == 1


def test_resume_early_exception_re_derives_image_from_persisted_metadata(
    _resume_mocks, monkeypatch
):
    async def submit_boom(**kwargs):
        raise RuntimeError("socket died")

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", submit_boom)

    # image/video were derived before submission, so they come back populated
    # even though the failure happened before confirmation; the except branch
    # additionally re-derives them from the persisted metadata JSON when they
    # were never computed at all.
    res = _run_resume(_claim(), _Callbacks())
    assert res.error == "socket died"
    assert res.nft_id is None
    assert res.image_url == _METADATA["image"]
    assert res.video_url == _METADATA["video"]
    assert res.body_type == "Alien"


def test_resume_refuses_an_incomplete_claim(_resume_mocks, monkeypatch):
    async def submit_forbidden(**kwargs):
        raise AssertionError("an incomplete claim must never be submitted")

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", submit_forbidden)

    res = _run_resume(_claim(mint_signed_tx_blob=None), _Callbacks())
    assert res.error == "prepared sponsored claim is incomplete"
    assert res.nft_id is None
    assert not res.mint_definitively_failed


# --- run_mint_session drives the resume for real -------------------------------


def _session():
    return mint_flow.MintSession("dev", "rNEW", platform="discord", sponsored=True)


async def _noop(*args):
    return None


def _run_session(monkeypatch, claim, offers=None):
    """Drive run_mint_session with claim_for_session returning `claim`,
    WITHOUT monkeypatching mint_one_unit — the delegation at the
    resume_prepared seam runs for real."""
    monkeypatch.setattr(mint_flow.sponsored_mint, "claim_for_session", lambda *a, **k: claim)

    async def forbidden_payment(*args, **kwargs):
        raise AssertionError("sponsored session reached a payment operation")

    async def forbidden_allocate():
        raise AssertionError("a fully-journaled claim must reuse its nft_number")

    monkeypatch.setattr(mint_flow.xrpl_ops, "wait_for_payment", forbidden_payment)
    monkeypatch.setattr(mint_flow, "_allocate_nft_number", forbidden_allocate)

    async def on_offer(offer_id, error):
        if offers is not None:
            offers.append((offer_id, error))

    session = _session()
    _run(
        mint_flow.run_mint_session(
            session,
            on_sponsored_mint=_noop,
            on_sponsored_prepared=_noop,
            on_sponsored_forwarded=_noop,
            on_sponsored_offer=on_offer,
        )
    )
    return session


def test_run_mint_session_resumes_a_fully_journaled_claim(_resume_mocks, monkeypatch):
    offers: list[tuple] = []
    session = _run_session(monkeypatch, _claim(), offers)

    assert session.state == mint_flow.OFFER_READY
    assert session.nft_number == 4000  # journaled, not freshly allocated
    assert session.nft_id == "NFTID1"
    assert session.traits == {"Body": "Alien", "Hat": "Crown"}
    assert session.body_type == "Alien"
    assert session.image_url == _METADATA["image"]
    assert session.video_url == _METADATA["video"]
    assert session.accept_uuid == "u"
    assert offers == [("OFFER1", None)]
    # The real resume body ran: the journaled blob was submitted verbatim.
    assert _resume_mocks["submit_kwargs"]["signed_tx_blob"] == "BLOB:" + "A" * 64
    assert _resume_mocks["record_kwargs"]["nft_number"] == 4000


@pytest.mark.parametrize(
    "hole",
    [
        "mint_signed_tx_blob",
        "mint_signed_ledger_floor",
        "mint_nft_number",
        "mint_metadata_url",
        "mint_metadata_json",
        "mint_body_type",
    ],
)
def test_run_mint_session_incomplete_journal_fails_closed(_resume_mocks, monkeypatch, hole):
    """A partially-populated journal (tx hash present, `hole` missing) must
    raise the recovery error — never silently compose and mint fresh."""

    async def submit_forbidden(**kwargs):
        raise AssertionError("incomplete journal must never be submitted")

    monkeypatch.setattr(mint_flow.xrpl_ops, "submit_sponsored_mint", submit_forbidden)

    session = _run_session(monkeypatch, _claim(**{hole: None}))

    assert session.state == mint_flow.FAILED
    assert session.error == "sponsored mint recovery required: prepared journal is incomplete"
    assert session.nft_id is None
