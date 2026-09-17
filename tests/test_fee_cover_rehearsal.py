"""Testnet rehearsal harness for the marketplace fee cover (#515).

Everything runs against a fake XRPL client: nothing here reaches a network.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat

import pytest
from xrpl.models.response import Response, ResponseStatus
from xrpl.models.transactions import NFTokenAcceptOffer, NFTokenCreateOffer, NFTokenMint, Payment
from xrpl.wallet import Wallet

from lfg_core import brokers, config, db_path, memos, xrpl_ops
from lfg_core import fee_cover_store as store
from scripts import fee_cover_rehearsal as rehearsal

CAFE = "rpx9JThQ2y37FaGeeJP7PXDUVEXY3PHZSC"
BIDDER = "rBidderAddress0000000000000000000"
BUY_OFFER = "B" * 64


def _run(coro):
    # A private loop: never disturbs (or depends on) the global event loop.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeChain:
    """Stands in for the XRPL. Records every faucet funding and every
    transaction with the address that signed it, and answers validated results
    shaped like rippled's (meta `nftoken_id` / `offer_id`)."""

    def __init__(self, fail: type | None = None):
        self.fail = fail
        self.faucet_funded: list[str] = []
        self.submitted: list[tuple[object, str, str]] = []  # (tx, signer, label)
        self.results: list[dict] = []
        self.entries: dict[str, dict] = {}

    async def fund_from_faucet(self, wallet):
        self.faucet_funded.append(wallet.address)

    async def submit(self, tx, wallet, label):
        if self.fail is not None and isinstance(tx, self.fail):
            raise rehearsal.RehearsalError(f"{label}: tecUNFUNDED_PAYMENT")
        self.submitted.append((tx, wallet.address, label))
        n = len(self.submitted)
        meta: dict = {"TransactionResult": "tesSUCCESS"}
        if isinstance(tx, NFTokenMint):
            meta["nftoken_id"] = f"{n:064X}"
        if isinstance(tx, NFTokenCreateOffer):
            meta["offer_id"] = f"{n:064X}"
        result = {"hash": f"HASH{n}", "validated": True, "meta": meta}
        self.results.append(result)
        return result

    async def ledger_entry(self, index):
        return self.entries.get(index)

    def txs(self, kind):
        return [(tx, signer) for tx, signer, _ in self.submitted if isinstance(tx, kind)]

    def result_for(self, tx):
        return next(r for (t, _, _), r in zip(self.submitted, self.results, strict=True) if t is tx)


@pytest.fixture
def testnet(monkeypatch):
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "rehearsal" / "state.json")


