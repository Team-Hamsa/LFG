from lfg_core import closet_market_store as cms
from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from tests.test_economy_flow_equip import _char, _conn_with_assets, _deps, _Fakes, _run
from tests.test_economy_flow_extract import _F
from tests.test_economy_flow_extract import _deps as _extract_deps


def _list(conn, owner, slot, value):
    es.set_closet_status(conn, owner, ct.ACTIVE)
    return cms.create_ask(conn, owner=owner, slot=slot, value=value, price_brix="5", platform=None)


def test_listed_error_counts_encumbrance(tmp_path):
    conn = _conn_with_assets([("Head", "Crown", 1)])
    _list(conn, "rUser", "Head", "Crown")
    assert "listed" in ef._listed_error(
        conn, "rUser", {("Head", "Crown"): 1}, {("Head", "Crown"): 1}
    )
    assert ef._listed_error(conn, "rUser", {("Head", "Crown"): 1}, {("Head", "Crown"): 2}) is None


def test_equip_refuses_the_only_listed_copy(tmp_path):
    conn, f = _conn_with_assets([("Head", "Crown", 1)]), _Fakes()
    _list(conn, "rUser", "Head", "Crown")
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.FAILED and "listed" in s.error
    assert f.char_modifies == []


def test_equip_allows_an_unlisted_second_copy(tmp_path):
    conn, f = _conn_with_assets([("Head", "Crown", 2)]), _Fakes()
    _list(conn, "rUser", "Head", "Crown")
    s = ef.EquipSession(owner="rUser", character=_char(), changes=[("Head", "Crown")])
    _run(ef.run_equip(s, _deps(conn, f, tmp_path)))
    assert s.state == ef.DONE


def test_extract_refuses_the_only_listed_copy(tmp_path):
    conn = _conn_with_assets([("Hat", "Cap", 1)])
    es.set_closet_token(conn, "rUser", "CLOSET", "AB", status=ct.ACTIVE, offer_id=None)
    _list(conn, "rUser", "Hat", "Cap")
    s = ef.ExtractSession(owner="rUser", slot="Hat", value="Cap")
    _run(ef.run_extract(s, _extract_deps(conn, _F(), tmp_path)))
    assert s.state == ef.FAILED and "listed" in s.error
