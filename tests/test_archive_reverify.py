# Tests for lfg_core/archive_reverify.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio
import json

import pytest

from lfg_core import archive_reverify, history_store

GENESIS = "ABC123GENESISHASH"


@pytest.fixture(scope="module", autouse=True)
def _restore_event_loop():
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _isolated_onchain_index(tmp_path, monkeypatch):
    # The `nfts` sweep reads the on-chain index; never let a test touch the
    # repo's real onchain_<net>.db.
    monkeypatch.setenv("ONCHAIN_DB_PATH", str(tmp_path / "onchain_testnet.db"))


# The full attested account set (#331): every certified source that carries an
# address. `nfts` is attested in `sources` but has no account.
COVERED_ACCOUNTS = {
    "issuer": "rrrrrrrrrrrrrrrrrrrrrhoLvTp",
    "brix": "rBRIX",
    "token_issuer": "rTOKEN",
    "signing": "rSIGN",
    "distributor": "rDIST",
}


def _fake_request_fn(tip=500_000, genesis=GENESIS, account_pages=None, fail_account_tx=False):
    async def request_fn(req):
        if req["method"] == "ledger":
            if req["ledger_index"] == history_store.EARLIEST_AVAILABLE_LEDGER:
                return {
                    "ledger_index": history_store.EARLIEST_AVAILABLE_LEDGER,
                    "ledger_hash": genesis,
                }
            return {"ledger_index": tip, "ledger_hash": "TIPHASH"}
        if req["method"] == "account_tx":
            if fail_account_tx:
                raise RuntimeError("account_tx failed: boom")
            return {
                "account": req["account"],
                "ledger_index_min": req["ledger_index_min"],
                "ledger_index_max": req["ledger_index_max"],
                "validated": True,
                "transactions": (account_pages or {}).get(req["account"], []),
            }
        if req["method"] == "nft_history":
            return {
                "nft_id": req["nft_id"],
                "ledger_index_min": req["ledger_index_min"],
                "ledger_index_max": req["ledger_index_max"],
                "validated": True,
                "transactions": [],
            }
        raise AssertionError(f"unexpected method {req['method']}")

    return request_fn


def _certified_conn(tmp_path, *, provenance="hamsa manual audit 2026-08-01", coverage=True):
    conn = history_store.init_history_db(str(tmp_path / "history_testnet.db"))
    doc = archive_reverify.baseline_coverage_document(
        COVERED_ACCOUNTS,
        sources=archive_reverify.REQUIRED_BASELINE_SOURCES,
        source_tag=2606160021,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=400_000,
    )
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash=GENESIS,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=400_000,
        provenance=provenance,
        source_tag=2606160021,
        coverage=json.dumps(doc, sort_keys=True, separators=(",", ":")) if coverage else None,
    )
    return conn


def test_inherit_attestation_passthrough_and_unnesting():
    assert archive_reverify.inherit_attestation("hamsa audit") == "hamsa audit"
    wrapped = "auto-reverify at 2026-08-03T14:00:00Z (baseline: hamsa audit)"
    assert archive_reverify.inherit_attestation(wrapped) == "hamsa audit"
    # double-wrap never nests
    rewrapped = f"auto-reverify at 2026-08-04T00:00:00Z (baseline: {archive_reverify.inherit_attestation(wrapped)})"
    assert archive_reverify.inherit_attestation(rewrapped) == "hamsa audit"


def test_reverify_refuses_without_prior_baseline(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "history_testnet.db"))
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(), network="testnet")
    )
    assert result == archive_reverify.ReverifyResult(False, "baseline_never_certified", None, None)


def test_reverify_refuses_on_genesis_mismatch(tmp_path):
    conn = _certified_conn(tmp_path)
    result = asyncio.run(
        archive_reverify.reverify_archive(
            conn, _fake_request_fn(genesis="OTHERCHAIN"), network="testnet"
        )
    )
    assert (result.ok, result.reason) == (False, "genesis_mismatch")


def test_reverify_refuses_on_source_tag_change(tmp_path, monkeypatch):
    from lfg_core import config

    monkeypatch.setattr(config, "SOURCE_TAG", config.SOURCE_TAG + 1)
    conn = _certified_conn(tmp_path)
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(), network="testnet")
    )
    assert (result.ok, result.reason) == (False, "source_tag_changed")


def test_reverify_refuses_on_unbound_coverage(tmp_path):
    conn = _certified_conn(tmp_path, coverage=False)
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(), network="testnet")
    )
    assert (result.ok, result.reason) == (False, "coverage_unbound")


def test_reverify_certifies_to_tip_and_inherits_attestation(tmp_path):
    conn = _certified_conn(tmp_path, provenance="hamsa manual audit 2026-08-01")
    result = asyncio.run(
        archive_reverify.reverify_archive(
            conn, _fake_request_fn(tip=600_000), network="testnet", now=1_800_000_000
        )
    )
    assert result.ok and result.reason is None
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and state.baseline_complete
    assert state.baseline_ledger_max == 600_000 == result.ledger_max
    assert state.baseline_provenance is not None
    assert "(baseline: hamsa manual audit 2026-08-01)" in state.baseline_provenance
    assert state.baseline_provenance.startswith("auto-reverify at ")
    # coverage doc rebuilt against the new range, same accounts
    doc = json.loads(state.baseline_coverage or "{}")
    assert doc["ledger_max"] == 600_000
    assert doc["accounts"] == dict(sorted(COVERED_ACCOUNTS.items()))
    assert doc["sources"] == sorted(archive_reverify.REQUIRED_BASELINE_SOURCES)
    # certification clears the heartbeat by design
    assert state.heartbeat_at is None and state.validated_ledger_index is None


