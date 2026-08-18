# Listener startup auto catch-up (#402): on (re)subscribe the index listener
# self-heals a certified-but-gapped (bounded) eligibility archive by launching
# the bounded `backfill_history.py --catch-up-from-gap` in the background.
# These tests pin the trigger's preconditions (fail-closed: never-certified /
# unbounded / flag-off / missing distributor all skip), the single-flight +
# cooldown debounce, the crash isolation from the stream loop, and the
# cross-process certification flock shared with the #341 Start-time reverify.

import asyncio
import os
import sys

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("XUMM_API_KEY", "x")
os.environ.setdefault("XUMM_API_SECRET", "x")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "x")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "x")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("XRPL_NETWORK", "testnet")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import onchain_listener as oln  # noqa: E402

from lfg_core import archive_reverify, config, history_store  # noqa: E402

GENESIS = "A" * 64
NETWORK = "testnet"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _history_conn(tmp_path):
    return history_store.init_history_db(str(tmp_path / "history_testnet.db"))


def _certify(conn, *, tip=1_000_000):
    history_store.record_archive_baseline(
        conn,
        network=NETWORK,
        genesis_hash=GENESIS,
        ledger_min=history_store.EARLIEST_AVAILABLE_LEDGER,
        ledger_max=tip,
        provenance="human audit for tests",
        source_tag=config.SOURCE_TAG,
    )


def _stamp_gap(conn, *, gap_after=999_000):
    history_store.invalidate_archive_continuity(
        conn,
        network=NETWORK,
        reason="listener process restart lacks exact stream catch-up",
        gap_after=gap_after,
    )


class FakeRunner:
    def __init__(self, rc=0, exc=None, gate=None):
        self.rc = rc
        self.exc = exc
        self.gate = gate
        self.calls = []

    async def __call__(self, provenance, distributor):
        self.calls.append((provenance, distributor))
        if self.gate is not None:
            await self.gate.wait()
        if self.exc is not None:
            raise self.exc
        return self.rc, "fake output"


def _catchup(runner, cooldown=0.0):
    return oln.AutoCatchup(NETWORK, runner=runner, cooldown=cooldown)


def _enable(monkeypatch, distributor="rDistributorWalletForTests"):
    monkeypatch.delenv("LISTENER_AUTO_CATCHUP", raising=False)
    monkeypatch.setattr(config, "BRIX_DISTRIBUTOR_ADDRESS", distributor)


def test_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("LISTENER_AUTO_CATCHUP", raising=False)
    assert config.env_flag("LISTENER_AUTO_CATCHUP", config.LISTENER_AUTO_CATCHUP_DEFAULT)


def test_fires_on_certified_bounded_gap(tmp_path, monkeypatch):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    _certify(conn)
    _stamp_gap(conn)
    runner = FakeRunner(rc=0)
    catchup = _catchup(runner)

    async def go():
        assert catchup.maybe_start(conn) == "started"
        await catchup._task

    _run(go())
    assert len(runner.calls) == 1
    provenance, distributor = runner.calls[0]
    assert provenance.startswith("auto catch-up after listener restart @")
    assert distributor == "rDistributorWalletForTests"


def test_skips_when_never_certified(tmp_path, monkeypatch):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    runner = FakeRunner()
    assert _run(_async_maybe(_catchup(runner), conn)) == "never_certified"
    assert runner.calls == []


def test_skips_when_no_gap(tmp_path, monkeypatch):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    _certify(conn)
    runner = FakeRunner()
    assert _run(_async_maybe(_catchup(runner), conn)) == "no_gap"
    assert runner.calls == []


def test_skips_unbounded_gap(tmp_path, monkeypatch):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    _certify(conn)
    # invalidate_archive_continuity backfills a bound from the certified tip,
    # so an unbounded gap on a certified archive can only appear via legacy /
    # hand-edited rows — seed it directly to pin the defensive branch.
    conn.execute(
        "UPDATE archive_state SET baseline_complete=0, continuity_gap_at=1, "
        "continuity_gap_after=NULL, continuity_gap_reason='legacy' WHERE network=?",
        (NETWORK,),
    )
    conn.commit()
    runner = FakeRunner()
    assert _run(_async_maybe(_catchup(runner), conn)) == "unbounded_gap"
    assert runner.calls == []


