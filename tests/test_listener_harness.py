# End-to-end harness for scripts/onchain_listener.py::_listen (#347).
#
# A scripted fake AsyncWebsocketClient drives the REAL listen loop — subscribe,
# endpoint-identity verification, stream consumption, the ArchiveBatch flush /
# continuity-invalidation teardown (#362), and the reconnect/backoff path
# (clean close, error, and the #345 StreamStalled watchdog all ride it) —
# without a live websocket. Piecewise units (in test_onchain_listener.py)
# already cover _verify_archive_connection / process_stream_tx / ArchiveBatch
# internals; this file asserts only the WIRING and its ordering, per network.

import argparse
import asyncio
import os
import sys

import pytest
from xrpl.models.requests import StreamParameter, Subscribe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import backfill_onchain as bf  # noqa: E402
import onchain_listener as oln  # noqa: E402

from lfg_core import config  # noqa: E402
from lfg_core import history_store as hs  # noqa: E402

L0 = hs.EARLIEST_AVAILABLE_LEDGER
GENESIS = {net: f"{net}-ledger-one" for net in bf.NETWORKS}
TIP = L0 + 50


class _HarnessDone(BaseException):
    """Raised by the fake connection factory to end _listen's infinite loop.

    A BaseException on purpose: it must escape the loop's `except Exception`
    reconnect handler and propagate out through `finally`."""


class FakeResponse:
    def __init__(self, result):
        self.result = result

    def is_successful(self):
        return True


class FakeClient:
    """Async-context-manager + async-iterator + request() triple, scripted.

    `script` is a list of stream messages followed by one terminal behavior:
    "close" (clean StopAsyncIteration), "error" (raise RuntimeError), or
    "hang" (await forever — the #345 watchdog must break it)."""

    def __init__(self, url, script, genesis_hash, log):
        self.url = url
        self.script = list(script)
        self.genesis_hash = genesis_hash
        self.log = log  # shared event log across all connections

    async def __aenter__(self):
        if self.script == ["refuse"]:
            self.log.append(("refused",))
            raise RuntimeError("scripted connection refusal")
        self.log.append(("enter", self.url))
        return self

    async def __aexit__(self, *exc):
        self.log.append(("exit",))
        return False

    async def request(self, req):
        if isinstance(req, Subscribe):
            self.log.append(("subscribe", tuple(req.streams or ())))
            return FakeResponse({"status": "success"})
        ledger_index = getattr(req, "ledger_index", None)
        self.log.append(("ledger", ledger_index))
        if ledger_index == L0:
            return FakeResponse({"ledger_hash": self.genesis_hash, "ledger_index": L0})
        return FakeResponse({"ledger_hash": "tip-hash", "ledger_index": TIP})

    def __aiter__(self):
        return self

    async def __anext__(self):
        step = self.script.pop(0) if self.script else "close"
        if step == "close":
            self.log.append(("closed",))
            raise StopAsyncIteration
        if step == "error":
            self.log.append(("errored",))
            raise RuntimeError("scripted stream error")
        if step == "hang":
            self.log.append(("hung",))
            await asyncio.Event().wait()
        self.log.append(("message", step.get("hash")))
        return step


class Harness:
    """Wire _listen to scripted fake connections and tmp per-network DBs."""

    def __init__(self, tmp_path, monkeypatch, network, scripts):
        self.network = network
        self.scripts = list(scripts)
        self.log = []
        self.sleeps = []
        self.connections = []
        monkeypatch.setenv("ONCHAIN_DB_PATH", str(tmp_path / f"onchain_{network}.db"))
        monkeypatch.setenv("HISTORY_DB_PATH", str(tmp_path / f"history_{network}.db"))
        monkeypatch.setattr(oln.db_path, "app_db_path", lambda _n: str(tmp_path / "app.db"))
        monkeypatch.setattr(config, "SPONSORED_MINT_ARCHIVE_GENESIS_HASHES", {})
        monkeypatch.setattr(oln, "AsyncWebsocketClient", self._connect)
        # Stub the idle flusher (its own flush behavior has #362 unit tests):
        # the harness asserts only that _listen starts it per connection and
        # cancels it on teardown. Stubbing also keeps its periodic sleeps out
        # of the backoff recorder below.
        monkeypatch.setattr(oln, "_archive_idle_flush_loop", self._fake_flusher)
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda d: self.sleeps.append(d) or real_sleep(0))

    async def _fake_flusher(self, _batch, interval=None):
        self.log.append(("flusher-start",))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.log.append(("flusher-cancelled",))
            raise

    def _connect(self, url):
        if not self.scripts:
            raise _HarnessDone
        client = FakeClient(url, self.scripts.pop(0), GENESIS[self.network], self.log)
        self.connections.append(client)
        return client

    def history_conn(self):
        return hs.init_history_db(os.environ["HISTORY_DB_PATH"])

    def certify(self):
        """Pre-record a certified baseline whose max matches the fake tip."""
        conn = self.history_conn()
        hs.record_archive_baseline(
            conn,
            network=self.network,
            genesis_hash=GENESIS[self.network],
            ledger_min=L0,
            ledger_max=TIP,
            provenance="harness",
            source_tag=config.SOURCE_TAG,
            completed_at=100,
        )
        conn.close()

    def run(self, issuer, taxon, clio):
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(_HarnessDone):
                loop.run_until_complete(oln._listen(self.network, issuer, taxon, clio))
        finally:
            loop.close()


