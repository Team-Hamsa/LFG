"""Testnet rehearsal harness for the marketplace fee cover (#515).

Everything runs against a fake XRPL client: nothing here reaches a network.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import stat
from types import SimpleNamespace

import httpx
import pytest
from xrpl.models.requests import AccountNFTs
from xrpl.models.response import Response, ResponseStatus
from xrpl.models.transactions import NFTokenAcceptOffer, NFTokenCreateOffer, NFTokenMint, Payment
from xrpl.wallet import Wallet

from lfg_core import brokers, config, db_path, history_store, memos, xrpl_ops, xrpl_rpc
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

    def __init__(self, fail: type | None = None, network: str = "testnet"):
        self.fail = fail
        self.network = network
        self.faucet_funded: list[str] = []
        self.submitted: list[tuple[object, str, str]] = []  # (tx, signer, label)
        self.results: list[dict] = []
        self.entries: dict[str, dict] = {}
        self.lookups: list[str] = []
        self.owned: set[tuple[str, str]] = set()
        self.ownership_checks: list[tuple[str, str]] = []

    async def ensure_testnet(self):
        if self.network != "testnet":
            raise rehearsal.WrongNetwork(f"endpoint is on {self.network}")

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
        if isinstance(tx, NFTokenCreateOffer):
            # the offer it created is now a ledger object...
            self.entries[meta["offer_id"]] = {
                "LedgerEntryType": "NFTokenOffer",
                "NFTokenID": tx.nftoken_id,
                "Amount": tx.amount,
                "Flags": int(tx.flags or 0),
                "Owner": wallet.address,
            }
        if isinstance(tx, NFTokenAcceptOffer):
            # ...until an accept consumes it and moves the token
            sell = self.entries.get(tx.nftoken_sell_offer or "") or {}
            buy = self.entries.get(tx.nftoken_buy_offer or "") or {}
            nft = sell.get("NFTokenID") or buy.get("NFTokenID")
            if nft:
                self.owned = {(a, n) for a, n in self.owned if n != nft}
                self.owned.add((buy.get("Owner") or wallet.address, nft))
            for consumed in (tx.nftoken_sell_offer, tx.nftoken_buy_offer):
                if consumed:
                    self.entries.pop(consumed, None)
        result = {"hash": f"HASH{n}", "validated": True, "meta": meta}
        self.results.append(result)
        return result

    async def ledger_entry(self, index):
        self.lookups.append(index)
        return self.entries.get(index)

    async def holds_nft(self, address, nft_id):
        self.ownership_checks.append((address, nft_id))
        return (address, nft_id) in self.owned

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
        ["verify", "--offer-index", "BID1"],
    ],
)
def test_main_exits_2_unless_the_network_is_testnet(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(config, "XRPL_NETWORK", "mainnet")
    state = str(tmp_path / "state.json")
    chain = FakeChain()
    assert rehearsal.main([state if a == "STATE" else a for a in argv], chain=chain) == 2
    assert chain.faucet_funded == [] and chain.submitted == []
    assert not os.path.exists(state)


@pytest.mark.parametrize(
    "argv",
    [
        ["setup", "--state", "STATE"],
        ["mint-to-seller", "--state", "STATE"],
        ["list", "--state", "STATE", "--price-xrp", "5"],
        ["broker-accept", "--state", "STATE", "--buy-offer", BUY_OFFER],
    ],
)
def test_ledger_subcommands_check_the_endpoint_before_acting(testnet, state_path, argv):
    """XRPL_NETWORK=testnet is not proof: every subcommand that touches the
    ledger asks the endpoint first, and one that isn't testnet stops the run
    (exit 2) before any funding, lookup or transaction."""
    staged = FakeChain()
    state = _staged(staged, state_path)
    staged.entries[BUY_OFFER] = _bid(state["nft_id"], 5_075_000)
    wrong = FakeChain(network="mainnet")
    wrong.entries = staged.entries
    assert rehearsal.main([state_path if a == "STATE" else a for a in argv], chain=wrong) == 2
    assert wrong.faucet_funded == [] and wrong.submitted == [] and wrong.lookups == []


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


def test_mint_to_seller_refuses_a_zero_transfer_fee(testnet, state_path, monkeypatch):
    """TransferFee 0 means no royalty reaches the issuer, and settlement then
    declines the refund `royalty_unobserved`: refuse before minting rather
    than spend the operator's time on a rehearsal that cannot pass."""
    chain = FakeChain()
    assert rehearsal.main(["setup", "--state", state_path], chain=chain) == 0
    monkeypatch.setattr(config, "NFT_TRANSFER_FEE", 0)
    before = len(chain.submitted)
    assert rehearsal.main(["mint-to-seller", "--state", state_path], chain=chain) == 1
    assert len(chain.submitted) == before
    assert "nft_id" not in _state(state_path)


