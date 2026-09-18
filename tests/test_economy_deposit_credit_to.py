"""Deposit can credit a Closet other than the token holder's (#548: the app
wallet's stock moves into the house Closet)."""

import json
import sqlite3

from lfg_core import closet_token as ct
from lfg_core import economy_flow as ef
from lfg_core import economy_store as es
from tests.test_economy_flow_deposit import _F, _deps, _run

APP, HOUSE = "rApp", "rHouse"


class _HouseF(_F):
    async def closet_owner(self, nft_id: str) -> str | None:  # type: ignore[override]
        return {"C-house": HOUSE, "C-app": APP}.get(nft_id)


def _setup(conn):
    es.init_economy_schema(conn)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    es.set_closet_contents(conn, HOUSE, [], [])
    es.upsert_trait_token(conn, "TRAIT9", APP, "Hat", "Cap")


def test_deposit_credits_another_wallets_closet(tmp_path):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.DONE
    assert f.burns == [("TRAIT9", APP)]  # the holder's token is burned
    assert {(o, s, v): n for o, s, v, n in es.read_closet_assets(conn)} == {
        (HOUSE, "Hat", "Cap"): 1
    }
    assert es.read_trait_tokens(conn) == []


def test_the_credited_closet_must_be_active(tmp_path):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    es.upsert_trait_token(conn, "TRAIT9", APP, "Hat", "Cap")
    # The holder has a Closet and the credit target doesn't: the target decides.
    es.set_closet_token(conn, APP, "C-app", "AB", status=ct.ACTIVE, offer_id=None)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []


def test_the_holder_is_still_checked_on_ledger(tmp_path):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = "rSomeoneElse"
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert session.state == ef.FAILED
    assert f.burns == []


def test_the_journal_names_the_credited_closet(tmp_path):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    record = json.loads((tmp_path / f"deposit-{session.id}.json").read_text())
    assert record["owner"] == APP and record["credit_to"] == HOUSE


def test_the_lock_is_the_credited_closet(tmp_path, monkeypatch):
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    f = _HouseF()
    f.owner_for["TRAIT9"] = APP
    locked: list[str] = []
    real = ef.owner_lock.owner_lock

    def spy(owner):
        locked.append(owner)
        return real(owner)

    monkeypatch.setattr(ef.owner_lock, "owner_lock", spy)
    session = ef.DepositSession(owner=APP, nft_id="TRAIT9", credit_to=HOUSE)
    _run(ef.run_deposit(session, _deps(conn, f, tmp_path)))

    assert locked[0] == HOUSE