def _tagged_payment(ledger_index):
    return {
        "type": "transaction",
        "validated": True,
        "hash": f"{ledger_index:064X}"[-64:],
        "ledger_index": ledger_index,
        "tx_json": {
            "TransactionType": "Payment",
            "Account": "rHarnessWallet",
            "SourceTag": config.SOURCE_TAG,
            "date": 800_000_000,
        },
        "meta": {"TransactionResult": "tesSUCCESS"},
    }


def _foreign_mint(ledger_index):
    return {
        "type": "transaction",
        "validated": True,
        "hash": f"{ledger_index:064X}"[-64:],
        "ledger_index": ledger_index,
        "tx_json": {
            "TransactionType": "NFTokenMint",
            "Account": "rForeignMinter",
            "Issuer": "rSomeForeignIssuer",
            "SourceTag": config.SOURCE_TAG,
            "date": 800_000_001,
        },
        "meta": {"TransactionResult": "tesSUCCESS"},
    }


@pytest.mark.parametrize("network", sorted(bf.NETWORKS))
def test_resolve_defaults_per_network(network):
    args = argparse.Namespace(network=network, issuer=None, taxon=None, clio=None)
    net, issuer, taxon, clio = oln._resolve(args)
    expected = bf.NETWORKS[network]
    assert net == network
    assert issuer == (expected["issuer"] or config.SWAP_ISSUER_ADDRESS)
    assert taxon == expected["taxon"]
    assert clio == expected["clio"]


@pytest.mark.parametrize("network", sorted(bf.NETWORKS))
def test_listen_subscribes_verifies_identity_then_processes_and_invalidates_on_disconnect(
    tmp_path, monkeypatch, network
):
    tagged = _tagged_payment(TIP + 1)
    foreign = _foreign_mint(TIP + 2)
    h = Harness(
        tmp_path,
        monkeypatch,
        network,
        scripts=[
            [{"type": "ledgerClosed"}, tagged, foreign, "close"],  # conn 1
            ["close"],  # conn 2: proves identity is re-verified after reconnect
        ],
    )
    h.certify()
    _net, issuer, taxon, clio = oln._resolve(
        argparse.Namespace(network=network, issuer=None, taxon=None, clio=None)
    )
    h.run(issuer, taxon, clio)

    # The network's clio URL was dialed on every connection.
    assert [c.url for c in h.connections] == [bf.NETWORKS[network]["clio"], clio]

    # Per connection: Subscribe(transactions) first, then BOTH identity ledger
    # requests, before any stream message is consumed.
    kinds = [e[0] for e in h.log]
    first_conn = kinds[: kinds.index("closed")]
    assert first_conn[:4] == ["enter", "subscribe", "ledger", "ledger"]
    assert h.log[1] == ("subscribe", (StreamParameter.TRANSACTIONS,))
    assert h.log[2] == ("ledger", L0)
    assert h.log[3] == ("ledger", "validated")
    assert first_conn.count("message") == 3  # ledgerClosed msg + 2 txs

    # Teardown ordering (#362 wiring): the idle flusher is started once per
    # connection and cancelled-and-awaited BEFORE the next connection dials.
    assert kinds.count("flusher-start") == 2
    assert kinds.count("flusher-cancelled") == 2
    # Reconnect re-runs the full subscribe + identity sequence.
    second_conn = kinds[kinds.index("closed") + 1 :]
    assert second_conn[:6] == [
        "exit",
        "flusher-cancelled",
        "enter",
        "subscribe",
        "ledger",
        "ledger",
    ]
    # ...and the second identity snapshot is COMPLETE: the genesis (ledger 1)
    # AND validated-ledger requests both re-run after reconnect.
    ledger_reqs = [e for e in h.log if e[0] == "ledger"]
    assert ledger_reqs[-2:] == [("ledger", L0), ("ledger", "validated")]
    # Clean close backed off at RECONNECT_BASE before reconnecting.
    assert h.sleeps[0] == oln.RECONNECT_BASE

    # Both tagged txs (plain Payment AND the foreign-issuer NFTokenMint fast
    # path) were archived via the ArchiveBatch and flushed on teardown.
    conn = h.history_conn()
    rows = {
        r["tx_hash"]: r["source_tag"]
        for r in conn.execute("SELECT tx_hash, source_tag FROM xrpl_txs")
    }
    assert rows == {tagged["hash"]: config.SOURCE_TAG, foreign["hash"]: config.SOURCE_TAG}

    # Teardown ordering (#362): the pending batch flushed BEFORE the
    # continuity invalidation — the recorded gap bound is the batch's cursor
    # (highest streamed ledger), not the pre-stream baseline max.
    state = hs.get_archive_state(conn, network)
    conn.close()
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_reason == "transaction stream disconnected"
    assert state.continuity_gap_after == TIP + 2
    assert state.validated_ledger_index == TIP + 2