def _crashed_before_recording_the_accept(state_path):
    """mint and offer recorded, the accept not: the state a crash leaves."""
    crashed = FakeChain(fail=NFTokenAcceptOffer)
    assert rehearsal.main(["setup", "--state", state_path], chain=crashed) == 0
    assert rehearsal.main(["mint-to-seller", "--state", state_path], chain=crashed) == 1
    state = _state(state_path)
    assert state["transfer_offer"] and "seller_holds_nft" not in state
    return state


def test_mint_to_seller_reconciles_a_transfer_offer_already_accepted(testnet, state_path):
    """A crash after the validated accept but before its state was saved made
    the re-run submit the consumed offer, which can only fail. An offer gone
    from the ledger while the seller holds the character means it landed."""
    state = _crashed_before_recording_the_accept(state_path)
    retry = FakeChain()  # the offer is off the ledger and the seller holds it
    retry.owned.add((state["wallets"]["seller"]["address"], state["nft_id"]))
    assert rehearsal.main(["mint-to-seller", "--state", state_path], chain=retry) == 0
    assert _state(state_path)["seller_holds_nft"] is True
    assert retry.txs(NFTokenAcceptOffer) == [] and retry.txs(NFTokenCreateOffer) == []


def test_mint_to_seller_re_offers_a_transfer_offer_that_was_cancelled(testnet, state_path):
    """An absent offer is not proof of an accept: the issuer can cancel its
    own offer, leaving the seller with nothing. Marking the delivery done
    there would have `list` try to sell a character the seller never got."""
    state = _crashed_before_recording_the_accept(state_path)
    stale_offer = state["transfer_offer"]
    seller = state["wallets"]["seller"]["address"]
    retry = FakeChain()  # the offer is gone AND the seller holds nothing
    assert rehearsal.main(["mint-to-seller", "--state", state_path], chain=retry) == 0
    assert retry.ownership_checks == [(seller, state["nft_id"])]
    [(offer, signer)] = retry.txs(NFTokenCreateOffer)  # a fresh delivery offer
    assert (offer.amount, offer.destination, offer.nftoken_id) == ("0", seller, state["nft_id"])
    assert signer == config.SIGNING_ACCOUNT
    [(accept, _)] = retry.txs(NFTokenAcceptOffer)  # ...which the seller accepts
    after = _state(state_path)
    assert accept.nftoken_sell_offer == after["transfer_offer"] != stale_offer
    assert after["seller_holds_nft"] is True and after["nft_id"] == state["nft_id"]


def _mint_meta(nft_id):
    return {
        "TransactionResult": "tesSUCCESS",
        "AffectedNodes": [
            {
                "CreatedNode": {
                    "LedgerEntryType": "NFTokenPage",
                    "NewFields": {"NFTokens": [{"NFToken": {"NFTokenID": nft_id}}]},
                }
            }
        ],
    }


def _offer_meta(offer_index):
    return {
        "TransactionResult": "tesSUCCESS",
        "AffectedNodes": [
            {"CreatedNode": {"LedgerEntryType": "NFTokenOffer", "LedgerIndex": offer_index}}
        ],
    }


def test_ids_are_derived_from_metadata_when_the_convenience_fields_are_absent():
    """meta.nftoken_id / meta.offer_id are conveniences rippled may omit. By
    then the transaction has committed, so deriving the id from AffectedNodes
    is what stops a re-run from minting a second character (xrpl_ops.mint_nft
    derives it for the same reason)."""
    nft_id = "00081388" + "A" * 56
    offer_index = "F" * 64
    assert rehearsal._nft_id({"hash": "H", "meta": _mint_meta(nft_id)}) == nft_id
    assert rehearsal._offer_id({"hash": "H", "meta": _offer_meta(offer_index)}, "l") == offer_index


