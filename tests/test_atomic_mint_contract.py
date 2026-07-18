import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from xrpl.models import IssuedCurrencyAmount
from xrpl.wallet import Wallet

from lfg_core import atomic_mint, mint_flow, xrpl_actions
from lfg_service import app as server

BUYER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
ISSUER = "rLs1MzkFWCxTbuAHgjeTZK4fcCDDnf2KRv"
NFT_ISSUER = "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
SEED = "sEdTM1uX8pu2do5XvTnutH6HsouMaM2"


def _request(method: str, path: str, body: dict | None = None):
    request = make_mocked_request(method, path, app=web.Application())

    async def request_json():
        return body or {}

    request.json = request_json  # type: ignore[method-assign]
    return request


def _json(response: web.Response) -> dict:
    return json.loads(response.body.decode())


class ContractDeps:
    def __init__(self):
        self.payload_count = 0
        self.payload_status_result = None
        self.rows = {}
        self.persisted = []
        self.events = []
        self.payment = xrpl_actions.MintPayment(
            "LFGO",
            "1",
            ISSUER,
            IssuedCurrencyAmount(
                currency="4C46474F00000000000000000000000000000000",
                issuer=ISSUER,
                value="1",
            ),
        )
        self.assets = mint_flow.PreparedMintAssets(
            nft_number=7401,
            session_tag="action:contract",
            metadata_url="https://cdn.example/7401/7401_0.json",
            image_url="https://cdn.example/7401/7401_0.png",
            video_url=None,
            metadata={
                "attributes": [
                    {"trait_type": "Body", "value": "Straight"}
                ]
            },
            traits={"Body": "Straight", "Hat": "Hard Hat"},
            body_type="male",
        )

    async def capability(self):
        return xrpl_actions.BatchCapability(True, None)

    async def choose_payment(self, wallet):
        assert wallet == BUYER
        return self.payment

    async def reserve_headroom(self, session):
        return True

    async def allocate_number(self):
        return 7401

    async def prepare_assets(self, number, tag):
        assert number == 7401
        return replace(self.assets, session_tag=tag)

    async def list_tickets(self):
        return [9001]

    async def lease_ticket(self, session, tickets):
        return tickets[0]

    async def prepare_batch(self, session, assets, payment):
        batch = xrpl_actions.build_atomic_mint_batch(
            buyer=BUYER,
            issuer_account=ISSUER,
            nft_issuer=NFT_ISSUER,
            issuer_ticket=9001,
            metadata_url=assets.metadata_url,
            payment=payment,
            platform="webapp",
            campaign="x-mint-link",
            nft_flags=25,
            nft_taxon=0,
            transfer_fee=7000,
            source_tag=2606160021,
        )
        pay, mint, accept = batch.raw_transactions
        filled = replace(
            batch,
            sequence=100,
            fee="40",
            last_ledger_sequence=500,
            raw_transactions=[
                replace(pay, sequence=101, fee="0", signing_pub_key=""),
                replace(mint, sequence=0, fee="0", signing_pub_key=""),
                replace(
                    accept, sequence=102, fee="0", signing_pub_key=""
                ),
            ],
        )
        signed = xrpl_actions.sign_issuer_batch(
            filled, wallet=Wallet.from_seed(SEED), issuer_account=ISSUER
        )
        hashes = tuple(tx.get_hash() for tx in signed.raw_transactions)
        return xrpl_actions.PreparedBatch(
            transaction=signed,
            offer_id=xrpl_actions.nft_offer_id(ISSUER, 9001),
            inner_hashes=(hashes[0], hashes[1], hashes[2]),
            last_ledger_sequence=500,
        )

    async def persist(self, session):
        self.persisted.append((session.state, session.batch_json))

    async def create_payload(self, session):
        self.payload_count += 1
        return {
            "uuid": "00000000-0000-4000-8000-000000000001",
            "xumm_url": "https://xumm.app/sign/one-batch",
            "qr_url": "https://xumm.app/qr/one-batch.png",
            "push": None,
        }

    async def payload_status(self, uuid):
        return self.payload_status_result

    async def verify_batch(self, session):
        async def fetch(tx_hash):
            return self.rows.get(tx_hash)

        return await xrpl_actions.verify_atomic_batch_result(
            outer_hash=session.outer_hash,
            inner_hashes=session.inner_hashes,
            expected_offer_id=session.offer_id,
            fetch_tx=fetch,
        )

    async def current_ledger(self):
        return 499

    async def record_mint(self, session, nft_id):
        self.events.append("record")
        return True

    async def buy_and_burn(self, session):
        self.events.append("buy-and-burn")

    async def settle_headroom(self, session, minted):
        self.events.append(f"headroom:{minted}")

    async def discard_assets(self, session):
        self.events.append("discard")

    async def release_number(self, session):
        self.events.append("release-number")

    async def release_ticket(self, session):
        self.events.append("release-ticket")
        return True

    async def consume_ticket(self, session):
        self.events.append("consume-ticket")

    async def quarantine_ticket(self, session):
        self.events.append("quarantine-ticket")

    def deps(self):
        return atomic_mint.AtomicMintDeps(
            capability=self.capability,
            choose_payment=self.choose_payment,
            reserve_headroom=self.reserve_headroom,
            allocate_number=self.allocate_number,
            prepare_assets=self.prepare_assets,
            list_tickets=self.list_tickets,
            lease_ticket=self.lease_ticket,
            prepare_batch=self.prepare_batch,
            persist=self.persist,
            create_payload=self.create_payload,
            payload_status=self.payload_status,
            verify_batch=self.verify_batch,
            current_ledger=self.current_ledger,
            ledger_tickets=self.list_tickets,
            record_mint=self.record_mint,
            buy_and_burn=self.buy_and_burn,
            settle_headroom=self.settle_headroom,
            discard_assets=self.discard_assets,
            release_number=self.release_number,
            release_ticket=self.release_ticket,
            consume_ticket=self.consume_ticket,
            quarantine_ticket=self.quarantine_ticket,
        )

    def validate_all_in_ledger(self, session, ledger_index):
        pay_hash, mint_hash, accept_hash = session.inner_hashes
        offer_id = session.offer_id
        self.rows = {
            "OUTER": {
                "validated": True,
                "ledger_index": ledger_index,
                "tx_json": {"TransactionType": "Batch"},
                "meta": {"TransactionResult": "tesSUCCESS"},
            },
            pay_hash: {
                "validated": True,
                "ledger_index": ledger_index,
                "tx_json": {"TransactionType": "Payment"},
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "ParentBatchID": "OUTER",
                },
            },
            mint_hash: {
                "validated": True,
                "ledger_index": ledger_index,
                "tx_json": {"TransactionType": "NFTokenMint"},
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "ParentBatchID": "OUTER",
                    "nftoken_id": "NFT7401",
                },
            },
            accept_hash: {
                "validated": True,
                "ledger_index": ledger_index,
                "tx_json": {
                    "TransactionType": "NFTokenAcceptOffer",
                    "NFTokenSellOffer": offer_id,
                },
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "ParentBatchID": "OUTER",
                    "nftoken_id": "NFT7401",
                    "AffectedNodes": [
                        {
                            "DeletedNode": {
                                "LedgerEntryType": "NFTokenOffer",
                                "LedgerIndex": offer_id,
                            }
                        }
                    ],
                },
            },
        }


