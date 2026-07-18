import pytest

from lfg_core import atomic_mint, mint_flow, xrpl_actions


class _CanonicalBatch:
    def to_xrpl(self):
        return {
            "TransactionType": "Batch",
            "Account": "rBuyer",
            "RawTransactions": [
                {"RawTransaction": {"TransactionType": "Payment"}},
                {
                    "RawTransaction": {
                        "TransactionType": "NFTokenMint",
                        "Amount": "0",
                    }
                },
                {"RawTransaction": {"TransactionType": "NFTokenAcceptOffer"}},
            ],
        }


class FakeAtomicDeps:
    def __init__(self):
        self.events = []
        self.capability_result = xrpl_actions.BatchCapability(True, None)
        self.payment = xrpl_actions.MintPayment("XRP", "10", "rIssuer", "10000000")
        self.assets = mint_flow.PreparedMintAssets(
            nft_number=4001,
            session_tag="action:s1",
            metadata_url="https://cdn.example/4001/4001_0.json",
            image_url="https://cdn.example/4001/4001_0.png",
            video_url=None,
            metadata={
                "attributes": [{"trait_type": "Body", "value": "Straight"}]
            },
            traits={"Body": "Straight", "Hat": "None"},
            body_type="male",
        )
        self.tickets = [7]
        self.payload = {
            "uuid": "u1",
            "xumm_url": "https://xumm.app/sign/u1",
            "qr_url": "https://qr/u1.png",
            "push": None,
        }
        self.status = None
        self.verified = None
        self.verify_error = None
        self.current = 90
        self.ledger_ticket_values = [7]
        self.released_tickets = []
        self.quarantined_tickets = []

    async def capability(self):
        self.events.append("capability")
        return self.capability_result

    async def choose_payment(self, wallet):
        self.events.append("quote")
        return self.payment

    async def reserve_headroom(self, session):
        self.events.append("reserve-headroom")
        return True

    async def allocate_number(self):
        self.events.append("allocate-number")
        return 4001

    async def prepare_assets(self, number, tag):
        self.events.append("prepare-assets")
        return self.assets

    async def list_tickets(self):
        self.events.append("list-tickets")
        return self.tickets

    async def lease_ticket(self, session, tickets):
        self.events.append("lease-ticket")
        return min(tickets) if tickets else None

    async def prepare_batch(self, session, assets, payment):
        self.events.append("prepare-batch")
        return xrpl_actions.PreparedBatch(
            transaction=_CanonicalBatch(),
            offer_id="OFFER7",
            inner_hashes=("PAY", "MINT", "ACCEPT"),
            last_ledger_sequence=100,
        )

    async def persist(self, session):
        if session.batch_json and "persist-batch" not in self.events:
            self.events.append("persist-batch")
        else:
            self.events.append(f"persist-{session.state}")

    async def create_payload(self, session):
        self.events.append("create-xaman")
        return self.payload

    async def payload_status(self, uuid):
        self.events.append("payload-status")
        return self.status

    async def verify_batch(self, session):
        self.events.append("verify-batch")
        if self.verify_error is not None:
            raise self.verify_error
        return self.verified

    async def current_ledger(self):
        return self.current

    async def ledger_tickets(self):
        return self.ledger_ticket_values

    async def record_mint(self, session, nft_id):
        self.events.append("record-mint")
        return True

    async def buy_and_burn(self, session):
        self.events.append("buy-and-burn")

    async def settle_headroom(self, session, minted):
        self.events.append(f"settle-headroom-{minted}")

    async def discard_assets(self, session):
        self.events.append("discard-assets")

    async def release_number(self, session):
        self.events.append("release-number")

    async def release_ticket(self, session):
        self.events.append("release-ticket")
        self.released_tickets.append(session.ticket_sequence)
        return True

    async def consume_ticket(self, session):
        self.events.append("consume-ticket")

    async def quarantine_ticket(self, session):
        self.events.append("quarantine-ticket")
        self.quarantined_tickets.append(session.ticket_sequence)

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
            ledger_tickets=self.ledger_tickets,
            record_mint=self.record_mint,
            buy_and_burn=self.buy_and_burn,
            settle_headroom=self.settle_headroom,
            discard_assets=self.discard_assets,
            release_number=self.release_number,
            release_ticket=self.release_ticket,
            consume_ticket=self.consume_ticket,
            quarantine_ticket=self.quarantine_ticket,
        )


@pytest.fixture
def action_deps():
    return FakeAtomicDeps()


def _session():
    return atomic_mint.AtomicMintSession.new(
        user_id="u1",
        wallet="rBuyer",
        platform="web",
        network="testnet",
        campaign="x-mint-link",
    )