def test_an_id_that_cannot_be_read_at_all_still_refuses_with_the_tx_hash():
    empty = {"hash": "HASHX", "meta": {"TransactionResult": "tesSUCCESS", "AffectedNodes": []}}
    with pytest.raises(rehearsal.RehearsalError, match="HASHX"):
        rehearsal._nft_id(empty)
    with pytest.raises(rehearsal.RehearsalError, match="HASHX"):
        rehearsal._offer_id(empty, "listing")


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
    # 5_075_020 x 0.01589 = 80_642.07: ceil takes 80_643 where rounding would
    # take 80_642, so this bid tells the two apart (a .75 fraction would not).
    chain.entries[BUY_OFFER] = _bid(state["nft_id"], 5_075_020)
    assert _broker_accept(chain, state_path) == 0
    [(accept, signer)] = [
        (tx, s) for tx, s in chain.txs(NFTokenAcceptOffer) if tx.nftoken_broker_fee is not None
    ]
    assert round(5_075_020 * 0.01589) == 80_642
    assert accept.nftoken_broker_fee == str(math.ceil(5_075_020 * 0.01589)) == "80643"
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


def _campaign(conn, min_bid=0):
    knobs = store.Knobs(
        coverage_bps=10_000,
        budget_drops=1_000_000,
        wallet_cap_drops=1_000_000,
        min_bid_drops=min_bid,
    )
    campaign, _ = store.start_campaign(
        conn, network="testnet", actor="t", knobs=knobs, duration_seconds=None, now=1000
    )
    return campaign


def _bid_promise(conn, campaign, decline_reason=None):
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
        decline_reason=decline_reason,
        now=1001,
    )


def _declined_when_the_bid_started(app_db, reason):
    """A bid the quote already declined: the bid start records NO quote for
    it, but finalize still writes its promise, `declined` with the reason."""
    conn = store.connect(app_db)
    try:
        # below_min_bid is the one verdict record_promise re-decides from the
        # campaign, so that campaign must really set a minimum above the bid.
        min_bid = 9_000_000 if reason == "below_min_bid" else 0
        _bid_promise(conn, _campaign(conn, min_bid=min_bid), decline_reason=reason)
        promise = store.get_promise(conn, "BID1")
        assert (promise["state"], promise["reason"]) == ("declined", reason)
    finally:
        conn.close()


def _covered_bid(app_db, outcome):
    """Quote S1 -> promise on BID1 -> an accept settled `outcome`."""
    conn = store.connect(app_db)
    try:
        campaign = _campaign(conn)
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
        _bid_promise(conn, campaign)
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


@pytest.mark.parametrize("reason", ["below_clearing", "below_min_bid", "system_wallet"])
def test_verify_reports_a_decline_decided_when_the_bid_started(testnet, app_db, capsys, reason):
    """A bid declined at its start has no quote, only a declined promise, so
    only its offer index finds it: verify must still print the reason."""
    _declined_when_the_bid_started(app_db, reason)
    # --timeout 0: a decline is terminal on the first read, so a state that
    # is anything else must fail the test instead of polling for 30 minutes.
    assert rehearsal.main(["verify", "--offer-index", "BID1", "--timeout", "0"]) == 1
    out = capsys.readouterr().out
    assert f"declined: {reason}" in out
    assert "AUDIT PASS" in out


def test_verify_by_session_points_a_quoteless_bid_at_its_offer_index(testnet, app_db, capsys):
    _declined_when_the_bid_started(app_db, "below_clearing")
    assert rehearsal.main(["verify", "--bid", "S1", "--timeout", "0"]) == 1
    out = capsys.readouterr().out
    assert "no_quote" in out and "--offer-index" in out


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_verify_refuses_a_non_finite_timeout(testnet, app_db, capsys, value):
    """`--timeout nan` (or inf) never passes its deadline, so a fee cover that
    is still moving would be polled for ever."""
    _covered_bid(app_db, None)  # `quoted`: a waiting state
    assert rehearsal.main(["verify", "--bid", "S1", "--timeout", value]) == 1
    assert "finite" in capsys.readouterr().err


def test_verify_by_offer_index_follows_a_covered_bid_too(testnet, app_db, capsys):
    _covered_bid(app_db, "confirmed")
    assert rehearsal.main(["verify", "--offer-index", "BID1", "--timeout", "0"]) == 0
    assert "PAYOUTHASH" in capsys.readouterr().out


# --- the real chain ---------------------------------------------------------------


TESTNET_URL = "https://testnet.example/"
MAINNET_URL = "https://mainnet.example/"
NO_ID_URL = "https://no-network-id.example/"
DOWN_URL = "https://down.example/"


