"""Activation-funder lookups read the transaction that CREATED the account,
from full history only (2026-09-21: a pruned validator's oldest retained
transaction, and a SetRegularKey naming the address before it existed, were
both being cached as the activation)."""

import importlib
import json
import sqlite3
import sys

import pytest

from lfg_core import config, funding

W = "rWallet1111111111111111111111111"
FUNDER = "rFunder1111111111111111111111111"
OTHER = "rOther11111111111111111111111111"


def created(account, *, signer=FUNDER, ledger=10, result="tesSUCCESS"):
    return {
        "ledger_index": ledger,
        "tx_json": {"TransactionType": "Payment", "Account": signer, "Destination": account},
        "meta": {
            "TransactionResult": result,
            "AffectedNodes": [
                {
                    "ModifiedNode": {
                        "LedgerEntryType": "AccountRoot",
                        "FinalFields": {"Account": signer},
                    }
                },
                {
                    "CreatedNode": {
                        "LedgerEntryType": "AccountRoot",
                        "NewFields": {"Account": account},
                    }
                },
            ],
        },
    }


def touching(*, signer=OTHER, ledger=5, tx_type="SetRegularKey"):
    """A transaction that lists the wallet in its account_tx without creating it."""
    return {
        "ledger_index": ledger,
        "tx_json": {"TransactionType": tx_type, "Account": signer, "RegularKey": W},
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [
                {
                    "ModifiedNode": {
                        "LedgerEntryType": "AccountRoot",
                        "FinalFields": {"Account": signer},
                    }
                }
            ],
        },
    }


def test_activation_is_the_creating_tx_not_the_oldest():
    assert funding.activation_in([touching(), created(W)], W) == (FUNDER, 10)


def test_a_payment_that_did_not_create_the_account_is_not_its_activation():
    # An incoming payment to an already-existing account (the pruned-node case).
    later = created(OTHER, signer=OTHER)
    later["tx_json"]["Destination"] = W
    assert funding.activation_in([later], W) is None


def test_a_failed_creating_payment_is_ignored():
    assert funding.activation_in([created(W, result="tecNO_DST_INSUF_XRP")], W) is None


def test_parse_page_without_the_creating_tx_fails_closed():
    with pytest.raises(funding.FunderLookupError):
        funding.parse_account_tx({"transactions": [touching()]}, W)


class FakeClient:
    """Stands in for xrpl.clients.WebsocketClient, serving account_tx pages."""

    pages: list[dict] = []
    requests: list = []

    def __init__(self, url):
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, req):
        FakeClient.requests.append(req)
        page = FakeClient.pages[len(FakeClient.requests) - 1]

        class Resp:
            result = page

            def is_successful(self):
                return "error" not in page

        return Resp()


@pytest.fixture
def fake_ws(monkeypatch):
    import xrpl.clients

    FakeClient.pages = []
    FakeClient.requests = []
    monkeypatch.setattr(xrpl.clients, "WebsocketClient", FakeClient)
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    return FakeClient


def test_lookup_pages_until_the_creating_tx(fake_ws):
    fake_ws.pages = [
        {"ledger_index_min": 32570, "transactions": [touching()], "marker": {"m": 1}},
        {"ledger_index_min": 32570, "transactions": [created(W, ledger=99)]},
    ]
    assert funding.lookup_funder(W, url="wss://clio.example") == (FUNDER, 99)
    assert fake_ws.requests[1].marker == {"m": 1}
    assert all(r.forward for r in fake_ws.requests)


def test_lookup_refuses_a_pruned_mainnet_node(fake_ws):
    fake_ws.pages = [{"ledger_index_min": 107094868, "transactions": [created(W)]}]
    with pytest.raises(funding.FunderLookupError, match="full history"):
        funding.lookup_funder(W, url="ws://validator.example")


def test_lookup_without_a_creating_tx_fails_closed(fake_ws):
    fake_ws.pages = [{"ledger_index_min": 32570, "transactions": [touching()]}]
    with pytest.raises(funding.FunderLookupError, match="no transaction creating"):
        funding.lookup_funder(W, url="wss://clio.example")


