"""scripts/house_closet.py: argument rules and a dry run that writes nothing."""

import asyncio
import importlib
import json

import pytest

from lfg_core import config

script = importlib.import_module("scripts.house_closet")


@pytest.fixture(scope="module", autouse=True)
def _restore_event_loop():
    # main() uses asyncio.run(), which ends with set_event_loop(None) and would
    # break the asyncio.get_event_loop() idiom in test files that sort after
    # this one (test_market_api etc.). Same fix as test_archive_reverify.py.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def test_apply_needs_migrate():
    with pytest.raises(SystemExit):
        script.main(["--network", config.XRPL_NETWORK, "--apply"])


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from tests.test_house_closet_migration import APP, _db, _stock

    conn = _db(tmp_path)
    _stock(conn)
    monkeypatch.setattr(config, "SIGNING_ACCOUNT", APP)
    monkeypatch.setattr(config, "CLOSET_HOUSE_WALLET", "")  # unset is fine for a dry run
    monkeypatch.setattr(script.deps, "open_index", lambda network: conn)
    plan_path = tmp_path / "reports" / "plan.json"
    monkeypatch.setattr(script, "plan_path", lambda network: str(plan_path))

    assert script.main(["--network", config.XRPL_NETWORK, "--migrate"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["plan"]["list"] == 1 and out["plan"]["hold"] == 1
    assert out["plan"]["list_brix_total"] == "12"
    assert not plan_path.exists()