class _Endpoint:
    """One fake JSON-RPC endpoint, as xrpl_ops.rpc_client(urls=[url]) returns
    it: answers server_info with its network_id (omitted when None)."""

    def __init__(self, network_id=None, down=False):
        self.network_id = network_id
        self.down = down

    def request(self, req):
        if self.down:
            raise httpx.ConnectError("connection refused")
        info = {"build_version": "2.4.0"}
        if self.network_id is not None:
            info["network_id"] = self.network_id
        return Response(status=ResponseStatus.SUCCESS, result={"info": info})


HELD_NFT = "00081388" + "C" * 56


class _Client:
    """The failover client xrpl_ops.rpc_client() returns: remembers the
    endpoints it may use, and answers ledger_entry and account_nfts."""

    def __init__(self, urls):
        self.urls = list(urls)
        self.requests = []

    def request(self, req):
        self.requests.append(req)
        if isinstance(req, AccountNFTs):
            if req.marker is None:
                page = [{"NFTokenID": "00081388" + "D" * 56}]
                return Response(
                    status=ResponseStatus.SUCCESS,
                    result={"account_nfts": page, "marker": "next"},
                )
            page = [{"NFTokenID": HELD_NFT}]  # found only on the second page
            return Response(status=ResponseStatus.SUCCESS, result={"account_nfts": page})
        if req.index == BUY_OFFER:
            return Response(
                status=ResponseStatus.SUCCESS, result={"node": {"LedgerEntryType": "NFTokenOffer"}}
            )
        return Response(status=ResponseStatus.ERROR, result={"error": "entryNotFound"})


@pytest.fixture
def endpoints(monkeypatch, tmp_path):
    table = {
        TESTNET_URL: _Endpoint(network_id=1),
        MAINNET_URL: _Endpoint(network_id=0),
        NO_ID_URL: _Endpoint(network_id=None),
        DOWN_URL: _Endpoint(down=True),
    }
    seen = SimpleNamespace(clients=[], submitted=[], funded=[])

    def rpc_client(urls=None):
        if urls is not None:
            [url] = urls
            return table[url]
        client = _Client(xrpl_rpc.default_urls(config.JSON_RPC_URLS))
        seen.clients.append(client)
        return client

    async def submit(tx, wallet, client, label, **kwargs):
        seen.submitted.append((client, label))
        if label == "refused":
            return None  # a definitive, validated failure
        meta = {"TransactionResult": "tesSUCCESS", "nftoken_id": "A" * 64, "offer_id": "B" * 64}
        return {"hash": "H", "validated": True, "meta": meta}

    async def faucet(client, wallet):
        seen.funded.append((client, wallet.address))
        return wallet

    monkeypatch.setattr(xrpl_ops, "rpc_client", rpc_client)
    monkeypatch.setattr(xrpl_ops, "_submit_and_confirm", submit)
    monkeypatch.setattr(rehearsal, "generate_faucet_wallet", faucet)
    # An archive with no recorded testnet genesis: the ledger-32570 anchor
    # check has nothing to compare against, so network_id decides alone.
    history = str(tmp_path / "history.db")
    monkeypatch.setattr(history_store, "history_db_path", lambda network=None: history)
    return seen


def test_a_mainnet_endpoint_is_refused_before_any_submit(
    testnet, state_path, endpoints, monkeypatch
):
    """XRPL_NETWORK=testnet proves nothing about the endpoint: an explicit
    XRPL_JSON_RPC_URL wins over the network's defaults, and an exported shell
    variable beats .env. On a mainnet endpoint, mint-to-seller would sign a
    real mainnet mint with the environment's key."""
    assert rehearsal.main(["setup", "--state", state_path], chain=FakeChain()) == 0
    monkeypatch.setattr(config, "JSON_RPC_URLS", [MAINNET_URL])
    assert rehearsal.main(["mint-to-seller", "--state", state_path]) == 2
    assert endpoints.submitted == [] and endpoints.funded == []
    assert "nft_id" not in _state(state_path)