def test_lookup_act_not_found_is_unfunded(fake_ws):
    fake_ws.pages = [{"error": "actNotFound"}]
    assert funding.lookup_funder(W, url="wss://clio.example") == (None, None)


def test_lookup_fails_over_across_history_endpoints(monkeypatch):
    tried = []

    def on(endpoint, wallet):
        tried.append(endpoint)
        if endpoint == "wss://busy.example":
            raise funding.FunderLookupError("tooBusy")
        return (FUNDER, 3)

    monkeypatch.setattr(config, "HISTORY_WS_URLS", ("wss://busy.example", "wss://ok.example"))
    monkeypatch.setattr(funding, "_lookup_on", on)
    assert funding.lookup_funder(W) == (FUNDER, 3)
    assert tried == ["wss://busy.example", "wss://ok.example"]


def test_lookup_raises_when_every_endpoint_fails(monkeypatch):
    def on(endpoint, wallet):
        raise funding.FunderLookupError(f"down: {endpoint}")

    monkeypatch.setattr(config, "HISTORY_WS_URLS", ("wss://a.example", "wss://b.example"))
    monkeypatch.setattr(funding, "_lookup_on", on)
    with pytest.raises(funding.FunderLookupError, match="b.example"):
        funding.lookup_funder(W)


def test_act_not_found_on_one_endpoint_does_not_end_the_lookup(monkeypatch):
    answers = {"wss://lagging.example": (None, None), "wss://synced.example": (FUNDER, 9)}
    monkeypatch.setattr(config, "HISTORY_WS_URLS", tuple(answers))
    monkeypatch.setattr(funding, "_lookup_on", lambda endpoint, wallet: answers[endpoint])
    assert funding.lookup_funder(W) == (FUNDER, 9)


def test_unfunded_only_when_every_endpoint_agrees(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_WS_URLS", ("wss://a.example", "wss://b.example"))
    monkeypatch.setattr(funding, "_lookup_on", lambda endpoint, wallet: (None, None))
    assert funding.lookup_funder(W) == (None, None)


def test_act_not_found_plus_a_failure_fails_closed(monkeypatch):
    def on(endpoint, wallet):
        if endpoint == "wss://busy.example":
            raise funding.FunderLookupError("tooBusy")
        return (None, None)

    monkeypatch.setattr(config, "HISTORY_WS_URLS", ("wss://a.example", "wss://busy.example"))
    monkeypatch.setattr(funding, "_lookup_on", on)
    with pytest.raises(funding.FunderLookupError, match="tooBusy"):
        funding.lookup_funder(W)


def test_history_endpoints_start_with_clio_and_take_env_fallbacks():
    urls = config._ordered_urls("wss://clio.example", "wss://full.example", ())
    assert urls == ("wss://clio.example", "wss://full.example")
    assert config.HISTORY_WS_URLS[0] == config.CLIO_WS_URL


# --- backfill script --------------------------------------------------------


def _script(monkeypatch):
    script = importlib.import_module("scripts.backfill_wallet_funders")
    monkeypatch.setattr(script.config, "XRPL_NETWORK", "mainnet")
    return script


def _seed(db, rows):
    with sqlite3.connect(db) as conn:
        funding.ensure_schema(conn)
        conn.executemany(
            "INSERT INTO wallet_funders (wallet, funder, ledger_index) VALUES (?, ?, ?)", rows
        )


def test_reverify_dry_run_reports_without_writing(tmp_path, monkeypatch):
    db = str(tmp_path / "app.db")
    report = tmp_path / "r.json"
    _seed(db, [("rA", None, 107100000), ("rB", FUNDER, 10)])
    truth = {"rA": (OTHER, 50), "rB": (FUNDER, 10)}
    script = _script(monkeypatch)
    monkeypatch.setattr(script.funding, "lookup_funder", lambda w: truth[w])
    monkeypatch.setattr(
        sys,
        "argv",
        ["b", "--network", "mainnet", "--app-db", db, "--reverify", "--report", str(report)],
    )
    assert script.main() == 0
    body = json.loads(report.read_text())
    assert body["applied"] is False
    assert [c["wallet"] for c in body["changes"]] == ["rA"]
    assert body["changes"][0]["old"] == {"funder": None, "ledger_index": 107100000}
    with sqlite3.connect(db) as conn:
        assert funding.cached_funder(conn, "rA") is None


def test_reverify_apply_rewrites_deletes_and_skips_failures(tmp_path, monkeypatch):
    db = str(tmp_path / "app.db")
    report = tmp_path / "r.json"
    _seed(db, [("rA", "rWrong", 107100000), ("rGone", None, None), ("rFail", "rKeep", 1)])

    def lookup(w):
        if w == "rFail":
            raise funding.FunderLookupError("tooBusy")
        return {"rA": (FUNDER, 50), "rGone": (None, None)}[w]

    script = _script(monkeypatch)
    monkeypatch.setattr(script.funding, "lookup_funder", lookup)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "b",
            "--network",
            "mainnet",
            "--app-db",
            db,
            "--reverify",
            "--apply",
            "--report",
            str(report),
        ],
    )
    assert script.main() == 1  # a failed lookup is reported
    with sqlite3.connect(db) as conn:
        rows = {
            w: (f, lg)
            for w, f, lg in conn.execute("SELECT wallet, funder, ledger_index FROM wallet_funders")
        }
    assert rows == {"rA": (FUNDER, 50), "rFail": ("rKeep", 1)}
    body = json.loads(report.read_text())
    assert body["failed"][0]["wallet"] == "rFail"
    assert body["apply_requested"] is True
    assert body["applied"] is True