def test_second_reverify_does_not_nest_provenance(tmp_path):
    conn = _certified_conn(tmp_path, provenance="hamsa manual audit 2026-08-01")
    asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(tip=600_000), network="testnet")
    )
    asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(tip=700_000), network="testnet")
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and state.baseline_provenance is not None
    assert state.baseline_provenance.count("auto-reverify at") == 1
    assert state.baseline_provenance.endswith("(baseline: hamsa manual audit 2026-08-01)")


def test_reverify_heals_bounded_continuity_gap(tmp_path):
    conn = _certified_conn(tmp_path)
    history_store.invalidate_archive_continuity(
        conn, network="testnet", gap_after=450_000, reason="listener disconnect"
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and not state.baseline_complete
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(tip=600_000), network="testnet")
    )
    assert result.ok
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and state.baseline_complete
    assert state.continuity_gap_at is None


def test_reverify_reports_sweep_failure(tmp_path):
    conn = _certified_conn(tmp_path)
    result = asyncio.run(
        archive_reverify.reverify_archive(
            conn, _fake_request_fn(fail_account_tx=True), network="testnet"
        )
    )
    assert not result.ok
    assert result.reason is not None and result.reason.startswith("sweep_failed: ")


def test_reverify_refuses_when_gap_bound_lies_past_tip(tmp_path):
    conn = _certified_conn(tmp_path)
    history_store.invalidate_archive_continuity(
        conn, network="testnet", gap_after=700_000, reason="listener disconnect"
    )
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and not state.baseline_complete
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(tip=600_000), network="testnet")
    )
    assert (result.ok, result.reason) == (False, "gap_not_covered")
    state = history_store.get_archive_state(conn, "testnet")
    assert state is not None and not state.baseline_complete


def test_reverify_refuses_when_coverage_lacks_required_source(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "history_testnet.db"))
    doc = archive_reverify.baseline_coverage_document(
        {"issuer": "rISS"},
        sources={"issuer"},
        source_tag=2606160021,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=400_000,
    )
    history_store.record_archive_baseline(
        conn,
        network="testnet",
        genesis_hash=GENESIS,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=400_000,
        provenance="hamsa manual audit 2026-08-01",
        source_tag=2606160021,
        coverage=json.dumps(doc, sort_keys=True, separators=(",", ":")),
    )
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(), network="testnet")
    )
    assert (result.ok, result.reason) == (False, "missing_required_sources")


def test_reverify_refuses_listener_created_row_never_certified(tmp_path):
    conn = history_store.init_history_db(str(tmp_path / "history_testnet.db"))
    # A row created purely by the streaming listener's heartbeat, with no
    # baseline ever certified through record_archive_baseline, must never be
    # treated as a certified archive — otherwise a listener could "launder"
    # an uncertified archive into eligibility just by staying connected.
    history_store.record_validated_ledger(
        conn,
        network="testnet",
        genesis_hash=GENESIS,
        ledger_index=500_000,
        close_time=1_800_000_000,
        source_tag=2606160021,
    )
    result = asyncio.run(
        archive_reverify.reverify_archive(conn, _fake_request_fn(), network="testnet")
    )
    assert (result.ok, result.reason) == (False, "baseline_never_certified")


def test_wait_for_archive_usable_polls_until_true(monkeypatch, tmp_path):
    from lfg_core import sponsored_mint

    calls = {"n": 0}

    def fake_usable(path, *, network=None, now=None):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(sponsored_mint, "archive_is_usable", fake_usable)
    clock = {"t": 0.0}
    slept: list[float] = []

    async def fake_sleep(seconds):
        clock["t"] += seconds
        slept.append(seconds)

    ok = asyncio.run(
        archive_reverify.wait_for_archive_usable(
            str(tmp_path / "h.db"),
            network="testnet",
            timeout=90.0,
            poll=5.0,
            now_fn=lambda: clock["t"],
            sleep_fn=fake_sleep,
        )
    )
    assert ok and calls["n"] == 3 and slept == [5.0, 5.0]


def test_wait_for_archive_usable_times_out(monkeypatch, tmp_path):
    from lfg_core import sponsored_mint

    monkeypatch.setattr(sponsored_mint, "archive_is_usable", lambda *a, **k: False)
    clock = {"t": 0.0}

    async def fake_sleep(seconds):
        clock["t"] += seconds

    ok = asyncio.run(
        archive_reverify.wait_for_archive_usable(
            str(tmp_path / "h.db"),
            network="testnet",
            timeout=20.0,
            poll=5.0,
            now_fn=lambda: clock["t"],
            sleep_fn=fake_sleep,
        )
    )
    assert not ok