@pytest.mark.parametrize("network", sorted(bf.NETWORKS))
def test_error_and_clean_close_both_back_off_and_reverify_identity(tmp_path, monkeypatch, network):
    # A stream error, six refused reconnect attempts (failing before the
    # subscribe succeeds, so backoff never resets), then a successful
    # connection that closes cleanly: backoff must double RECONNECT_BASE →
    # RECONNECT_MAX across the failures, and a successful subscribe+verify
    # resets it, so the clean close backs off at RECONNECT_BASE again.
    h = Harness(
        tmp_path,
        monkeypatch,
        network,
        scripts=[["error"]] + [["refuse"]] * 6 + [["close"]],
    )
    clio = bf.NETWORKS[network]["clio"]
    h.run("rrrrrrrrrrrrrrrrrrrrrhoLvTp", 0, clio)

    assert len(h.connections) == 8
    expected, backoff = [], oln.RECONNECT_BASE
    for _ in range(7):
        expected.append(backoff)
        backoff = min(backoff * 2, oln.RECONNECT_MAX)
    assert h.sleeps == expected + [oln.RECONNECT_BASE]
    assert h.sleeps[-2] == oln.RECONNECT_MAX  # the cap was reached and held
    # Identity was re-verified on every connection that actually subscribed
    # (uncertified here, but the snapshot fetch itself always runs): the first
    # (errored mid-stream) and the last (clean close), never the refusals.
    assert [e for e in h.log if e[0] == "ledger"] == [("ledger", L0), ("ledger", "validated")] * 2


@pytest.mark.parametrize("network", sorted(bf.NETWORKS))
def test_watchdog_stall_rides_the_reconnect_path(tmp_path, monkeypatch, network):
    monkeypatch.setattr(oln, "STREAM_IDLE_TIMEOUT", 0.01)
    h = Harness(tmp_path, monkeypatch, network, scripts=[["hang"], ["close"]])
    h.certify()
    h.run("rrrrrrrrrrrrrrrrrrrrrhoLvTp", 0, bf.NETWORKS[network]["clio"])

    assert ("hung",) in h.log
    assert len(h.connections) == 2  # the stall forced a reconnect
    assert h.sleeps[0] == oln.RECONNECT_BASE
    # The stall is a disconnect: continuity invalidated fail-closed.
    conn = h.history_conn()
    state = hs.get_archive_state(conn, network)
    conn.close()
    assert state is not None
    assert state.baseline_complete is False
    assert state.continuity_gap_reason == "transaction stream disconnected"


def test_uncertified_archive_streams_without_writing_archive_state(tmp_path, monkeypatch):
    # No certified baseline: _listen must still subscribe and consume the
    # stream (index/market/history duties), but never fabricate archive state.
    tagged = _tagged_payment(TIP + 1)
    h = Harness(tmp_path, monkeypatch, "testnet", scripts=[[tagged, "close"]])
    h.run("rrrrrrrrrrrrrrrrrrrrrhoLvTp", 0, bf.NETWORKS["testnet"]["clio"])

    conn = h.history_conn()
    row = conn.execute(
        "SELECT source_tag FROM xrpl_txs WHERE tx_hash=?", (tagged["hash"],)
    ).fetchone()
    state = hs.get_archive_state(conn, "testnet")
    conn.close()
    assert row is not None and row["source_tag"] == config.SOURCE_TAG
    assert state is None