def _state(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _staged(chain, state_path, price="4.99"):
    """setup -> mint-to-seller -> list, the way an operator runs them."""
    assert rehearsal.main(["setup", "--state", state_path], chain=chain) == 0
    assert rehearsal.main(["mint-to-seller", "--state", state_path], chain=chain) == 0
    assert rehearsal.main(["list", "--state", state_path, "--price-xrp", price], chain=chain) == 0
    return _state(state_path)


def _bid(nft_id, drops):
    return {
        "LedgerEntryType": "NFTokenOffer",
        "Flags": 0,
        "NFTokenID": nft_id,
        "Amount": str(drops),
        "Owner": BIDDER,
    }


def _broker_accept(chain, state_path):
    return rehearsal.main(
        ["broker-accept", "--state", state_path, "--buy-offer", BUY_OFFER], chain=chain
    )


# --- network guard --------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["setup", "--state", "STATE"],
        ["mint-to-seller", "--state", "STATE"],
        ["list", "--state", "STATE", "--price-xrp", "5"],
        ["print-allowlist", "--state", "STATE"],
        ["broker-accept", "--state", "STATE", "--buy-offer", BUY_OFFER],
        ["verify", "--bid", "S1"],
    ],
)
def test_main_exits_2_unless_the_network_is_testnet(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    state = str(tmp_path / "state.json")
    chain = FakeChain()
    assert rehearsal.main([state if a == "STATE" else a for a in argv], chain=chain) == 2
    assert chain.faucet_funded == [] and chain.submitted == []
    assert not os.path.exists(state)


# --- every transaction ----------------------------------------------------------


def test_every_built_tx_carries_the_source_tag_and_memos(testnet, state_path):
    chain = FakeChain()
    state = _staged(chain, state_path)
    chain.entries[BUY_OFFER] = _bid(state["nft_id"], 5_075_000)
    assert _broker_accept(chain, state_path) == 0
    assert {type(tx) for tx, _, _ in chain.submitted} == {
        Payment,
        NFTokenMint,
        NFTokenCreateOffer,
        NFTokenAcceptOffer,
    }
    for tx, _, label in chain.submitted:
        wire = tx.to_xrpl()
        assert wire["SourceTag"] == 2606160021, label
        decoded = memos.decode_memos(wire.get("Memos"))
        assert decoded is not None, label
        memos.assert_valid_values(decoded)
        assert decoded["campaign"] == rehearsal.CAMPAIGN, label


# --- setup ----------------------------------------------------------------------


def test_setup_funds_the_seller_from_the_script_created_intermediate(testnet, state_path):
    """The seller's activation funder must differ from a faucet-funded
    bidder's, or the linkage rule reads them as one person. So the faucet
    funds a script-created intermediate, and the intermediate funds the
    seller: the faucet never pays the seller directly."""
    chain = FakeChain()
    assert rehearsal.main(["setup", "--state", state_path], chain=chain) == 0
    state = _state(state_path)
    address = {role: state["wallets"][role]["address"] for role in rehearsal.ROLES}
    assert len(set(address.values())) == 3
    assert address["seller"] not in chain.faucet_funded
    assert address["intermediate"] in chain.faucet_funded
    [(payment, signer)] = chain.txs(Payment)
    assert payment.account == signer == address["intermediate"]
    assert payment.destination == address["seller"]
    assert int(payment.amount) == rehearsal.SELLER_FUNDING_DROPS
    # the seeds live in the --state file only, readable by its owner alone
    assert stat.S_IMODE(os.stat(state_path).st_mode) == 0o600
    for role in rehearsal.ROLES:
        assert Wallet.from_seed(state["wallets"][role]["seed"]).address == address[role]


def test_setup_resumes_after_a_failed_step_without_refunding_wallets(testnet, state_path):
    first = FakeChain(fail=Payment)
    assert rehearsal.main(["setup", "--state", state_path], chain=first) == 1
    funded_once = _state(state_path)
    retry = FakeChain()
    assert rehearsal.main(["setup", "--state", state_path], chain=retry) == 0
    assert retry.faucet_funded == []  # the faucet-funded wallets were kept, not re-funded
    [(payment, signer)] = retry.txs(Payment)
    state = _state(state_path)
    for role in rehearsal.ROLES:
        assert state["wallets"][role]["address"] == funded_once["wallets"][role]["address"]
    assert payment.destination == state["wallets"]["seller"]["address"]
    assert signer == state["wallets"]["intermediate"]["address"]


def test_the_state_file_is_refused_inside_the_repo(testnet):
    chain = FakeChain()
    inside = os.path.join(rehearsal.REPO_ROOT, "fee-cover-rehearsal-state.json")
    assert rehearsal.main(["setup", "--state", inside], chain=chain) == 2
    assert not os.path.exists(inside)
    assert chain.faucet_funded == [] and chain.submitted == []


# --- mint-to-seller / list / print-allowlist ------------------------------------


def test_mint_to_seller_and_list_stage_a_brokered_listing(testnet, state_path, monkeypatch):
    monkeypatch.setattr(config, "NFT_TRANSFER_FEE", 7000)
    monkeypatch.setattr(config, "NFT_FLAGS", 25)
    chain = FakeChain()
    state = _staged(chain, state_path, price="4.99")
    issuer = config.SIGNING_ACCOUNT
    seller = state["wallets"]["seller"]["address"]
    broker = state["wallets"]["broker"]["address"]
    [(mint, _)] = chain.txs(NFTokenMint)
    assert (mint.account, mint.transfer_fee, mint.nftoken_taxon, int(mint.flags)) == (
        issuer,
        7000,
        config.NFT_TAXON,
        25,
    )
    (transfer, _), (listing, list_signer) = chain.txs(NFTokenCreateOffer)
    # a destination-locked 0-drop offer hands the character to the seller...
    assert (transfer.account, transfer.amount, transfer.destination, transfer.nftoken_id) == (
        issuer,
        "0",
        seller,
        state["nft_id"],
    )
    [(accept, accept_signer)] = chain.txs(NFTokenAcceptOffer)
    assert accept_signer == accept.account == seller
    assert accept.nftoken_sell_offer == state["transfer_offer"]
    # ...who lists it so that only the broker can take it
    assert (listing.account, list_signer, listing.destination, listing.amount) == (
        seller,
        seller,
        broker,
        "4990000",
    )
    assert state["nft_id"] == chain.result_for(mint)["meta"]["nftoken_id"]
    assert state["sell_offer"] == chain.result_for(listing)["meta"]["offer_id"]
    assert state["ask_drops"] == 4_990_000


def test_mint_to_seller_refuses_a_non_transferable_character(testnet, state_path, monkeypatch):
    """TransferFee is only valid on a transferable token, and a character the
    seller can't sell can't be rehearsed: refuse before minting anything."""
    chain = FakeChain()
    assert rehearsal.main(["setup", "--state", state_path], chain=chain) == 0
    monkeypatch.setattr(config, "NFT_FLAGS", 16)  # mutable only
    before = len(chain.submitted)
    assert rehearsal.main(["mint-to-seller", "--state", state_path], chain=chain) == 1
    assert len(chain.submitted) == before


def test_print_allowlist_emits_an_overlay_the_app_loads(
    testnet, state_path, monkeypatch, capsys, tmp_path
):
    assert rehearsal.main(["setup", "--state", state_path], chain=FakeChain()) == 0
    broker = _state(state_path)["wallets"]["broker"]["address"]
    capsys.readouterr()
    assert rehearsal.main(["print-allowlist", "--state", state_path]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {
        broker: {"name": "testbroker", "url_template": None, "broker_rate": 0.01589}
    }
    overlay = tmp_path / "allowlist.json"
    overlay.write_text(out)
    monkeypatch.setenv("BROKER_ALLOWLIST_PATH", str(overlay))
    assert brokers.resolve(broker, "00" * 32) == {
        "name": "testbroker",
        "url": None,
        "broker_rate": 0.01589,
    }


# --- broker-accept ----------------------------------------------------------------


def test_broker_fee_is_ceil_of_bid_times_rate(testnet, state_path):
    chain = FakeChain()
    state = _staged(chain, state_path, price="4.99")
    chain.entries[BUY_OFFER] = _bid(state["nft_id"], 5_075_000)
    assert _broker_accept(chain, state_path) == 0
    [(accept, signer)] = [
        (tx, s) for tx, s in chain.txs(NFTokenAcceptOffer) if tx.nftoken_broker_fee is not None
    ]
    # ceil(5_075_000 x 0.01589) = ceil(80_641.75): cafe's verified fee on this bid
    assert accept.nftoken_broker_fee == str(math.ceil(5_075_000 * 0.01589)) == "80642"
    assert (accept.nftoken_buy_offer, accept.nftoken_sell_offer) == (BUY_OFFER, state["sell_offer"])
    assert signer == accept.account == state["wallets"]["broker"]["address"]


@pytest.mark.parametrize("ask", [1, 999_999, 4_990_000, 5_000_000, 123_456_789])
def test_a_bid_at_the_apps_clearing_price_clears_after_the_broker_fee(monkeypatch, ask):
    """The harness's fee and the app's Buy-now clearing math must agree, or a
    bid the app placed at the clearing price is one the harness refuses."""
    monkeypatch.delenv("BROKER_CLEARING_BUFFER_DROPS", raising=False)
    bid = brokers.clearing_drops(ask, rehearsal.BROKER_RATE)
    assert bid - rehearsal.broker_fee_drops(bid) >= ask


def test_broker_accept_refuses_a_bid_a_broker_would_not_fill(testnet, state_path):
    chain = FakeChain()
    state = _staged(chain, state_path, price="4.99")
    chain.entries[BUY_OFFER] = _bid(state["nft_id"], 5_000_000)  # less its fee, under the ask
    before = len(chain.submitted)
    assert _broker_accept(chain, state_path) == 1
    assert len(chain.submitted) == before


# --- verify -----------------------------------------------------------------------


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    monkeypatch.setattr(db_path, "app_db_path", lambda network=None: path)
    return path


def _covered_bid(app_db, outcome):
    """Quote S1 -> promise on BID1 -> an accept settled `outcome`."""
    conn = store.connect(app_db)
    try:
        knobs = store.Knobs(
            coverage_bps=10_000, budget_drops=1_000_000, wallet_cap_drops=1_000_000, min_bid_drops=0
        )
        campaign, _ = store.start_campaign(
            conn, network="testnet", actor="t", knobs=knobs, duration_seconds=None, now=1000
        )
        store.record_quote(
            conn,
            store.Quote(
                session_id="S1",
                network="testnet",
                payload_uuid="U1",
                txid=None,
                nft_id="NFT1",
                owner="rSeller",
                bidder=BIDDER,
                bid_drops=5_075_000,
                bid_expires_at=None,
                listing={},
                created_at=1000,
            ),
        )
        if outcome is None:
            return
        store.record_promise(
            conn,
            campaign,
            store.PromiseInput(
                offer_index="BID1",
                network="testnet",
                nft_id="NFT1",
                bidder=BIDDER,
                bid_drops=5_075_000,
                ask_drops=4_990_000,
                broker=CAFE,
                broker_rate=0.01589,
                bid_expiration=None,
            ),
            decline_reason=None,
            now=1001,
        )
        store.close_quote(conn, "S1", "promised", offer_index="BID1", now=1001)
        if outcome == "confirmed":
            decision = store.RefundDecision("ACC1", "rSeller", CAFE, 80_642, 349_605, 80_642, None)
            store.fill_promise(conn, "BID1", decision, now=1100)
            store.claim_for_payout(conn, "ACC1", 5_000, claim_ledger=4_000, now=1101)
            store.record_payout(
                conn, "ACC1", state="confirmed", tx_hash="PAYOUTHASH", last_ledger_seq=4_990
            )
        else:
            decision = store.RefundDecision(
                "ACC1", "rSeller", CAFE, 80_642, 349_605, 0, "linked_counterparty"
            )
            store.fill_promise(conn, "BID1", decision, now=1100)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("outcome", "code", "shown"),
    [("confirmed", 0, "PAYOUTHASH"), ("declined", 1, "linked_counterparty")],
)
def test_verify_reports_the_outcome_and_runs_the_audit(
    testnet, app_db, capsys, outcome, code, shown
):
    _covered_bid(app_db, outcome)
    assert rehearsal.main(["verify", "--bid", "S1"]) == code
    out = capsys.readouterr().out
    assert shown in out
    assert "AUDIT PASS" in out


def test_verify_gives_up_at_its_timeout(testnet, app_db, capsys):
    _covered_bid(app_db, None)  # quoted, never promised
    assert rehearsal.main(["verify", "--bid", "S1", "--timeout", "0"]) == 1
    assert "timed out" in capsys.readouterr().out


# --- the real chain ---------------------------------------------------------------


class _Client:
    """A fake XRPL JSON-RPC client: answers ledger_entry only."""

    def __init__(self):
        self.requests = []

    def request(self, req):
        self.requests.append(req)
        if req.index == BUY_OFFER:
            return Response(
                status=ResponseStatus.SUCCESS, result={"node": {"LedgerEntryType": "NFTokenOffer"}}
            )
        return Response(status=ResponseStatus.ERROR, result={"error": "entryNotFound"})


def test_the_real_chain_sends_everything_through_rpc_client(monkeypatch):
    client = _Client()
    monkeypatch.setattr(xrpl_ops, "rpc_client", lambda urls=None: client)
    submitted = []

    async def fake_submit(tx, wallet, used_client, label, **kwargs):
        submitted.append(used_client)
        return None if label == "refused" else {"hash": "H", "validated": True, "meta": {}}

    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", fake_submit)
    funded = []

    async def fake_faucet(used_client, wallet):
        funded.append((used_client, wallet.address))
        return wallet

    monkeypatch.setattr(rehearsal, "generate_faucet_wallet", fake_faucet)
    chain = rehearsal.XrplChain()
    wallet = Wallet.create()
    tx = Payment(account=wallet.address, destination=CAFE, amount="1")
    assert _run(chain.submit(tx, wallet, "ok"))["hash"] == "H"
    with pytest.raises(rehearsal.RehearsalError):  # a definitive, validated failure
        _run(chain.submit(tx, wallet, "refused"))
    _run(chain.fund_from_faucet(wallet))
    assert _run(chain.ledger_entry(BUY_OFFER)) == {"LedgerEntryType": "NFTokenOffer"}
    assert _run(chain.ledger_entry("C" * 64)) is None
    assert submitted == [client, client]
    assert funded == [(client, wallet.address)]
    assert len(client.requests) == 2