@pytest.mark.asyncio
async def test_prepare_session_persists_canonical_batch_before_xaman(action_deps):
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    assert session.state == atomic_mint.AWAITING_SIGNATURE
    assert session.ticket_sequence == 7
    assert session.offer_id == "OFFER7"
    assert session.batch_json["RawTransactions"][0]["RawTransaction"][
        "TransactionType"
    ] == "Payment"
    assert action_deps.events.index("persist-batch") < action_deps.events.index(
        "create-xaman"
    )
    ready = session.to_dict()
    assert ready["transaction"]["TransactionType"] == "Batch"
    assert ready["wallets"]["xaman"]["deeplink"].endswith("/u1")
    assert "ticket_sequence" not in ready


@pytest.mark.asyncio
async def test_rejected_session_does_not_release_ticket_or_headroom(action_deps):
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    action_deps.status = {
        "signed": False,
        "cancelled": True,
        "expired": False,
    }
    await atomic_mint.refresh_session(session, action_deps.deps())
    assert session.state == atomic_mint.REJECTED
    assert "release-ticket" not in action_deps.events
    assert "settle-headroom-False" not in action_deps.events


@pytest.mark.asyncio
async def test_signed_batch_settles_only_after_strict_verification(action_deps):
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    action_deps.status = {
        "signed": True,
        "cancelled": False,
        "expired": False,
        "account": "rBuyer",
        "txid": "OUTER",
        "user_token": "push2",
    }
    await atomic_mint.refresh_session(session, action_deps.deps())
    assert session.state == atomic_mint.CONFIRMING
    assert session.outer_hash == "OUTER"
    assert "record-mint" not in action_deps.events

    action_deps.verified = xrpl_actions.VerifiedAtomicMint("NFT1", 500)
    await atomic_mint.refresh_session(session, action_deps.deps())
    assert session.state == atomic_mint.DONE
    assert session.nft_id == "NFT1"
    assert session.ledger_index == 500
    assert action_deps.events.index("verify-batch") < action_deps.events.index(
        "record-mint"
    )
    assert action_deps.events.index("record-mint") < action_deps.events.index(
        "consume-ticket"
    )
    done = session.to_dict()
    assert "transaction" not in done and "wallets" not in done
    assert done["inner_hashes"] == ["PAY", "MINT", "ACCEPT"]


@pytest.mark.asyncio
async def test_no_ticket_cleans_prepared_assets_and_headroom(action_deps):
    action_deps.tickets = []
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    assert session.state == atomic_mint.FAILED
    assert session.error_code == "ticket_unavailable"
    assert "discard-assets" in action_deps.events
    assert "release-number" in action_deps.events
    assert "settle-headroom-False" in action_deps.events


@pytest.mark.asyncio
async def test_ambiguous_xaman_create_failure_retains_ticket_until_lls(action_deps):
    action_deps.payload = None
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    assert session.state == atomic_mint.FAILED
    assert session.error_code == "signing_unavailable"
    assert "release-ticket" not in action_deps.events
    assert "settle-headroom-False" not in action_deps.events
    assert session.last_ledger_sequence == 100


@pytest.mark.asyncio
async def test_reconcile_releases_ticket_only_after_lls_and_ledger_presence(action_deps):
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    session.state = atomic_mint.REJECTED
    action_deps.current = 100
    await atomic_mint.reconcile_session(session, action_deps.deps())
    assert action_deps.released_tickets == []
    action_deps.current = 101
    await atomic_mint.reconcile_session(session, action_deps.deps())
    assert action_deps.released_tickets == [7]
    assert session.ticket_sequence is None
    assert "settle-headroom-False" in action_deps.events


@pytest.mark.asyncio
async def test_reconcile_quarantines_consumed_ticket_without_verified_batch(action_deps):
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    session.state = atomic_mint.CONFIRMING
    session.outer_hash = "OUTER"
    action_deps.current = 101
    action_deps.ledger_ticket_values = []
    await atomic_mint.reconcile_session(session, action_deps.deps())
    assert session.state == atomic_mint.INDETERMINATE
    assert action_deps.quarantined_tickets == [7]


@pytest.mark.asyncio
async def test_reconcile_quarantines_after_outer_lookup_failure(action_deps):
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    session.state = atomic_mint.CONFIRMING
    session.outer_hash = "OUTER"
    action_deps.current = 101
    action_deps.verify_error = RuntimeError("RPC unavailable")
    await atomic_mint.reconcile_session(session, action_deps.deps())
    assert session.state == atomic_mint.INDETERMINATE
    assert session.error_code == "outcome_indeterminate"
    assert action_deps.quarantined_tickets == [7]


@pytest.mark.asyncio
async def test_reconcile_validated_batch_resumes_settlement(action_deps):
    session = _session()
    await atomic_mint.prepare_session(session, action_deps.deps())
    session.state = atomic_mint.CONFIRMING
    session.outer_hash = "OUTER"
    action_deps.verified = xrpl_actions.VerifiedAtomicMint("NFT1", 500)
    await atomic_mint.reconcile_session(session, action_deps.deps())
    assert session.state == atomic_mint.DONE
    assert session.nft_id == "NFT1"
    assert "record-mint" in action_deps.events
    assert "consume-ticket" in action_deps.events