@pytest.mark.asyncio
async def test_action_contract_is_payment_first_one_request_and_same_ledger(
    monkeypatch,
):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server.mock_economy, "DEV_OWNER", BUYER)
    monkeypatch.setattr(server, "atomic_mint_sessions", {})
    monkeypatch.setattr(server, "_action_create_hits", {})
    monkeypatch.setattr(
        server,
        "_action_readiness",
        AsyncMock(return_value=xrpl_actions.BatchCapability(True, None)),
    )
    monkeypatch.setattr(server, "_push_token", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "_persist_atomic_session", AsyncMock())
    scheduled = []
    monkeypatch.setattr(
        server, "_schedule_atomic_mint", lambda session: scheduled.append(session)
    )

    start = await server.handle_action_create(
        _request(
            "POST",
            "/api/actions/mint",
            {"account": BUYER, "campaign": "x-mint-link"},
        )
    )
    started = _json(start)
    assert start.status == 202
    assert len(scheduled) == 1
    session = scheduled[0]
    deps = ContractDeps()
    await atomic_mint.prepare_session(session, deps.deps())

    async def refresh(value):
        await atomic_mint.refresh_session(value, deps.deps())

    monkeypatch.setattr(server, "_refresh_atomic_mint", refresh)
    status_request = _request(
        "GET", f"/api/actions/mint/{started['sessionId']}"
    )
    status_request._match_info = {"session_id": started["sessionId"]}
    ready_response = await server.handle_action_status(status_request)
    ready = _json(ready_response)
    raw = ready["transaction"]["RawTransactions"]
    assert [row["RawTransaction"]["TransactionType"] for row in raw] == [
        "Payment",
        "NFTokenMint",
        "NFTokenAcceptOffer",
    ]
    assert raw[1]["RawTransaction"]["Amount"] == "0"
    assert raw[1]["RawTransaction"]["Destination"] == BUYER
    assert raw[2]["RawTransaction"]["NFTokenSellOffer"] == session.offer_id
    assert ready["wallets"]["xaman"]["deeplink"].endswith("one-batch")
    assert deps.payload_count == 1
    assert "ticket_sequence" not in ready

    deps.payload_status_result = {
        "signed": True,
        "account": BUYER,
        "txid": "OUTER",
        "expired": False,
        "cancelled": False,
    }
    deps.validate_all_in_ledger(session, ledger_index=740)
    done_response = await server.handle_action_status(status_request)
    done = _json(done_response)
    assert done["state"] == "done"
    assert done["nft_id"] == "NFT7401"
    assert done["ledger_index"] == 740
    assert len(done["inner_hashes"]) == 3
    assert "transaction" not in done and "wallets" not in done
    assert deps.payload_count == 1
    assert deps.events.index("record") < deps.events.index("consume-ticket")
