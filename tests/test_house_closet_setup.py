"""House setup (#548): BRIX trust line + Closet claim, signed by the house."""

import sqlite3

import pytest
from xrpl.models.transactions import NFTokenAcceptOffer, TrustSet
from xrpl.wallet import Wallet

from lfg_core import closet_token as ct
from lfg_core import config, system_wallets
from lfg_core import economy_store as es
from lfg_core import house_closet as hc
from tests.test_economy_flow_deposit import _F, _deps, _run

HOUSE_WALLET = Wallet.create()
HOUSE = HOUSE_WALLET.classic_address


@pytest.fixture(autouse=True)
def _house_is_durably_excluded(monkeypatch):
    monkeypatch.setattr(system_wallets, "DURABLE_SYSTEM_ACCOUNTS", frozenset({HOUSE}))


class _Chain(_F):
    def __init__(self, *, funded=True, line=None, accept_result="tesSUCCESS"):
        super().__init__()
        self.funded, self.line, self.accept_result = funded, line, accept_result
        self.submitted: list = []
        self.accepted = False

    async def account(self, address):
        return {"Account": address} if self.funded else None

    async def brix_line(self, address):
        return self.line

    async def submit(self, tx, wallet):
        assert wallet is HOUSE_WALLET
        self.submitted.append(tx)
        if isinstance(tx, NFTokenAcceptOffer):
            self.accepted = self.accept_result == "tesSUCCESS"
            return self.accept_result
        return "tesSUCCESS"

    async def closet_owner(self, nft_id):  # type: ignore[override]
        return HOUSE if self.accepted else config.SWAP_ISSUER_ADDRESS


def _setup_deps(tmp_path, chain):
    conn = sqlite3.connect(":memory:")
    es.init_economy_schema(conn)
    return conn, hc.SetupDeps(
        economy=_deps(conn, chain, tmp_path),
        account_fn=chain.account,
        brix_line_fn=chain.brix_line,
        submit_fn=chain.submit,
    )


def _tagged(tx):
    return tx.source_tag == config.SOURCE_TAG and tx.memos


def test_setup_sets_the_trust_line_and_claims_the_closet(tmp_path):
    chain = _Chain()
    conn, deps = _setup_deps(tmp_path, chain)
    steps = _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))

    trust, accept = chain.submitted
    assert isinstance(trust, TrustSet) and trust.account == HOUSE and _tagged(trust)
    assert trust.limit_amount.issuer == config.BRIX_ISSUER
    assert trust.limit_amount.value == "1000000000"
    assert isinstance(accept, NFTokenAcceptOffer) and accept.account == HOUSE and _tagged(accept)
    assert accept.nftoken_sell_offer == "OFFER"
    assert es.get_closet_record(conn, HOUSE)[2] == ct.ACTIVE
    assert steps == {"trust_line": "set", "closet": ct.ACTIVE}


def test_setup_is_idempotent(tmp_path):
    chain = _Chain(line={"limit": "1000000000"})
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    assert _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000")) == {
        "trust_line": "present",
        "closet": ct.ACTIVE,
    }
    assert chain.submitted == []


def test_setup_raises_a_low_limit(tmp_path):
    chain = _Chain(line={"limit": "10"})
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.ACTIVE, offer_id=None)
    assert _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))["trust_line"] == "set"


def test_setup_refuses_an_unfunded_wallet(tmp_path):
    chain = _Chain(funded=False)
    conn, deps = _setup_deps(tmp_path, chain)
    with pytest.raises(hc.MigrationRefused, match="not funded"):
        _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))
    assert chain.submitted == []


def test_setup_accepts_a_pending_closets_offer_without_minting(tmp_path):
    chain = _Chain(line={"limit": "1000000000"})
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.PENDING_ACCEPT, offer_id="OLD")
    _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))
    (accept,) = chain.submitted
    assert accept.nftoken_sell_offer == "OLD"
    assert es.get_closet_record(conn, HOUSE)[0] == "C-house"


def test_a_failed_accept_clears_the_offer_for_the_next_run(tmp_path):
    chain = _Chain(line={"limit": "1000000000"}, accept_result="tecOBJECT_NOT_FOUND")
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.PENDING_ACCEPT, offer_id="OLD")
    with pytest.raises(hc.MigrationRefused, match="tecOBJECT_NOT_FOUND"):
        _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))
    assert es.get_closet_record(conn, HOUSE)[3] is None


def test_setup_confirms_a_landed_accept_without_resubmitting(tmp_path):
    chain = _Chain(line={"limit": "1000000000"})
    chain.accepted = True  # a previous run's accept landed; the DB hasn't caught up
    conn, deps = _setup_deps(tmp_path, chain)
    es.set_closet_token(conn, HOUSE, "C-house", "AB", status=ct.PENDING_ACCEPT, offer_id="OLD")
    assert _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))["closet"] == ct.ACTIVE
    assert chain.submitted == []


def test_setup_refuses_a_house_missing_from_the_durable_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(system_wallets, "DURABLE_SYSTEM_ACCOUNTS", frozenset())
    chain = _Chain()
    conn, deps = _setup_deps(tmp_path, chain)
    with pytest.raises(hc.MigrationRefused, match="HISTORICAL_HOUSE_WALLETS"):
        _run(hc.setup_house(HOUSE_WALLET, deps, limit="1000000000"))
    assert chain.submitted == []