def test_skips_when_flag_off(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("LISTENER_AUTO_CATCHUP", "0")
    conn = _history_conn(tmp_path)
    _certify(conn)
    _stamp_gap(conn)
    runner = FakeRunner()
    assert _run(_async_maybe(_catchup(runner), conn)) == "disabled"
    assert runner.calls == []


def test_skips_when_distributor_missing(tmp_path, monkeypatch, caplog):
    _enable(monkeypatch, distributor=None)
    conn = _history_conn(tmp_path)
    _certify(conn)
    _stamp_gap(conn)
    runner = FakeRunner()
    with caplog.at_level("WARNING"):
        assert _run(_async_maybe(_catchup(runner), conn)) == "no_distributor"
    assert runner.calls == []
    assert "BRIX_DISTRIBUTOR_ADDRESS" in caplog.text


def test_single_flight_second_trigger_noop(tmp_path, monkeypatch):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    _certify(conn)
    _stamp_gap(conn)

    async def go():
        gate = asyncio.Event()
        runner = FakeRunner(rc=0, gate=gate)
        catchup = _catchup(runner)
        assert catchup.maybe_start(conn) == "started"
        assert catchup.maybe_start(conn) == "already_running"
        gate.set()
        await catchup._task
        assert len(runner.calls) == 1

    _run(go())


def test_cooldown_debounces_flapping_stream(tmp_path, monkeypatch):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    _certify(conn)
    _stamp_gap(conn)
    runner = FakeRunner(rc=1)  # failed attempt: gap stays, retry is allowed later
    catchup = oln.AutoCatchup(NETWORK, runner=runner, cooldown=3600.0)

    async def go():
        assert catchup.maybe_start(conn) == "started"
        await catchup._task
        assert catchup.maybe_start(conn) == "cooldown"

    _run(go())
    assert len(runner.calls) == 1


def test_retry_after_cooldown_expires(tmp_path, monkeypatch):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    _certify(conn)
    _stamp_gap(conn)
    runner = FakeRunner(rc=1)
    catchup = _catchup(runner, cooldown=0.0)

    async def go():
        assert catchup.maybe_start(conn) == "started"
        await catchup._task
        assert catchup.maybe_start(conn) == "started"
        await catchup._task

    _run(go())
    assert len(runner.calls) == 2


def test_catchup_crash_does_not_propagate(tmp_path, monkeypatch, caplog):
    _enable(monkeypatch)
    conn = _history_conn(tmp_path)
    _certify(conn)
    _stamp_gap(conn)
    runner = FakeRunner(exc=RuntimeError("boom"))
    catchup = _catchup(runner)

    async def go():
        assert catchup.maybe_start(conn) == "started"
        await catchup._task  # must not raise

    with caplog.at_level("ERROR"):
        _run(go())
    assert "auto catch-up crashed" in caplog.text


def test_trigger_error_is_swallowed(monkeypatch):
    _enable(monkeypatch)

    class BrokenConn:
        def execute(self, *a, **k):
            raise RuntimeError("db exploded")

    catchup = _catchup(FakeRunner())
    assert _run(_async_maybe(catchup, BrokenConn())) == "error"


def test_certification_lock_is_exclusive(tmp_path):
    db = str(tmp_path / "history_testnet.db")
    first = archive_reverify.acquire_certification_lock(db)
    assert first is not None
    assert archive_reverify.acquire_certification_lock(db) is None
    first.close()
    second = archive_reverify.acquire_certification_lock(db)
    assert second is not None
    second.close()


async def _async_maybe(catchup, conn):
    # maybe_start needs a running loop only to create the task; precondition
    # skips never reach create_task, but keep every call inside a loop anyway.
    return catchup.maybe_start(conn)