@pytest.mark.parametrize(
    ("urls", "code", "verified"),
    [
        # a submit can fail over to any configured endpoint, so each is asked
        ([TESTNET_URL, MAINNET_URL], 2, None),
        ([MAINNET_URL, TESTNET_URL], 2, None),
        ([NO_ID_URL], 2, None),  # an endpoint that can't say it is testnet isn't trusted
        ([DOWN_URL, TESTNET_URL], 0, [TESTNET_URL]),  # unreachable: excluded for the run
        ([DOWN_URL], 1, None),  # nothing verified: nothing to submit through
    ],
)
def test_every_endpoint_a_submit_can_reach_must_be_testnet(
    testnet, state_path, endpoints, monkeypatch, urls, code, verified
):
    assert rehearsal.main(["setup", "--state", state_path], chain=FakeChain()) == 0
    monkeypatch.setattr(config, "JSON_RPC_URLS", urls)
    assert rehearsal.main(["mint-to-seller", "--state", state_path]) == code
    if verified is None:
        assert endpoints.submitted == []
    else:
        assert len(endpoints.submitted) == 3  # mint, offer, accept
        assert all(client.urls == verified for client, _ in endpoints.submitted)


def test_the_faucet_helper_drives_the_client_rpc_client_returns(monkeypatch, tmp_path):
    """The async faucet helper is handed the client xrpl_ops.rpc_client()
    returns. xrpl-py's async helpers reach a client only through its
    `_request_impl` coroutine, which JsonRpcBase defines and
    FailoverJsonRpcClient overrides — the same client xrpl_ops hands to
    submit_and_wait. This drives the REAL helper over the REAL client with
    only its HTTP boundary faked."""
    monkeypatch.setattr(config, "JSON_RPC_URLS", [TESTNET_URL])
    monkeypatch.setattr(
        history_store, "history_db_path", lambda network=None: str(tmp_path / "history.db")
    )
    assert inspect.iscoroutinefunction(xrpl_rpc.FailoverJsonRpcClient._request_impl)
    balances = iter(["0", "100000000", "100000000", "100000000"])
    seen: list[str] = []

    async def request_impl(self, request, *args, **kwargs):
        method = str(request.method).lower()  # e.g. "requestmethod.server_info"
        seen.append(method)
        if "server_info" in method:
            info = {"network_id": 1, "build_version": "2.4.0"}
            return Response(status=ResponseStatus.SUCCESS, result={"info": info})
        if "account_info" in method:
            account = {"Balance": next(balances), "Sequence": 7}
            return Response(status=ResponseStatus.SUCCESS, result={"account_data": account})
        raise AssertionError(f"unexpected request: {method}")

    monkeypatch.setattr(xrpl_rpc.FailoverJsonRpcClient, "_request_impl", request_impl)
    posted: list[tuple[str, dict]] = []

    class _Faucet:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            posted.append((url, json))
            return httpx.Response(200)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Faucet())
    chain = rehearsal.XrplChain()
    _run(chain.ensure_testnet())
    wallet = Wallet.create()
    _run(chain.fund_from_faucet(wallet))
    [(url, body)] = posted
    assert "faucet.altnet.rippletest.net" in url and body["destination"] == wallet.address
    assert any("server_info" in m for m in seen) and any("account_info" in m for m in seen)


def test_the_real_chain_sends_everything_through_rpc_client(endpoints, monkeypatch):
    monkeypatch.setattr(config, "JSON_RPC_URLS", [TESTNET_URL])
    chain = rehearsal.XrplChain()
    wallet = Wallet.create()
    tx = Payment(account=wallet.address, destination=CAFE, amount="1")
    with pytest.raises(rehearsal.RehearsalError):  # nothing is sent before the check
        _run(chain.submit(tx, wallet, "ok"))
    _run(chain.ensure_testnet())
    [client] = endpoints.clients
    assert client.urls == [TESTNET_URL]
    assert _run(chain.submit(tx, wallet, "ok"))["hash"] == "H"
    with pytest.raises(rehearsal.RehearsalError):  # a definitive, validated failure
        _run(chain.submit(tx, wallet, "refused"))
    _run(chain.fund_from_faucet(wallet))
    assert _run(chain.ledger_entry(BUY_OFFER)) == {"LedgerEntryType": "NFTokenOffer"}
    assert _run(chain.ledger_entry("C" * 64)) is None
    # account_nfts, over the same verified endpoints, and it follows markers
    assert _run(chain.holds_nft(wallet.address, HELD_NFT)) is True
    assert _run(chain.holds_nft(wallet.address, "00081388" + "E" * 56)) is False
    assert endpoints.submitted == [(client, "ok"), (client, "refused")]
    assert endpoints.funded == [(client, wallet.address)]
    assert [type(r).__name__ for r in client.requests] == [
        "LedgerEntry",
        "LedgerEntry",
        "AccountNFTs",
        "AccountNFTs",
        "AccountNFTs",
        "AccountNFTs",
    ]