def test_reverify_report_is_not_marked_applied_if_the_write_fails(tmp_path, monkeypatch):
    db = str(tmp_path / "app.db")
    report = tmp_path / "r.json"
    _seed(db, [("rA", "rWrong", 1)])
    script = _script(monkeypatch)
    monkeypatch.setattr(script.funding, "lookup_funder", lambda w: (FUNDER, 50))
    real_connect = sqlite3.connect

    class Boom:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self._conn.__enter__()

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, *args)

    monkeypatch.setattr(script.sqlite3, "connect", lambda p: Boom(real_connect(p)))
    with pytest.raises(sqlite3.OperationalError):
        script.reverify(db, "mainnet", True, str(report))
    body = json.loads(report.read_text())
    assert body["apply_requested"] is True
    assert body["applied"] is False
    assert body["changes"][0]["wallet"] == "rA"


def test_apply_requires_reverify(tmp_path, monkeypatch):
    script = _script(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["b", "--network", "mainnet", "--app-db", str(tmp_path / "a.db"), "--apply"]
    )
    assert script.main() == 2


def test_backfill_does_not_cache_a_wallet_that_does_not_exist(tmp_path, monkeypatch):
    from lfg_core import history_store

    db = str(tmp_path / "app.db")
    history = str(tmp_path / "history.db")
    conn = history_store.init_history_db(history)
    conn.execute(
        "INSERT INTO xrpl_txs (tx_hash, ledger_index, close_time, tx_type, account, source_tag,"
        " raw_json) VALUES ('h1', 1, 1, 'Payment', 'rNew', ?, '{}')",
        (config.SOURCE_TAG,),
    )
    conn.commit()
    conn.close()
    script = _script(monkeypatch)
    monkeypatch.setattr(script.funding, "lookup_funder", lambda w: (None, None))
    monkeypatch.setattr(
        sys, "argv", ["b", "--network", "mainnet", "--app-db", db, "--from-history", history]
    )
    assert script.main() == 0
    with sqlite3.connect(db) as conn:
        assert not funding.has_cached_funder(conn, "rNew")


def test_brix_issuer_is_excluded_from_unique_users():
    from scripts import sourcetag_metrics as stm

    assert "rLfgoBriX5ZaMP32mtc7RUZJcjnisKh2Px" in stm.excluded_wallets()


def test_report_rewrite_failure_keeps_the_previous_report(tmp_path, monkeypatch):
    script = _script(monkeypatch)
    path = str(tmp_path / "r.json")
    script._write_report(path, {"applied": False})

    def boom(*a, **k):
        raise TypeError("not serializable")

    monkeypatch.setattr(script.json, "dump", boom)
    with pytest.raises(TypeError):
        script._write_report(path, {"applied": True})
    assert json.loads(open(path).read()) == {"applied": False}
