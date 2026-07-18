# XRPL Actions Payment-First Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a dark-launched XRPL Action that prepares an LFG NFT and lets an authenticated buyer approve `Payment -> NFTokenMint -> NFTokenAcceptOffer` as one `ALLORNOTHING` `BatchV1_1` transaction in Xaman.

**Architecture:** A focused `xrpl_actions` protocol module owns amendment gating, offer-key derivation, Batch construction, validation, and issuer signing. A durable action store leases issuer Tickets and persists restart-safe sessions; an atomic-mint orchestrator reuses refactored mint asset preparation and post-validation settlement. aiohttp exposes the draft Actions API, while the existing vanilla-JS PWA deep-links into a one-approval flow. The legacy mint path remains the default whenever the exact amendment/capability gate is closed.

**Tech Stack:** Python 3.10+, aiohttp, SQLite, xrpl-py 5.0+, Xaman Platform API, pytest + pytest-asyncio + pytest-aiohttp, vanilla ES modules, Node-backed JS tests, GitHub Pages.

## Global Constraints

- The inner transaction order is exactly `Payment`, `NFTokenMint`, `NFTokenAcceptOffer`.
- The outer Batch flag is exactly `tfAllOrNothing` (`65536`); every inner transaction includes only its NFT flags plus `tfInnerBatchTxn` (`1073741824`).
- The mint-created offer has `Amount: "0"` and `Destination` equal to the authenticated buyer; there is no second payment in the accept leg.
- The buyer is the outer Batch account and signs once in Xaman; `config.SIGNING_ACCOUNT` supplies the sole `BatchSigner`.
- The issuer mint uses `Sequence: 0` plus a durably leased `TicketSequence`; never hold an interactive issuer sequence open.
- Require the exact `BatchV1_1` ID `9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377` and `NFTokenMintOffer`; hard-deny obsolete Batch ID `894646DD5284E97DECFE6674A6D6152686791C4A95F8C132CCA9BAF9E5812FB6`.
- The environment switch `XRPL_ACTIONS_BATCH_ENABLED` defaults false. Configuration can close the gate but cannot force it open when the connected ledger lacks either amendment.
- Every outer and inner ledger transaction carries SourceTag `2606160021` and existing bounded provenance memos.
- Never infer success from Xaman `signed` or outer `tesSUCCESS`; validate all three fixed inner hashes in the same ledger with the expected `ParentBatchID` and `tesSUCCESS` results.
- Never reuse a leased Ticket before definitive success/expiry reconciliation. Indeterminate outcomes quarantine the Ticket.
- `xrpl-py>=5.0.0`; do not add another XRPL serialization or crypto dependency.
- Preserve all Discord, Telegram, bulk mint, marketplace, and legacy mint behavior with the feature flag off.
- All behavior changes follow a witnessed RED -> GREEN test cycle and each task ends in a focused commit.

---

## File map

- Create `lfg_core/xrpl_actions.py`: ledger constants, capability evaluation, Ticket discovery, deterministic NFT-offer ID, canonical Batch builder, invariant validator, regular-key-aware issuer signature, and validated-result reconciliation.
- Create `lfg_core/action_store.py`: SQLite action sessions and issuer Ticket lease lifecycle.
- Create `lfg_core/atomic_mint.py`: restart-safe atomic mint session model and preparation/confirmation orchestration.
- Modify `lfg_core/mint_flow.py`: extract reusable off-ledger asset preparation and post-validation recording while leaving the legacy flow behavior intact.
- Modify `lfg_core/xumm_ops.py`: enforced-signer Batch payload creation and richer rejection/expiry status.
- Modify `lfg_core/config.py`: dark-launch, expiry, rate, and ticket-pool settings.
- Modify `lfg_service/app.py`: Actions discovery, metadata, create/status/active endpoints, background task wiring, and session ownership/rate controls.
- Create `webapp/client/action_pure.js`: pure route/state decisions for Node tests.
- Modify `webapp/client/app.js` and `webapp/client/style.css`: action deep-link, preparation, one Batch approval, confirmation, retry, and completion UI.
- Create `webapp/client/.well-known/xrpl-actions.json`: local/default discovery mapping.
- Modify `.github/workflows/pages.yml`: rewrite the discovery API base in the Pages artifact.
- Create `scripts/provision_batch_tickets.py`: explicit network-checked issuer Ticket status/provisioning CLI.
- Create `docs/xls/xrpl-actions.md`: implementation-independent draft XLS.
- Modify `requirements.txt`, `README.md`, `CLAUDE.md`, and `docs/ops/env.staging.example`: dependency floor and operational contract.
- Create tests `tests/test_xrpl_actions.py`, `tests/test_action_store.py`, `tests/test_atomic_mint.py`, `tests/test_xumm_batch.py`, `tests/test_actions_service.py`, `tests/test_action_pure_js.py`, and `tests/test_batch_ticket_cli.py`.

---

### Task 1: Runtime floor and dark-launch configuration

**Files:**
- Modify: `requirements.txt`
- Modify: `lfg_core/config.py:89-115,240-260`
- Modify: `conftest.py:1-35`
- Create: `tests/test_xrpl_actions_config.py`

**Interfaces:**
- Consumes: existing environment parsing in `lfg_core.config`.
- Produces: `XRPL_ACTIONS_BATCH_ENABLED: bool`, `XRPL_ACTIONS_LAST_LEDGER_OFFSET: int`, `XRPL_ACTIONS_TICKET_TARGET: int`, `XRPL_ACTIONS_CREATE_LIMIT: int`, an installed `xrpl-py>=5.0.0` API, and the async/aiohttp pytest fixtures used by the new orchestration tests.

- [ ] **Step 1: Write failing configuration and dependency tests**

```python
# tests/test_xrpl_actions_config.py
from pathlib import Path

from lfg_core import config


def test_batch_actions_are_dark_by_default():
    assert config.XRPL_ACTIONS_BATCH_ENABLED is False


def test_batch_action_limits_are_positive():
    assert config.XRPL_ACTIONS_LAST_LEDGER_OFFSET > 0
    assert config.XRPL_ACTIONS_TICKET_TARGET > 0
    assert config.XRPL_ACTIONS_CREATE_LIMIT > 0


def test_xrpl_py_floor_is_v5():
    requirements = Path("requirements.txt").read_text()
    assert "xrpl-py>=5.0.0" in requirements


def test_async_test_plugin_is_declared():
    requirements = Path("requirements.txt").read_text()
    assert "pytest-asyncio>=0.23" in requirements
    assert "pytest-aiohttp>=1.0" in requirements
```

- [ ] **Step 2: Run the focused tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_xrpl_actions_config.py -q`

Expected: FAIL because the four config attributes do not exist, `requirements.txt` contains unversioned `xrpl-py`, and the async test plugin is undeclared.

- [ ] **Step 3: Add exact defaults and dependency floor**

Change `requirements.txt` from `xrpl-py` to `xrpl-py>=5.0.0` and add
`pytest-asyncio>=0.23` plus `pytest-aiohttp>=1.0` beside `pytest`. Add this
block beside the payment timeout settings in `lfg_core/config.py`:

```python
# XRPL Actions / corrected BatchV1_1 path. Dark until both this switch and
# the connected ledger's exact amendment checks pass.
XRPL_ACTIONS_BATCH_ENABLED = os.getenv("XRPL_ACTIONS_BATCH_ENABLED", "0") not in (
    "",
    "0",
    "false",
    "False",
)
XRPL_ACTIONS_LAST_LEDGER_OFFSET = int(os.getenv("XRPL_ACTIONS_LAST_LEDGER_OFFSET", "90"))
XRPL_ACTIONS_TICKET_TARGET = int(os.getenv("XRPL_ACTIONS_TICKET_TARGET", "16"))
XRPL_ACTIONS_CREATE_LIMIT = int(os.getenv("XRPL_ACTIONS_CREATE_LIMIT", "3"))
for _name in (
    "XRPL_ACTIONS_LAST_LEDGER_OFFSET",
    "XRPL_ACTIONS_TICKET_TARGET",
    "XRPL_ACTIONS_CREATE_LIMIT",
):
    if globals()[_name] <= 0:
        raise ValueError(f"{_name} must be greater than 0")
```

Add `os.environ.setdefault("XRPL_ACTIONS_BATCH_ENABLED", "0")` to `conftest.py` so a developer shell cannot accidentally turn the default suite into live-ledger tests.

- [ ] **Step 4: Run focused and config regression tests**

Run: `.venv/bin/python -m pytest tests/test_xrpl_actions_config.py tests/test_web_surface_config.py tests/test_config_economy_validate.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt lfg_core/config.py conftest.py tests/test_xrpl_actions_config.py
git commit -m "build: configure dark-launched XRPL Batch actions"
```

---

### Task 2: Capability gate, Ticket discovery, and deterministic offer keys

**Files:**
- Create: `lfg_core/xrpl_actions.py`
- Create: `tests/test_xrpl_actions.py`

**Interfaces:**
- Consumes: `config.JSON_RPC_URL`, `config.SIGNING_ACCOUNT`, xrpl-py `Feature`, `AccountObjects`, and classic-address decoding.
- Produces: `BatchCapability`, `evaluate_capabilities()`, `fetch_batch_capability()`, `list_ticket_sequences()`, and `nft_offer_id()`.

- [ ] **Step 1: Write failing offer-key tests with an independent reference formula**

```python
# tests/test_xrpl_actions.py
import hashlib

import pytest
from xrpl.core.addresscodec import decode_classic_address

from lfg_core import xrpl_actions

ACCOUNT = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"


def _reference_offer_id(account: str, sequence: int) -> str:
    payload = b"\x00q" + decode_classic_address(account) + sequence.to_bytes(4, "big")
    return hashlib.sha512(payload).digest()[:32].hex().upper()


def test_nft_offer_id_matches_protocol_keylet():
    assert xrpl_actions.nft_offer_id(ACCOUNT, 349) == _reference_offer_id(ACCOUNT, 349)


@pytest.mark.parametrize("sequence", [-1, 2**32, True])
def test_nft_offer_id_rejects_non_uint32(sequence):
    with pytest.raises(ValueError):
        xrpl_actions.nft_offer_id(ACCOUNT, sequence)
```

- [ ] **Step 2: Run the offer-key tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_xrpl_actions.py -q`

Expected: collection ERROR because `lfg_core.xrpl_actions` does not exist.

- [ ] **Step 3: Implement constants and offer-key derivation**

```python
# lfg_core/xrpl_actions.py
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from xrpl.clients import JsonRpcClient
from xrpl.core.addresscodec import decode_classic_address
from xrpl.models.requests import AccountObjects, AccountObjectType, Feature

BATCH_V1_1_ID = "9F287AED3CDB50A7BD1ACEC24296A30C9B5230CCD136219317AC790E3B884377"
NFTOKEN_MINT_OFFER_ID = "EE3CF852F0506782D05E65D49E5DCC3D16D50898CD1B646BAE274863401CC3CE"
OBSOLETE_BATCH_ID = "894646DD5284E97DECFE6674A6D6152686791C4A95F8C132CCA9BAF9E5812FB6"
NFTOKEN_OFFER_NAMESPACE = 0x0071


def nft_offer_id(account: str, sequence_or_ticket: int) -> str:
    if isinstance(sequence_or_ticket, bool) or not 0 <= sequence_or_ticket <= 0xFFFFFFFF:
        raise ValueError("sequence_or_ticket must be a uint32")
    account_id = decode_classic_address(account)
    payload = (
        NFTOKEN_OFFER_NAMESPACE.to_bytes(2, "big")
        + account_id
        + sequence_or_ticket.to_bytes(4, "big")
    )
    return hashlib.sha512(payload).digest()[:32].hex().upper()
```

- [ ] **Step 4: Add failing capability matrix tests**

```python
@pytest.mark.parametrize(
    ("batch", "mint_offer", "enabled", "reason"),
    [
        (True, True, True, None),
        (False, True, False, "batch_unavailable"),
        (True, False, False, "mint_offer_unavailable"),
    ],
)
def test_capability_requires_both_exact_amendments(batch, mint_offer, enabled, reason):
    rows = {
        xrpl_actions.BATCH_V1_1_ID: {"supported": True, "enabled": batch},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {"supported": True, "enabled": mint_offer},
    }
    got = xrpl_actions.evaluate_capabilities(rows, configured=True)
    assert (got.enabled, got.reason) == (enabled, reason)


def test_obsolete_batch_never_substitutes_for_v1_1():
    rows = {
        xrpl_actions.OBSOLETE_BATCH_ID: {"supported": True, "enabled": True},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {"supported": True, "enabled": True},
    }
    got = xrpl_actions.evaluate_capabilities(rows, configured=True)
    assert got.enabled is False
    assert got.reason == "batch_unavailable"


def test_obsolete_batch_enabled_hard_closes_even_if_v1_1_is_enabled():
    rows = {
        xrpl_actions.BATCH_V1_1_ID: {"supported": True, "enabled": True},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {"supported": True, "enabled": True},
        xrpl_actions.OBSOLETE_BATCH_ID: {"supported": True, "enabled": True},
    }
    got = xrpl_actions.evaluate_capabilities(rows, configured=True)
    assert got == xrpl_actions.BatchCapability(False, "obsolete_batch_enabled")


def test_config_switch_can_only_close_gate():
    rows = {
        xrpl_actions.BATCH_V1_1_ID: {"supported": True, "enabled": True},
        xrpl_actions.NFTOKEN_MINT_OFFER_ID: {"supported": True, "enabled": True},
    }
    assert xrpl_actions.evaluate_capabilities(rows, configured=False).reason == "action_disabled"


@pytest.mark.asyncio
async def test_feature_rpc_reads_row_keyed_by_amendment_id():
    class FakeClient:
        def request(self, request):
            amendment_id = request.feature
            return type("Response", (), {"result": {
                amendment_id: {
                    "supported": True,
                    "enabled": amendment_id != xrpl_actions.OBSOLETE_BATCH_ID,
                }
            }})()

    got = await xrpl_actions.fetch_batch_capability(FakeClient(), configured=True)
    assert got == xrpl_actions.BatchCapability(True, None)


@pytest.mark.asyncio
async def test_feature_rpc_fails_closed_on_malformed_response():
    class FakeClient:
        def request(self, request):
            return type("Response", (), {"result": {"enabled": True}})()

    got = await xrpl_actions.fetch_batch_capability(FakeClient(), configured=True)
    assert got.enabled is False
    assert got.reason == "batch_unavailable"
```

- [ ] **Step 5: Implement fail-closed capability evaluation and RPC fetch**

```python
@dataclass(frozen=True)
class BatchCapability:
    enabled: bool
    reason: str | None


def evaluate_capabilities(
    rows: Mapping[str, Mapping[str, Any]], *, configured: bool
) -> BatchCapability:
    if not configured:
        return BatchCapability(False, "action_disabled")
    obsolete = rows.get(OBSOLETE_BATCH_ID, {})
    if obsolete.get("enabled"):
        return BatchCapability(False, "obsolete_batch_enabled")
    batch = rows.get(BATCH_V1_1_ID, {})
    mint_offer = rows.get(NFTOKEN_MINT_OFFER_ID, {})
    if not batch.get("supported") or not batch.get("enabled"):
        return BatchCapability(False, "batch_unavailable")
    if not mint_offer.get("supported") or not mint_offer.get("enabled"):
        return BatchCapability(False, "mint_offer_unavailable")
    return BatchCapability(True, None)


async def fetch_batch_capability(
    client: JsonRpcClient, *, configured: bool
) -> BatchCapability:
    rows: dict[str, Mapping[str, Any]] = {}
    for amendment_id in (
        BATCH_V1_1_ID,
        NFTOKEN_MINT_OFFER_ID,
        OBSOLETE_BATCH_ID,
    ):
        unavailable = (
            "batch_unavailable"
            if amendment_id in (BATCH_V1_1_ID, OBSOLETE_BATCH_ID)
            else "mint_offer_unavailable"
        )
        try:
            response = await asyncio.to_thread(
                client.request, Feature(feature=amendment_id)
            )
        except Exception:
            return BatchCapability(False, unavailable)
        result = response.result if isinstance(response.result, dict) else {}
        row = result.get(amendment_id)
        if not isinstance(row, dict):
            return BatchCapability(False, unavailable)
        rows[amendment_id] = row
    return evaluate_capabilities(rows, configured=configured)


async def list_ticket_sequences(client: JsonRpcClient, account: str) -> list[int]:
    response = await asyncio.to_thread(
        client.request,
        AccountObjects(
            account=account,
            type=AccountObjectType.TICKET,
            ledger_index="validated",
        ),
    )
    objects = response.result.get("account_objects", [])
    return sorted(
        obj["TicketSequence"]
        for obj in objects
        if obj.get("LedgerEntryType") == "Ticket" and isinstance(obj.get("TicketSequence"), int)
    )
```

- [ ] **Step 6: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_xrpl_actions.py -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add lfg_core/xrpl_actions.py tests/test_xrpl_actions.py
git commit -m "feat: gate corrected Batch and derive NFT offer keys"
```

---

### Task 3: Canonical three-leg Batch builder, invariant guard, and issuer signature

**Files:**
- Modify: `lfg_core/xrpl_actions.py`
- Modify: `tests/test_xrpl_actions.py`

**Interfaces:**
- Consumes: `nft_offer_id()`, existing memo builders, `config.SIGNING_ACCOUNT`, `config.SWAP_ISSUER_ADDRESS`, `config.NFT_*`, xrpl-py `Batch`, `Payment`, `NFTokenMint`, `NFTokenAcceptOffer`, `autofill`, and `encode_for_signing_batch`.
- Produces: `MintPayment`, `PreparedBatch`, `build_atomic_mint_batch()`, `validate_atomic_mint_batch()`, `sign_issuer_batch()`, and `prepare_atomic_mint_batch()`.

- [ ] **Step 1: Write failing exact-shape tests for LFGO and XRP**

```python
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.transactions import BatchFlag, TransactionFlag


def _payment(pay_with="LFGO"):
    if pay_with == "XRP":
        return xrpl_actions.MintPayment("XRP", "10", "rDest", "10000000")
    amount = IssuedCurrencyAmount(currency="4C46474F00000000000000000000000000000000", issuer=ACCOUNT, value="1")
    return xrpl_actions.MintPayment("LFGO", "1", ACCOUNT, amount)


def test_builder_orders_payment_mint_accept_and_charges_once():
    batch = xrpl_actions.build_atomic_mint_batch(
        buyer=ACCOUNT,
        issuer_account="rLs1MzkFWCxTbuAHgjeTZK4fcCDDnf2KRv",
        nft_issuer="rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ",
        issuer_ticket=9001,
        metadata_url="https://cdn.example/7.json",
        payment=_payment(),
        platform="webapp",
        campaign="x-mint-link",
        nft_flags=9,
        nft_taxon=0,
        transfer_fee=7000,
        source_tag=2606160021,
    )
    assert batch.flags == BatchFlag.TF_ALL_OR_NOTHING
    assert [tx.transaction_type.value for tx in batch.raw_transactions] == [
        "Payment", "NFTokenMint", "NFTokenAcceptOffer"
    ]
    payment, mint, accept = batch.raw_transactions
    assert payment.account == ACCOUNT
    assert mint.sequence == 0 and mint.ticket_sequence == 9001
    assert mint.issuer == "rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ"
    assert mint.amount == "0" and mint.destination == ACCOUNT
    assert accept.nftoken_sell_offer == xrpl_actions.nft_offer_id(mint.account, 9001)
    assert all(tx.fee is None or tx.fee == "0" for tx in batch.raw_transactions)
    assert mint.has_flag(TransactionFlag.TF_INNER_BATCH_TXN)


def test_xrp_builder_keeps_payment_first_and_offer_free():
    batch = _build_test_batch(payment=_payment("XRP"))
    assert batch.raw_transactions[0].amount == "10000000"
    assert batch.raw_transactions[1].amount == "0"
```

In the test file, `_build_test_batch(**overrides)` supplies the same explicit values as the first test and merges only the named overrides.

- [ ] **Step 2: Run the builder tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_xrpl_actions.py -q`

Expected: FAIL because `MintPayment` and the Batch builder do not exist.

- [ ] **Step 3: Implement the minimal typed builder**

```python
from dataclasses import replace
from typing import Literal

from xrpl.core import keypairs
from xrpl.core.binarycodec import encode_for_signing_batch
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.transactions import (
    Batch,
    BatchFlag,
    NFTokenAcceptOffer,
    NFTokenMint,
    Payment,
    TransactionFlag,
)
from xrpl.models.transactions.batch import BatchSigner
from xrpl.wallet import Wallet

from lfg_core import memos


@dataclass(frozen=True)
class MintPayment:
    pay_with: Literal["LFGO", "XRP"]
    display_amount: str
    destination: str
    amount: str | IssuedCurrencyAmount


@dataclass(frozen=True)
class PreparedBatch:
    transaction: Batch
    offer_id: str
    inner_hashes: tuple[str, str, str]
    last_ledger_sequence: int


class AtomicMintInvariantError(ValueError):
    pass


def build_atomic_mint_batch(
    *, buyer: str, issuer_account: str, nft_issuer: str, issuer_ticket: int,
    metadata_url: str, payment: MintPayment, platform: str,
    campaign: str | None, nft_flags: int, nft_taxon: int,
    transfer_fee: int, source_tag: int
) -> Batch:
    user_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_PAYMENT, campaign
    )
    mint_memos = memos.build_memo_models(
        memos.INITIATOR_BACKEND, platform, memos.ACTION_MINT, campaign
    )
    accept_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_ACCEPT_OFFER, campaign
    )
    offer_id = nft_offer_id(issuer_account, issuer_ticket)
    mint_kwargs: dict[str, Any] = {
        "account": issuer_account,
        "sequence": 0,
        "ticket_sequence": issuer_ticket,
        "uri": metadata_url.encode().hex().upper(),
        "nftoken_taxon": nft_taxon,
        "flags": nft_flags | TransactionFlag.TF_INNER_BATCH_TXN,
        "amount": "0",
        "destination": buyer,
        "source_tag": source_tag,
        "memos": mint_memos,
    }
    if nft_flags & 0x0008:
        mint_kwargs["transfer_fee"] = transfer_fee
    if nft_issuer != issuer_account:
        mint_kwargs["issuer"] = nft_issuer
    return Batch(
        account=buyer,
        flags=BatchFlag.TF_ALL_OR_NOTHING,
        source_tag=source_tag,
        memos=memos.build_memo_models(
            memos.INITIATOR_USER, platform, memos.ACTION_MINT, campaign
        ),
        raw_transactions=[
            Payment(
                account=buyer, destination=payment.destination, amount=payment.amount,
                flags=TransactionFlag.TF_INNER_BATCH_TXN,
                source_tag=source_tag, memos=user_memos
            ),
            NFTokenMint(**mint_kwargs),
            NFTokenAcceptOffer(
                account=buyer, nftoken_sell_offer=offer_id,
                flags=TransactionFlag.TF_INNER_BATCH_TXN,
                source_tag=source_tag, memos=accept_memos
            ),
        ],
    )
```

- [ ] **Step 4: Add failing mutation and regular-key signature tests**

```python
from dataclasses import replace

from xrpl.core.binarycodec import encode_for_signing_batch
from xrpl.core.keypairs import is_valid_message
from xrpl.models.transactions import Batch
from xrpl.wallet import Wallet


def test_validator_rejects_non_payment_first():
    batch = _build_test_batch()
    txs = list(batch.raw_transactions)
    txs[0], txs[1] = txs[1], txs[0]
    mutated = replace(batch, raw_transactions=txs)
    with pytest.raises(xrpl_actions.AtomicMintInvariantError):
        xrpl_actions.validate_atomic_mint_batch(
            mutated, buyer=ACCOUNT,
            issuer_account="rLs1MzkFWCxTbuAHgjeTZK4fcCDDnf2KRv",
            nft_issuer="rLfgoMintj3KBcs4s2XKtquvDwEte2kYfJ",
            issuer_ticket=9001, payment=_payment(),
            metadata_url="https://cdn.example/7.json", platform="webapp",
            campaign="x-mint-link", nft_flags=9, nft_taxon=0,
            transfer_fee=7000, source_tag=2606160021,
        )


def test_regular_key_signature_names_authorizing_issuer_not_seed_address():
    wallet = Wallet.from_seed("sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
    issuer = "rLs1MzkFWCxTbuAHgjeTZK4fcCDDnf2KRv"
    batch = _autofilled_test_batch(issuer_account=issuer)
    signed = xrpl_actions.sign_issuer_batch(batch, wallet=wallet, issuer_account=issuer)
    signer = signed.batch_signers[0]
    message = encode_for_signing_batch({
        "flags": int(signed.flags),
        "transaction_ids": [tx.get_hash() for tx in signed.raw_transactions],
    })
    assert signer.account == issuer
    assert is_valid_message(message, bytes.fromhex(signer.txn_signature), signer.signing_pub_key)
```

- [ ] **Step 5: Implement strict validation, signing, and async autofill**

```python
from xrpl.asyncio.transaction import autofill
from xrpl.clients import Client


def validate_atomic_mint_batch(
    batch: Batch, *, buyer: str, issuer_account: str, nft_issuer: str,
    issuer_ticket: int, payment: MintPayment, metadata_url: str,
    platform: str, campaign: str | None, nft_flags: int, nft_taxon: int,
    transfer_fee: int, source_tag: int
) -> None:
    expected_outer_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_MINT, campaign
    )
    expected_payment_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_PAYMENT, campaign
    )
    expected_mint_memos = memos.build_memo_models(
        memos.INITIATOR_BACKEND, platform, memos.ACTION_MINT, campaign
    )
    expected_accept_memos = memos.build_memo_models(
        memos.INITIATOR_USER, platform, memos.ACTION_ACCEPT_OFFER, campaign
    )
    if int(batch.flags or 0) != int(BatchFlag.TF_ALL_OR_NOTHING):
        raise AtomicMintInvariantError("Batch must be ALLORNOTHING")
    if (
        batch.account != buyer
        or batch.source_tag != source_tag
        or batch.memos != expected_outer_memos
        or len(batch.raw_transactions) != 3
    ):
        raise AtomicMintInvariantError("outer Batch mismatch")
    pay, mint, accept = batch.raw_transactions
    if not isinstance(pay, Payment) or not isinstance(mint, NFTokenMint) or not isinstance(accept, NFTokenAcceptOffer):
        raise AtomicMintInvariantError("wrong inner order")
    inner = int(TransactionFlag.TF_INNER_BATCH_TXN)
    if (
        pay.account != buyer
        or pay.destination != payment.destination
        or pay.amount != payment.amount
        or int(pay.flags or 0) != inner
        or pay.source_tag != source_tag
        or pay.memos != expected_payment_memos
    ):
        raise AtomicMintInvariantError("payment mismatch")
    if (
        mint.account != issuer_account
        or mint.sequence != 0
        or mint.ticket_sequence != issuer_ticket
        or int(mint.flags or 0) != (nft_flags | inner)
        or mint.nftoken_taxon != nft_taxon
        or mint.issuer != (nft_issuer if nft_issuer != issuer_account else None)
        or mint.source_tag != source_tag
        or mint.memos != expected_mint_memos
    ):
        raise AtomicMintInvariantError("issuer ticket mismatch")
    if mint.amount != "0" or mint.destination != buyer:
        raise AtomicMintInvariantError("mint offer must be free and buyer-locked")
    if mint.uri != metadata_url.encode().hex().upper():
        raise AtomicMintInvariantError("metadata URI mismatch")
    expected_fee = transfer_fee if nft_flags & 0x0008 else None
    if mint.transfer_fee != expected_fee:
        raise AtomicMintInvariantError("transfer fee mismatch")
    if (
        accept.account != buyer
        or accept.nftoken_sell_offer != nft_offer_id(issuer_account, issuer_ticket)
        or int(accept.flags or 0) != inner
        or accept.source_tag != source_tag
        or accept.memos != expected_accept_memos
    ):
        raise AtomicMintInvariantError("accept offer mismatch")
    if batch.sequence is None or pay.sequence != batch.sequence + 1 or accept.sequence != batch.sequence + 2:
        raise AtomicMintInvariantError("buyer sequence allocation mismatch")
    for tx in batch.raw_transactions:
        if tx.fee != "0" or tx.signing_pub_key != "" or tx.txn_signature is not None or tx.signers is not None:
            raise AtomicMintInvariantError("inner signing fields invalid")


def sign_issuer_batch(batch: Batch, *, wallet: Wallet, issuer_account: str) -> Batch:
    if issuer_account not in {tx.account for tx in batch.raw_transactions}:
        raise AtomicMintInvariantError("issuer is not an inner account")
    fields = {
        "flags": int(batch.flags or 0),
        "transaction_ids": [tx.get_hash() for tx in batch.raw_transactions],
    }
    signature = keypairs.sign(encode_for_signing_batch(fields), wallet.private_key)
    signer = BatchSigner(
        account=issuer_account,
        signing_pub_key=wallet.public_key,
        txn_signature=signature,
    )
    return replace(batch, batch_signers=[signer])


async def prepare_atomic_mint_batch(
    *, client: Client, wallet: Wallet, buyer: str, issuer_account: str,
    nft_issuer: str, issuer_ticket: int, metadata_url: str,
    payment: MintPayment, platform: str, campaign: str | None,
    nft_flags: int, nft_taxon: int, transfer_fee: int, source_tag: int
) -> PreparedBatch:
    draft = build_atomic_mint_batch(
        buyer=buyer, issuer_account=issuer_account, nft_issuer=nft_issuer,
        issuer_ticket=issuer_ticket, metadata_url=metadata_url,
        payment=payment, platform=platform,
        campaign=campaign, nft_flags=nft_flags, nft_taxon=nft_taxon,
        transfer_fee=transfer_fee, source_tag=source_tag
    )
    filled = await autofill(draft, client, signers_count=1)
    validate_atomic_mint_batch(
        filled, buyer=buyer, issuer_account=issuer_account,
        nft_issuer=nft_issuer, issuer_ticket=issuer_ticket, payment=payment,
        metadata_url=metadata_url, platform=platform, campaign=campaign,
        nft_flags=nft_flags, nft_taxon=nft_taxon,
        transfer_fee=transfer_fee, source_tag=source_tag,
    )
    signed = sign_issuer_batch(filled, wallet=wallet, issuer_account=issuer_account)
    inner_hashes = tuple(tx.get_hash() for tx in signed.raw_transactions)
    return PreparedBatch(signed, nft_offer_id(issuer_account, issuer_ticket), inner_hashes, int(signed.last_ledger_sequence))
```

- [ ] **Step 6: Run protocol tests**

Run: `.venv/bin/python -m pytest tests/test_xrpl_actions.py -q`

Expected: all tests PASS, including signature verification and mutation rejection.

- [ ] **Step 7: Commit**

```bash
git add lfg_core/xrpl_actions.py tests/test_xrpl_actions.py
git commit -m "feat: build and sign payment-first atomic mint batches"
```

---

### Task 4: Durable action sessions and Ticket leases

**Files:**
- Create: `lfg_core/action_store.py`
- Create: `tests/test_action_store.py`

**Interfaces:**
- Consumes: a caller-owned SQLite connection and ledger-discovered Ticket sequence integers.
- Produces: `ensure_schema()`, `create_session()`, `update_session()`, `get_session()`, `list_reconcilable_sessions()`, `lease_ticket()`, `leased_ticket_sequences()`, `mark_ticket()`, and `release_ticket()`.

- [ ] **Step 1: Write failing session round-trip and concurrent lease tests**

```python
# tests/test_action_store.py
import sqlite3

from lfg_core import action_store


def _conn(tmp_path):
    return sqlite3.connect(tmp_path / "actions.db")


def test_session_round_trip(tmp_path):
    conn = _conn(tmp_path)
    action_store.create_session(
        conn, session_id="s1", account="rBuyer", user_id="u1", platform="web",
        network="testnet", state="preparing", created_ts=10
    )
    action_store.update_session(conn, "s1", now_ts=11, state="awaiting_signature", ticket_sequence=7)
    row = action_store.get_session(conn, "s1")
    assert row["state"] == "awaiting_signature"
    assert row["ticket_sequence"] == 7


def test_ticket_lease_is_unique_across_connections(tmp_path):
    path = tmp_path / "actions.db"
    c1, c2 = sqlite3.connect(path), sqlite3.connect(path)
    assert action_store.lease_ticket(c1, "testnet", "rIssuer", [7], "s1", 10) == 7
    assert action_store.lease_ticket(c2, "testnet", "rIssuer", [7], "s2", 10) is None


def test_quarantined_ticket_cannot_be_released_normally(tmp_path):
    conn = _conn(tmp_path)
    action_store.lease_ticket(conn, "testnet", "rIssuer", [7], "s1", 10)
    action_store.mark_ticket(conn, "testnet", "rIssuer", 7, state="quarantined")
    assert action_store.release_ticket(conn, "testnet", "rIssuer", 7) is False


def test_leased_ticket_sequences_lists_every_live_lease(tmp_path):
    conn = _conn(tmp_path)
    action_store.lease_ticket(conn, "testnet", "rIssuer", [7], "s1", 10)
    action_store.lease_ticket(conn, "testnet", "rIssuer", [8], "s2", 10)
    assert action_store.leased_ticket_sequences(
        conn, "testnet", "rIssuer"
    ) == {7, 8}
```

- [ ] **Step 2: Run store tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_action_store.py -q`

Expected: collection ERROR because `action_store` does not exist.

- [ ] **Step 3: Implement the schema and atomic lease operations**

```python
# lfg_core/action_store.py
from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

SESSION_STATES = frozenset({
    "preparing", "awaiting_signature", "confirming", "done", "rejected",
    "expired", "failed", "indeterminate"
})
TICKET_STATES = frozenset({"leased", "consumed", "quarantined"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS xrpl_action_sessions (
  session_id TEXT PRIMARY KEY, account TEXT NOT NULL, user_id TEXT NOT NULL,
  platform TEXT NOT NULL, network TEXT NOT NULL, state TEXT NOT NULL,
  campaign TEXT, pay_with TEXT, pay_amount TEXT, payment_json TEXT, nft_number INTEGER,
  metadata_url TEXT, image_url TEXT, video_url TEXT, traits_json TEXT,
  body_type TEXT, ticket_sequence INTEGER, offer_id TEXT, batch_json TEXT,
  outer_hash TEXT, inner_hashes_json TEXT, xumm_uuid TEXT,
  xumm_url TEXT, qr_url TEXT,
  last_ledger_sequence INTEGER, ledger_index INTEGER, nft_id TEXT, error_code TEXT,
  created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS xrpl_action_ticket_leases (
  network TEXT NOT NULL, account TEXT NOT NULL, ticket_sequence INTEGER NOT NULL,
  session_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL, leased_at INTEGER NOT NULL,
  last_ledger_sequence INTEGER, outer_hash TEXT,
  PRIMARY KEY(network, account, ticket_sequence)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def create_session(conn: sqlite3.Connection, *, session_id: str, account: str,
                   user_id: str, platform: str, network: str, state: str,
                   created_ts: int, campaign: str | None = None) -> None:
    if state not in SESSION_STATES:
        raise ValueError(f"unknown action state: {state}")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO xrpl_action_sessions"
        " (session_id,account,user_id,platform,network,state,campaign,created_ts,updated_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            session_id, account, user_id, platform, network, state,
            campaign, created_ts, created_ts,
        ),
    )
    conn.commit()


def update_session(conn: sqlite3.Connection, session_id: str, *, now_ts: int, **changes: Any) -> None:
    allowed = {
        "state", "campaign", "pay_with", "pay_amount", "payment_json", "nft_number",
        "metadata_url", "image_url", "video_url", "traits_json", "body_type",
        "ticket_sequence", "offer_id", "batch_json", "outer_hash",
        "inner_hashes_json", "xumm_uuid", "xumm_url", "qr_url",
        "last_ledger_sequence",
        "ledger_index", "nft_id", "error_code"
    }
    if not changes.keys() <= allowed:
        raise ValueError("unsupported action session column")
    if "state" in changes and changes["state"] not in SESSION_STATES:
        raise ValueError(f"unknown action state: {changes['state']}")
    sets = ["updated_ts=?"] + [f"{key}=?" for key in changes]
    values = [now_ts] + [json.dumps(value) if key.endswith("_json") and not isinstance(value, str) else value for key, value in changes.items()]
    conn.execute(f"UPDATE xrpl_action_sessions SET {', '.join(sets)} WHERE session_id=?", (*values, session_id))
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM xrpl_action_sessions WHERE session_id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def lease_ticket(conn: sqlite3.Connection, network: str, account: str,
                 available: Sequence[int], session_id: str, now_ts: int) -> int | None:
    ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    used = {row[0] for row in conn.execute(
        "SELECT ticket_sequence FROM xrpl_action_ticket_leases WHERE network=? AND account=?",
        (network, account),
    )}
    ticket = next((value for value in sorted(available) if value not in used), None)
    if ticket is not None:
        conn.execute(
            "INSERT INTO xrpl_action_ticket_leases (network,account,ticket_sequence,session_id,state,leased_at) VALUES (?,?,?,?, 'leased', ?)",
            (network, account, ticket, session_id, now_ts),
        )
    conn.commit()
    return ticket


def leased_ticket_sequences(
    conn: sqlite3.Connection, network: str, account: str
) -> set[int]:
    ensure_schema(conn)
    return {
        row[0]
        for row in conn.execute(
            "SELECT ticket_sequence FROM xrpl_action_ticket_leases"
            " WHERE network=? AND account=?",
            (network, account),
        )
    }


def mark_ticket(conn: sqlite3.Connection, network: str, account: str,
                ticket: int, *, state: str, last_ledger_sequence: int | None = None,
                outer_hash: str | None = None) -> None:
    if state not in TICKET_STATES:
        raise ValueError(f"unknown ticket state: {state}")
    conn.execute(
        "UPDATE xrpl_action_ticket_leases SET state=?, last_ledger_sequence=COALESCE(?,last_ledger_sequence), outer_hash=COALESCE(?,outer_hash) WHERE network=? AND account=? AND ticket_sequence=?",
        (state, last_ledger_sequence, outer_hash, network, account, ticket),
    )
    conn.commit()


def release_ticket(conn: sqlite3.Connection, network: str, account: str, ticket: int) -> bool:
    cur = conn.execute(
        "DELETE FROM xrpl_action_ticket_leases WHERE network=? AND account=? AND ticket_sequence=? AND state='leased'",
        (network, account, ticket),
    )
    conn.commit()
    return cur.rowcount == 1
```

Add the restart query and tests asserting only `done` is excluded: interrupted
Ticket-less preparation rows need their headroom, number, and staged art
cleaned up, while Ticket-bearing rejected, expired, failed, and indeterminate
rows need ledger reconciliation.

```python
def list_reconcilable_sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    cursor = conn.execute(
        "SELECT * FROM xrpl_action_sessions"
        " WHERE state != 'done' AND ("
        " state='preparing' OR ticket_sequence IS NOT NULL"
        " OR headroom_reserved=1 OR assets_prepared=1)"
        " ORDER BY created_ts"
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]
```

- [ ] **Step 4: Run focused store tests**

Run: `.venv/bin/python -m pytest tests/test_action_store.py -q`

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add lfg_core/action_store.py tests/test_action_store.py
git commit -m "feat: persist XRPL action sessions and ticket leases"
```

---

### Task 5: Refactor reusable mint preparation and validated settlement

**Files:**
- Modify: `lfg_core/mint_flow.py:300-590`
- Modify: `tests/test_mint_one_unit.py`
- Create: `tests/test_mint_action_assets.py`

**Interfaces:**
- Consumes: existing trait selection, composition, CDN upload, image archive, `record_nft_mint`, rarity, and headroom behavior.
- Produces: immutable `PreparedMintAssets`, `prepare_mint_assets()`, and `record_validated_mint()` used by both legacy `mint_one_unit()` and atomic actions.

- [ ] **Step 1: Write failing off-ledger preparation test**

```python
# tests/test_mint_action_assets.py
import pytest

from lfg_core import mint_flow


@pytest.mark.asyncio
async def test_prepare_mint_assets_never_calls_xrpl(monkeypatch, _asset_mocks):
    monkeypatch.setattr(
        mint_flow.xrpl_ops, "mint_nft",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("XRPL called")),
    )
    prepared = await mint_flow.prepare_mint_assets(
        nft_number=4001, session_tag="action:s1"
    )
    assert prepared.metadata_url == "https://cdn.example/4001/4001_0.json"
    assert prepared.image_url.endswith("4001_0.png")
    assert prepared.traits["Body"] == "Straight"
```

The `_asset_mocks` fixture copies the existing select/compose/upload/image-archive stubs from `tests/test_mint_one_unit.py` and does not mock mint, offer, or Xaman functions.

- [ ] **Step 2: Run the asset test and witness RED**

Run: `.venv/bin/python -m pytest tests/test_mint_action_assets.py -q`

Expected: FAIL because `prepare_mint_assets` does not exist.

- [ ] **Step 3: Extract preparation without changing legacy behavior**

Add this dataclass near `UnitResult`:

```python
@dataclass(frozen=True)
class PreparedMintAssets:
    nft_number: int
    session_tag: str
    metadata_url: str
    image_url: str
    video_url: str | None
    metadata: dict[str, Any]
    traits: dict[str, str]
    body_type: str
```

Create `prepare_mint_assets(nft_number, session_tag)` by moving the current trait selection, composition, still staging, CDN image/video upload, metadata assembly, metadata upload, Head-to-Hat normalization, and body capture out of `mint_one_unit()`. Return `PreparedMintAssets` after metadata upload and before any XRPL call. Replace the moved block in `mint_one_unit()` with:

```python
prepared = await prepare_mint_assets(nft_number=nft_number, session_tag=session_tag)
metadata_cdn_url = prepared.metadata_url
image_cdn_url = prepared.image_url
video_cdn_url = prepared.video_url
metadata = prepared.metadata
traits_dict = prepared.traits
body = prepared.body_type
```

- [ ] **Step 4: Run asset and legacy mint tests**

Run: `.venv/bin/python -m pytest tests/test_mint_action_assets.py tests/test_mint_one_unit.py tests/test_mint_cdn_paths.py tests/test_mint_issuer.py -q`

Expected: all tests PASS and the legacy result shape is unchanged.

- [ ] **Step 5: Write failing validated-settlement test**

```python
@pytest.mark.asyncio
async def test_record_validated_mint_promotes_and_records(monkeypatch, prepared_assets):
    calls = []
    monkeypatch.setattr(mint_flow.image_archive, "promote_still", lambda *args: calls.append("promote"))
    monkeypatch.setattr(mint_flow, "record_nft_mint", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setattr(mint_flow.rarity, "connect", _fake_rarity_connection)
    saved = await mint_flow.record_validated_mint(
        prepared_assets, nft_id="NFT1", wallet_address="rBuyer",
        user_id="u1", network="testnet"
    )
    assert saved is True
    assert calls[0] == "promote"
    assert calls[1]["nft_id"] == "NFT1"
    assert calls[1]["owner_address"] == "rBuyer"
```

- [ ] **Step 6: Extract post-validation recording and keep legacy tests green**

```python
async def record_validated_mint(
    prepared: PreparedMintAssets, *, nft_id: str, wallet_address: str,
    user_id: str, network: str,
    on_mint: Callable[[int, str, str], Awaitable[None]] | None = None,
) -> bool:
    image_archive.promote_still(network, prepared.nft_number, prepared.session_tag)
    if on_mint:
        await on_mint(prepared.nft_number, nft_id, prepared.image_url)
    record = {
        "nft_number": prepared.nft_number,
        "nft_id": nft_id,
        "discord_id": user_id,
        "owner_address": wallet_address,
        "metadata_url": prepared.metadata_url,
        "image_url": prepared.image_url,
        "traits": prepared.traits,
        "network": network,
        "body_type": prepared.body_type,
    }
    try:
        saved = await asyncio.to_thread(lambda: record_nft_mint(**record))
    except Exception:
        logging.error(f"record_nft_mint raised: {traceback.format_exc()}")
        saved = False
    if saved:
        _reserved_numbers.discard(prepared.nft_number)
        try:
            await asyncio.to_thread(
                _update_rarity_for_metadata,
                prepared.metadata,
                prepared.body_type,
            )
        except Exception:
            logging.error(f"rarity update failed: {traceback.format_exc()}")
    else:
        _save_recovery_record(record)
    return saved
```

Extract the existing nested rarity body without changing its operations:

```python
def _update_rarity_for_metadata(metadata: dict[str, Any], body_type: str) -> None:
    conn = rarity.connect()
    try:
        for attr in metadata["attributes"]:
            rarity.start_boost_clock(
                conn, body_type, attr["trait_type"], attr["value"]
            )
        rarity.start_boost_clock(
            conn, rarity.BODY_SENTINEL, rarity.BODY_CATEGORY, body_type
        )
        rarity.recalculate_rarity(conn)
    finally:
        conn.close()
```

Make `mint_one_unit()` call `record_validated_mint(..., on_mint=on_mint)`
immediately after its standalone mint confirms and before creating the legacy
offer. The helper promotes the still, invokes `on_mint` before database/rarity
awaits (preserving bulk crash-safety), then records the mint. The atomic flow
passes no callback. Preserve the existing rule that database and rarity
exceptions are logged/recovered and do not prevent the legacy transfer offer
from reaching the buyer.

- [ ] **Step 7: Run the refactor regression slice**

Run: `.venv/bin/python -m pytest tests/test_mint_action_assets.py tests/test_mint_one_unit.py tests/test_mint_cdn_paths.py tests/test_mint_issuer.py tests/test_mint_session_traits.py tests/test_shared_mint_result.py -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add lfg_core/mint_flow.py tests/test_mint_one_unit.py tests/test_mint_action_assets.py
git commit -m "refactor: share mint asset preparation and settlement"
```

---

### Task 6: Xaman Batch payload and atomic mint orchestrator

**Files:**
- Modify: `lfg_core/xumm_ops.py:250-420,620-720`
- Create: `lfg_core/atomic_mint.py`
- Create: `tests/test_xumm_batch.py`
- Create: `tests/test_atomic_mint.py`

**Interfaces:**
- Consumes: `PreparedMintAssets`, `PreparedBatch`, `action_store`, `xrpl_actions`, Xaman payload/status helpers, headroom, and existing price selection.
- Produces: `create_batch_payload()`, richer `get_payload_status()`, `AtomicMintSession`, `prepare_session()`, `refresh_session()`, and `reconcile_sessions()`.

- [ ] **Step 1: Write failing Xaman payload test**

```python
# tests/test_xumm_batch.py
import pytest

from lfg_core import xumm_ops


@pytest.mark.asyncio
async def test_batch_payload_enforces_buyer_and_submits(monkeypatch):
    captured = {}
    async def fake_post(payload):
        captured.update(payload)
        return {"qr_url": "q", "xumm_url": "x", "uuid": "u", "pushed": False}
    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    result = await xumm_ops.create_batch_payload(
        {"TransactionType": "Batch", "Account": "rBuyer"},
        signer="rBuyer", return_url=None, user_token=None
    )
    assert captured["options"]["submit"] is True
    assert captured["options"]["signer"] == "rBuyer"
    assert result["uuid"] == "u"
```

- [ ] **Step 2: Run Xaman test and witness RED**

Run: `.venv/bin/python -m pytest tests/test_xumm_batch.py -q`

Expected: FAIL because `create_batch_payload` does not exist.

- [ ] **Step 3: Implement enforced-signer Batch payload and status fields**

```python
async def create_batch_payload(
    txjson: dict[str, Any], *, signer: str,
    return_url: dict[str, str] | None = None,
    user_token: str | None = None,
) -> dict[str, Any] | None:
    if txjson.get("TransactionType") != "Batch" or txjson.get("Account") != signer:
        raise ValueError("Batch Account must equal enforced signer")
    return await _create_xumm_payload(
        txjson,
        options=_with_return_url({"submit": True, "signer": signer}, return_url),
        user_token=user_token,
    )
```

Extend `get_payload_status()` with:

```python
"cancelled": bool(meta.get("cancelled")),
"resolved": bool(meta.get("resolved")),
```

and update `_terminal()` to include `cancelled`. Add tests for signed, rejected, and expired status normalization.

- [ ] **Step 4: Write failing orchestrator happy-path and rejection tests**

```python
# tests/test_atomic_mint.py
import pytest

from lfg_core import atomic_mint


@pytest.mark.asyncio
async def test_prepare_session_persists_batch_before_xaman(action_deps):
    session = atomic_mint.AtomicMintSession.new(
        user_id="u1", wallet="rBuyer", platform="web", network="testnet"
    )
    await atomic_mint.prepare_session(session, action_deps)
    assert session.state == atomic_mint.AWAITING_SIGNATURE
    assert session.ticket_sequence == 7
    assert session.offer_id == "OFFER7"
    assert session.batch_json["RawTransactions"][0]["RawTransaction"]["TransactionType"] == "Payment"
    assert action_deps.events.index("persist-batch") < action_deps.events.index("create-xaman")


@pytest.mark.asyncio
async def test_rejected_session_releases_assets_but_not_ticket_before_lls(action_deps):
    session = action_deps.awaiting_session()
    action_deps.payload_status = {"signed": False, "cancelled": True, "expired": False}
    await atomic_mint.refresh_session(session, action_deps)
    assert session.state == atomic_mint.REJECTED
    assert action_deps.ticket_released is False
```

- [ ] **Step 5: Implement the state model and dependency seam**

```python
# lfg_core/atomic_mint.py
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

PREPARING = "preparing"
AWAITING_SIGNATURE = "awaiting_signature"
CONFIRMING = "confirming"
DONE = "done"
REJECTED = "rejected"
EXPIRED = "expired"
FAILED = "failed"
INDETERMINATE = "indeterminate"
TERMINAL_STATES = {DONE, REJECTED, EXPIRED, FAILED, INDETERMINATE}


@dataclass
class AtomicMintSession:
    id: str
    user_id: str
    wallet: str
    platform: str
    network: str
    campaign: str | None = None
    state: str = PREPARING
    created_at: int = field(default_factory=lambda: int(time.time()))
    pay_with: str | None = None
    pay_amount: str | None = None
    nft_number: int | None = None
    metadata_url: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    traits: dict[str, str] | None = None
    body_type: str | None = None
    ticket_sequence: int | None = None
    offer_id: str | None = None
    batch_json: dict[str, Any] | None = None
    inner_hashes: tuple[str, str, str] | None = None
    last_ledger_sequence: int | None = None
    xumm_uuid: str | None = None
    xumm_url: str | None = None
    qr_url: str | None = None
    outer_hash: str | None = None
    nft_id: str | None = None
    ledger_index: int | None = None
    error_code: str | None = None

    @classmethod
    def new(
        cls, *, user_id: str, wallet: str, platform: str, network: str,
        campaign: str | None = None,
    ) -> "AtomicMintSession":
        return cls(
            uuid.uuid4().hex, user_id, wallet, platform, network,
            campaign=campaign,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "type": "xrpl-action-session", "version": "1",
            "sessionId": self.id, "state": self.state,
            "pay_with": self.pay_with,
            "pay_amount": self.pay_amount, "nft_number": self.nft_number,
            "image_url": self.image_url, "video_url": self.video_url,
            "error_code": self.error_code,
        }
        if self.state == AWAITING_SIGNATURE:
            data.update({
                "type": "xrpl-sign-request",
                "account": self.wallet,
                "transaction": self.batch_json,
                "wallets": {"xaman": {
                    "uuid": self.xumm_uuid,
                    "deeplink": self.xumm_url,
                    "qr": self.qr_url,
                }},
            })
        if self.state == DONE:
            data.update({
                "outer_hash": self.outer_hash,
                "inner_hashes": list(self.inner_hashes or ()),
                "nft_id": self.nft_id,
                "ledger_index": self.ledger_index,
            })
        return data
```

Define the dependency seam with these exact signatures:

```python
@dataclass
class AtomicMintDeps:
    capability: Callable[[], Awaitable[Any]]
    choose_payment: Callable[[str], Awaitable[Any]]
    reserve_headroom: Callable[[AtomicMintSession], Awaitable[bool]]
    allocate_number: Callable[[], Awaitable[int]]
    prepare_assets: Callable[[int, str], Awaitable[Any]]
    list_tickets: Callable[[], Awaitable[list[int]]]
    lease_ticket: Callable[[AtomicMintSession, list[int]], Awaitable[int | None]]
    prepare_batch: Callable[[AtomicMintSession, Any, Any], Awaitable[Any]]
    persist: Callable[[AtomicMintSession], Awaitable[None]]
    create_payload: Callable[[AtomicMintSession], Awaitable[dict[str, Any] | None]]
    payload_status: Callable[[str], Awaitable[dict[str, Any] | None]]
    verify_batch: Callable[[AtomicMintSession], Awaitable[Any | None]]
    current_ledger: Callable[[], Awaitable[int]]
    ledger_tickets: Callable[[], Awaitable[list[int]]]
    record_mint: Callable[[AtomicMintSession, str], Awaitable[bool]]
    buy_and_burn: Callable[[AtomicMintSession], Awaitable[None]]
    settle_headroom: Callable[[AtomicMintSession, bool], Awaitable[None]]
    release_ticket: Callable[[AtomicMintSession], Awaitable[bool]]
    quarantine_ticket: Callable[[AtomicMintSession], Awaitable[None]]
```

`prepare_session()` executes those boundaries in this order: capability,
headroom, quote, number, assets, ledger Tickets, durable lease, Batch,
`persist`, then Xaman. Immediately after `prepare_batch`, assign canonical wire
JSON with `session.batch_json = prepared.transaction.to_xrpl()` (never
`to_dict()`), plus `offer_id`, `inner_hashes`, and `last_ledger_sequence`, then
persist that fixed transaction before creating the sole Xaman payload. Persist
the returned payload UUID/links without generating a second payload. It maps
false headroom to `capacity_reached`, no Ticket to `ticket_unavailable`, and
payload failure to `signing_unavailable`.
`refresh_session()` changes `awaiting_signature -> confirming` only when Xaman
returns signed with a txid; calls the fixed-hash verifier in `confirming`;
records the mint and optional XRP buyback only after the verifier returns the
NFT ID; and leaves Ticket release to ledger-height reconciliation.

- [ ] **Step 6: Add fixed-hash verification tests before its implementation**

Add to `tests/test_xrpl_actions.py` a fake transaction-fetch function and cases for: same-ledger three-inner success, missing `ParentBatchID`, one `tec` result, outer-only success, and a mint/accept NFToken ID mismatch. The success case calls:

```python
result = await xrpl_actions.verify_atomic_batch_result(
    outer_hash="OUTER", inner_hashes=("PAY", "MINT", "ACCEPT"),
    expected_offer_id="OFFER", fetch_tx=fake_fetch
)
assert result.nft_id == "NFT1"
assert result.ledger_index == 100
```

- [ ] **Step 7: Implement strict fixed-hash verification**

Add `VerifiedAtomicMint(nft_id: str, ledger_index: int)` and `verify_atomic_batch_result()`. It fetches the four exact hashes, requires `validated`, identical ledger indexes, inner `meta.TransactionResult == "tesSUCCESS"`, matching `ParentBatchID`, the prepared offer ID on the accept transaction, and equal mint/accept `nftoken_id` metadata. Return `None` while a hash is not yet validated; raise `AtomicMintInvariantError` for a definitive mismatch or inner failure.

- [ ] **Step 8: Run orchestrator, protocol, and Xaman tests**

Run: `.venv/bin/python -m pytest tests/test_xumm_batch.py tests/test_xrpl_actions.py tests/test_atomic_mint.py -q`

Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
git add lfg_core/xumm_ops.py lfg_core/atomic_mint.py lfg_core/xrpl_actions.py tests/test_xumm_batch.py tests/test_atomic_mint.py tests/test_xrpl_actions.py
git commit -m "feat: orchestrate one-signature atomic mint actions"
```

---

### Task 7: Actions discovery and authenticated service API

**Files:**
- Modify: `lfg_service/app.py:80-115,2850-2960,3470-3560,4480-4590`
- Create: `tests/test_actions_service.py`

**Interfaces:**
- Consumes: `AtomicMintSession`, atomic-mint prepare/refresh functions, existing `require_wallet`, user/platform/push-token helpers, headroom reservation, and event publishing.
- Produces: `GET /.well-known/xrpl-actions.json`, `GET /api/actions/mint`, `POST /api/actions/mint`, `GET /api/actions/mint/active`, and `GET /api/actions/mint/{session_id}`.

- [ ] **Step 1: Write failing disabled-metadata and wallet-binding tests**

```python
# tests/test_actions_service.py
import pytest
from unittest.mock import AsyncMock

from lfg_core import xrpl_actions
from lfg_service import app as server


@pytest.fixture
def action_app(monkeypatch, tmp_path):
    monkeypatch.setattr(server.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(server, "atomic_mint_sessions", {})
    monkeypatch.setattr(
        server.db_path, "app_db_path", lambda network=None: str(tmp_path / "actions.db")
    )
    return server.create_app()


@pytest.mark.asyncio
async def test_action_metadata_reports_ledger_gate(aiohttp_client, action_app, monkeypatch):
    monkeypatch.setattr(
        server,
        "_action_readiness",
        AsyncMock(return_value=xrpl_actions.BatchCapability(False, "batch_unavailable")),
    )
    client = await aiohttp_client(action_app)
    response = await client.get("/api/actions/mint")
    body = await response.json()
    assert response.status == 200
    assert body["enabled"] is False
    assert body["unavailableReason"] == "batch_unavailable"


@pytest.mark.asyncio
async def test_create_rejects_body_account_different_from_session_wallet(
    aiohttp_client, action_app
):
    client = await aiohttp_client(action_app)
    response = await client.post("/api/actions/mint", json={"account": "rForeign"})
    assert response.status == 403
    assert (await response.json())["code"] == "wallet_mismatch"


@pytest.mark.asyncio
async def test_create_returns_202_and_starts_background_preparation(
    aiohttp_client, action_app, monkeypatch
):
    started = []
    monkeypatch.setattr(
        server,
        "_action_readiness",
        AsyncMock(return_value=xrpl_actions.BatchCapability(True, None)),
    )
    monkeypatch.setattr(
        server, "_schedule_atomic_mint", lambda session: started.append(session.id)
    )
    client = await aiohttp_client(action_app)
    response = await client.post(
        "/api/actions/mint", json={"account": server.mock_economy.DEV_OWNER}
    )
    body = await response.json()
    assert response.status == 202
    assert body["state"] == "preparing"
    assert started == [body["sessionId"]]
```

- [ ] **Step 2: Run service tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_actions_service.py -q`

Expected: FAIL with 404 responses because no action routes exist.

- [ ] **Step 3: Add session registry, metadata, and create/status handlers**

Add `atomic_mint_sessions: dict[str, AtomicMintSession] = {}` beside `mint_sessions`. Implement:

```python
async def handle_action_metadata(request: web.Request) -> web.Response:
    capability = await _action_readiness()
    body = {
        "type": "xrpl-action", "version": "1",
        "chain": f"xrpl:{config.XRPL_NETWORK}",
        "icon": f"{config.EXTERNAL_WEBSITE_URL}/assets/mascot.png",
        "title": "Mint an LFG",
        "description": "Pay, mint, and receive your NFT atomically.",
        "label": "Mint",
        "transactionTypes": ["Payment", "NFTokenMint", "NFTokenAcceptOffer"],
        "requirements": {"amendments": ["NFTokenMintOffer", "BatchV1_1"], "wallet": "xaman"},
        "enabled": capability.enabled,
        "links": {"actions": [{"label": "Mint", "href": "/api/actions/mint"}]},
    }
    if capability.reason:
        body["unavailableReason"] = capability.reason
    return web.json_response(body)


@require_wallet
async def handle_action_create(request: web.Request) -> web.Response:
    body = await request.json()
    if body.get("account") != request["wallet"]:
        return web.json_response({"code": "wallet_mismatch"}, status=403)
    campaign = body.get("campaign")
    if campaign not in (None, "x-mint-link"):
        return web.json_response({"code": "invalid_campaign"}, status=400)
    capability = await _action_readiness()
    if not capability.enabled:
        return web.json_response({"code": capability.reason}, status=503)
    active = _active_atomic_session(request["user"], request["wallet"])
    if active:
        return web.json_response({"code": "mint_in_progress", "session": active.to_dict()}, status=409)
    session = AtomicMintSession.new(
        user_id=request["user"]["id"], wallet=request["wallet"],
        platform=_platform(request["user"]), network=config.XRPL_NETWORK,
        campaign=campaign,
    )
    atomic_mint_sessions[session.id] = session
    _schedule_atomic_mint(session)
    return web.json_response(
        {"type": "xrpl-action-session", "version": "1", "sessionId": session.id,
         "state": session.state, "status": f"/api/actions/mint/{session.id}"},
        status=202,
    )
```

Define readiness and active-session lookup explicitly:

```python
async def _action_readiness() -> xrpl_actions.BatchCapability:
    client = JsonRpcClient(config.JSON_RPC_URL)
    try:
        capability = await xrpl_actions.fetch_batch_capability(
            client, configured=config.XRPL_ACTIONS_BATCH_ENABLED
        )
    except Exception:
        logging.warning("XRPL Action capability lookup failed", exc_info=True)
        return xrpl_actions.BatchCapability(False, "batch_unavailable")
    if not capability.enabled:
        return capability
    try:
        tickets = await xrpl_actions.list_ticket_sequences(
            client, config.SIGNING_ACCOUNT
        )
    except Exception:
        logging.warning("XRPL Action Ticket lookup failed", exc_info=True)
        return xrpl_actions.BatchCapability(False, "ticket_unavailable")
    conn = sqlite3.connect(db_path.app_db_path(config.XRPL_NETWORK))
    try:
        leased = action_store.leased_ticket_sequences(
            conn, config.XRPL_NETWORK, config.SIGNING_ACCOUNT
        )
    finally:
        conn.close()
    if not set(tickets).difference(leased):
        return xrpl_actions.BatchCapability(False, "ticket_unavailable")
    return capability


def _active_atomic_session(user: dict[str, Any], wallet: str) -> AtomicMintSession | None:
    return next(
        (
            session
            for session in atomic_mint_sessions.values()
            if session.user_id == user["id"]
            and session.platform == _platform(user)
            and session.wallet == wallet
            and session.state not in atomic_mint.TERMINAL_STATES
        ),
        None,
    )
```

Use the tested `action_store.leased_ticket_sequences()` interface added in
Task 4; readiness counts quarantined and consumed rows as unavailable until
reconciliation explicitly proves they are safe to remove.

`handle_action_status` requires auth, checks `user_id`, platform, and wallet, calls `refresh_session()`, persists any issued push token, and returns `session.to_dict()`. `handle_action_active` returns only a matching non-terminal session.

- [ ] **Step 4: Add routes and action creation limiter**

Register the exact paths before `/api/mint/{session_id}` and the static catch-all:

```python
app.router.add_get("/.well-known/xrpl-actions.json", handle_actions_discovery)
app.router.add_get("/api/actions/mint", handle_action_metadata)
app.router.add_post("/api/actions/mint", handle_action_create)
app.router.add_get("/api/actions/mint/active", handle_action_active)
app.router.add_get("/api/actions/mint/{session_id}", handle_action_status)
```

Use the existing sliding-window limiter pattern with a separate `_action_create_hits` map, keyed by authenticated wallet and capped at `config.XRPL_ACTIONS_CREATE_LIMIT` per minute. The discovery response uses the request origin for same-origin service deployments and the configured web API base for production Pages.

- [ ] **Step 5: Add failing ownership, limit, and success response tests**

Add tests that a foreign user gets 404 for another session, the fourth create within one minute gets 429, an `awaiting_signature` response contains the full Batch plus Xaman links, and `done` includes outer/inner hashes and NFT ID.

- [ ] **Step 6: Run service and legacy mint service tests**

Run: `.venv/bin/python -m pytest tests/test_actions_service.py tests/test_service_auth.py tests/test_service_mint_platform.py tests/test_mint_active_resume.py tests/test_mint_cancel.py -q`

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add lfg_service/app.py tests/test_actions_service.py
git commit -m "feat: expose authenticated XRPL mint actions"
```

---

### Task 8: PWA action deep-link and one-approval UI

**Files:**
- Create: `webapp/client/action_pure.js`
- Modify: `webapp/client/app.js:1-20,380-410,590-880,2500-2620`
- Modify: `webapp/client/style.css`
- Create: `webapp/client/.well-known/xrpl-actions.json`
- Modify: `.github/workflows/pages.yml`
- Create: `tests/test_action_pure_js.py`

**Interfaces:**
- Consumes: action API response states and existing `api()`, `showFlow()`, QR, push, external opener, auth boot, and share helpers.
- Produces: `requestedAction()`, `actionIsTerminal()`, direct `?action=mint` boot, preparation polling, one Xaman Batch request, completion rendering, and Pages discovery output.

- [ ] **Step 1: Write failing pure route/state tests**

```python
# tests/test_action_pure_js.py
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_js(expr):
    script = (
        "import * as M from './webapp/client/action_pure.js';\n"
        f"const result = ({expr});\n"
        "console.log(JSON.stringify(result === undefined ? null : result));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module"], input=script, capture_output=True,
        text=True, cwd=ROOT, timeout=15
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_action_route_only_accepts_mint():
    assert run_js("M.requestedAction('?action=mint')") == "mint"
    assert run_js("M.requestedAction('?action=other')") is None


def test_terminal_action_states():
    for state in ("done", "rejected", "expired", "failed", "indeterminate"):
        assert run_js(f"M.actionIsTerminal('{state}')") is True
    assert run_js("M.actionIsTerminal('confirming')") is False


def test_action_error_copy_is_safe_and_bounded():
    assert "ticket" in run_js("M.actionErrorCopy('ticket_unavailable')").lower()
    assert run_js("M.actionErrorCopy('<script>')") == "The atomic mint did not complete. No payment was taken."
```

- [ ] **Step 2: Run JS tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_action_pure_js.py -q`

Expected: FAIL because `action_pure.js` does not exist.

- [ ] **Step 3: Implement pure helpers**

```javascript
// webapp/client/action_pure.js
const TERMINAL = new Set(['done', 'rejected', 'expired', 'failed', 'indeterminate']);
const ERRORS = Object.freeze({
  action_disabled: 'Atomic minting is not enabled yet.',
  batch_unavailable: 'The connected XRPL network is not ready for atomic Batch minting.',
  mint_offer_unavailable: 'The connected XRPL network cannot create the destination-locked mint offer yet.',
  ticket_unavailable: 'No issuer ticket is available right now. Please try again shortly.',
  capacity_reached: 'The collection is currently at mint capacity.',
  signing_unavailable: 'Xaman could not create the signing request. No payment was taken.',
});

export function requestedAction(search) {
  const action = new URLSearchParams(search || '').get('action');
  return action === 'mint' ? action : null;
}

export function actionIsTerminal(state) {
  return TERMINAL.has(state);
}

export function actionErrorCopy(code) {
  return ERRORS[code] || 'The atomic mint did not complete. No payment was taken.';
}
```

- [ ] **Step 4: Add failing app wiring assertions**

```python
def test_app_boot_routes_mint_action_after_auth():
    src = Path("webapp/client/app.js").read_text()
    assert "./action_pure.js" in src
    assert "requestedAction(window.location.search)" in src
    assert "startAtomicMint" in src


def test_action_ui_never_calls_legacy_accept_endpoint():
    src = Path("webapp/client/app.js").read_text()
    body = src.split("async function startAtomicMint", 1)[1].split("\n}\n", 1)[0]
    assert "/api/actions/mint" in body
    assert "create_accept" not in body


def test_pages_workflow_publishes_and_rewrites_action_discovery():
    discovery = Path("webapp/client/.well-known/xrpl-actions.json")
    assert discovery.exists()
    assert json.loads(discovery.read_text())["rules"][0]["apiPath"] == "/api/actions/**"
    workflow = Path(".github/workflows/pages.yml").read_text()
    assert "cp -r webapp/client/. _site/" in workflow
    assert "_site/.well-known/xrpl-actions.json" in workflow
    assert "WEB_API_BASE" in workflow
```

- [ ] **Step 5: Wire the action flow into the existing UI**

Import `action_pure.js` next to the existing pure modules with
`import * as actionPure from './action_pure.js';`, add
`let currentAtomicMintId = null`, `let atomicTimer = null`, and
`let atomicPollGen = 0`, then add:

```javascript
async function startAtomicMint() {
  const start = await api('/api/actions/mint', {
    method: 'POST',
    body: JSON.stringify({account: me.wallet, campaign: 'x-mint-link'}),
  });
  currentAtomicMintId = start.sessionId;
  showFlow({title: '🎨 Preparing your mint', text: 'Composing your NFT and securing an atomic ledger slot…', spinner: true});
  pollAtomicMint(start.sessionId);
}

function pollAtomicMint(sessionId) {
  const gen = ++atomicPollGen;
  async function tick() {
    if (gen !== atomicPollGen) return;
    let s;
    try {
      s = await api(`/api/actions/mint/${sessionId}`);
    } catch (_) {
      atomicTimer = setTimeout(tick, 2000);
      return;
    }
    if (s.state === 'awaiting_signature') {
      showFlow({
        title: 'Approve your atomic mint',
        text: 'One Xaman approval covers payment, mint, and delivery — all or nothing.',
        qrData: s.wallets.xaman.qr,
        link: s.wallets.xaman.deeplink,
        spinner: false,
      });
    } else if (s.state === 'confirming') {
      showFlow({title: '⛓️ Confirming on XRPL', text: 'Checking all three Batch results…', spinner: true});
    } else if (s.state === 'done') {
      showFlow({
        title: `LFG #${s.nft_number} is yours`, text: 'Payment, mint, and transfer validated atomically.',
        image: s.image_url,
        video: s.video_url,
        done: true,
        celebrate: true,
        share: {text: mintShareText(s.nft_number), url: shareUrlFor(s.nft_number, s.nft_id)},
      });
      return;
    } else if (actionPure.actionIsTerminal(s.state)) {
      showFlow({title: 'Mint not completed', text: actionPure.actionErrorCopy(s.error_code), done: true});
      return;
    }
    atomicTimer = setTimeout(tick, 2000);
  }
  tick();
}
```

`showFlow()` already accepts every property above; do not add an unsupported
`retry` property. Add `resumeAtomicMint()` using `/api/actions/mint/active` and
the same reconnect/poll pattern as `resumeMint()`. Set the mint button handler
to `startAtomicMint` only when
`actionPure.requestedAction(window.location.search) === 'mint'`, otherwise to
the existing `startMint`. In both authenticated boot branches, when the action
query is present, attempt `resumeAtomicMint()` and then call `startAtomicMint()`
if no action session is active; do not call legacy `resumeMint()` for that URL.
Non-action URLs retain the existing boot sequence unchanged.

- [ ] **Step 6: Add discovery document and Pages API rewrite**

```json
{
  "version": "1",
  "rules": [
    {"pathPattern": "/actions/**", "apiPath": "/api/actions/**"}
  ]
}
```

In `.github/workflows/pages.yml`, after copying `webapp/client` to `_site`, add
this step inside `Assemble site`, after writing `config.js`:

```yaml
          python - <<'PY'
          import json
          import os
          from pathlib import Path

          path = Path("_site/.well-known/xrpl-actions.json")
          document = json.loads(path.read_text())
          for rule in document["rules"]:
              rule["apiPath"] = os.environ["WEB_API_BASE"].rstrip("/") + "/api/actions/**"
          path.write_text(json.dumps(document, indent=2) + "\n")
          PY
```

The `cp -r webapp/client/. _site/` source form is retained because the `.` is
what copies the hidden `.well-known` directory. The workflow source assertion
above locks both that copy behavior and the explicit artifact rewrite.

- [ ] **Step 7: Run client tests**

Run: `.venv/bin/python -m pytest tests/test_action_pure_js.py tests/test_mint_pure_js.py tests/test_app_js_boot.py -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add webapp/client/action_pure.js webapp/client/app.js webapp/client/style.css webapp/client/.well-known/xrpl-actions.json .github/workflows/pages.yml tests/test_action_pure_js.py
git commit -m "feat: open atomic mint actions from the PWA"
```

---

### Task 9: Ticket provisioning CLI and draft XLS

**Files:**
- Create: `scripts/provision_batch_tickets.py`
- Create: `tests/test_batch_ticket_cli.py`
- Create: `docs/xls/xrpl-actions.md`
- Modify: `README.md:190-280`
- Modify: `CLAUDE.md:50-115,350-430`
- Modify: `docs/ops/env.staging.example`

**Interfaces:**
- Consumes: `config.assert_cli_network_match()`, `xrpl_actions.list_ticket_sequences()`, xrpl-py `TicketCreate`, existing single-sign submit/confirm posture, and Actions JSON schemas.
- Produces: `status` and explicit `provision` commands plus a self-contained application-standard proposal.

- [ ] **Step 1: Write failing CLI parser and no-side-effect tests**

```python
# tests/test_batch_ticket_cli.py
import importlib


def test_import_has_no_ledger_side_effect(monkeypatch):
    calls = []
    monkeypatch.setattr("xrpl.transaction.submit_and_wait", lambda *a, **k: calls.append(1))
    importlib.import_module("scripts.provision_batch_tickets")
    assert calls == []


def test_parser_requires_matching_network():
    from scripts.provision_batch_tickets import build_parser
    args = build_parser().parse_args(["status", "--network", "testnet"])
    assert args.command == "status"
    assert args.network == "testnet"
```

- [ ] **Step 2: Run CLI tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_batch_ticket_cli.py -q`

Expected: FAIL because the script does not exist.

- [ ] **Step 3: Implement explicit status/provision commands**

```python
# scripts/provision_batch_tickets.py
from __future__ import annotations

import argparse
import asyncio

from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import TicketCreate
from xrpl.wallet import Wallet

from lfg_core import config, memos, xrpl_actions, xrpl_ops


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or provision XRPL Action issuer Tickets")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "provision"):
        child = sub.add_parser(command)
        child.add_argument("--network", required=True, choices=("testnet", "mainnet"))
    sub.choices["provision"].add_argument("--target", type=int, default=config.XRPL_ACTIONS_TICKET_TARGET)
    return parser


async def run(args: argparse.Namespace) -> int:
    config.assert_cli_network_match(args.network)
    client = JsonRpcClient(config.JSON_RPC_URL)
    tickets = await xrpl_actions.list_ticket_sequences(client, config.SIGNING_ACCOUNT)
    if args.command == "status":
        print(f"{args.network}: {len(tickets)} issuer Tickets")
        return 0
    missing = max(0, args.target - len(tickets))
    if missing == 0:
        print(f"{args.network}: target already met")
        return 0
    wallet = Wallet.from_seed(config.SEED)
    tx = TicketCreate(
        account=config.SIGNING_ACCOUNT,
        ticket_count=missing,
        source_tag=config.SOURCE_TAG,
        memos=memos.build_memo_models(
            memos.INITIATOR_BACKEND,
            memos.PLATFORM_BACKEND,
            memos.ACTION_BATCH_TICKET_CREATE,
        ),
    )
    result = await xrpl_ops.submit_backend_transaction(tx, wallet, label="TicketCreate")
    print(result["hash"])
    return 0


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

Expose this narrowly named wrapper in `lfg_core/xrpl_ops.py` rather than
importing a private helper from the CLI:

```python
async def submit_backend_transaction(
    tx: Transaction, wallet: Wallet, *, label: str
) -> dict[str, Any]:
    client = JsonRpcClient(config.JSON_RPC_URL)
    result = await _submit_and_confirm(tx, wallet, client, label)
    if result is None:
        raise RuntimeError(f"{label} failed with a validated ledger result")
    return result
```

Add `ACTION_BATCH_TICKET_CREATE = "batch-ticket-create"` to `memos.py` and to
its closed `_ACTIONS` set. Extend `tests/test_batch_ticket_cli.py` with a fake
Ticket list and a patched `submit_backend_transaction`; assert the captured
transaction has `Account == config.SIGNING_ACCOUNT`, the configured SourceTag,
the exact missing `TicketCount`, and decoded memo values `backend`, `backend`,
and `batch-ticket-create`. Also assert `status` never calls the submit wrapper.

- [ ] **Step 4: Write the draft application XLS**

Create `docs/xls/xrpl-actions.md` with complete sections: abstract, motivation, terminology, `/.well-known/xrpl-actions.json`, GET metadata schema, asynchronous POST/status schema, canonical XRPL JSON transaction response, wallet requirements, Batch multi-account signing, chain/amendment identifiers, error codes, CORS, authentication/resource-abuse guidance, replay/expiry rules, security considerations, LFG payment-first example, backward compatibility, and reference implementation link. State explicitly that it is an application standard and does not activate or replace `BatchV1_1`.

- [ ] **Step 5: Document deployment controls**

Add to environment docs:

```dotenv
XRPL_ACTIONS_BATCH_ENABLED=0
XRPL_ACTIONS_LAST_LEDGER_OFFSET=90
XRPL_ACTIONS_TICKET_TARGET=16
XRPL_ACTIONS_CREATE_LIMIT=3
```

Document the exact `status` and `provision` commands, owner-reserve impact, testnet-first requirement, exact amendment IDs, and that production activation waits for `BatchV1_1` to be enabled.

- [ ] **Step 6: Run CLI and documentation checks**

Run: `.venv/bin/python -m pytest tests/test_batch_ticket_cli.py tests/test_xumm_source_tag.py -q && git diff --check`

Expected: tests PASS and `git diff --check` prints nothing.

- [ ] **Step 7: Commit**

```bash
git add scripts/provision_batch_tickets.py tests/test_batch_ticket_cli.py docs/xls/xrpl-actions.md README.md CLAUDE.md docs/ops/env.staging.example lfg_core/xrpl_ops.py lfg_core/memos.py
git commit -m "docs: publish XRPL Actions draft and ticket operations"
```

---

### Task 10: Restart reconciliation, end-to-end contract test, and full verification

**Files:**
- Modify: `lfg_core/atomic_mint.py`
- Modify: `lfg_service/app.py`
- Modify: `tests/test_atomic_mint.py`
- Modify: `tests/test_actions_service.py`
- Create: `tests/test_atomic_mint_contract.py`

**Interfaces:**
- Consumes: persisted sessions/leases, fixed transaction hashes, ledger index and Ticket discovery, service startup/cleanup hooks, and all prior public interfaces.
- Produces: startup reconciliation, safe Ticket release/quarantine, and a single in-process reference flow proving the exact API-to-Batch contract.

- [ ] **Step 1: Write failing reconciliation tests**

```python
@pytest.mark.asyncio
async def test_expired_unsigned_batch_releases_ticket_after_last_ledger(action_deps):
    session = action_deps.expired_session(last_ledger_sequence=100, ticket=7)
    action_deps.current_ledger = 101
    action_deps.outer_lookup = None
    action_deps.ledger_tickets = [7]
    await atomic_mint.reconcile_session(session, action_deps)
    assert action_deps.released_tickets == [7]


@pytest.mark.asyncio
async def test_unknown_outer_result_quarantines_ticket(action_deps):
    session = action_deps.confirming_session(last_ledger_sequence=100, ticket=7)
    action_deps.current_ledger = 101
    action_deps.outer_lookup_raises = RuntimeError("RPC unavailable")
    await atomic_mint.reconcile_session(session, action_deps)
    assert session.state == atomic_mint.INDETERMINATE
    assert action_deps.quarantined_tickets == [7]


@pytest.mark.asyncio
async def test_consumed_ticket_with_validated_batch_resumes_settlement(action_deps):
    session = action_deps.confirming_session(ticket=7)
    action_deps.ledger_tickets = []
    action_deps.verified_nft_id = "NFT1"
    await atomic_mint.reconcile_session(session, action_deps)
    assert session.state == atomic_mint.DONE
    assert session.nft_id == "NFT1"
```

- [ ] **Step 2: Run reconciliation tests and witness RED**

Run: `.venv/bin/python -m pytest tests/test_atomic_mint.py -q`

Expected: FAIL because `reconcile_session` does not exist.

- [ ] **Step 3: Implement reconciliation and startup wiring**

`reconcile_session()` follows this exact decision table:

```python
if verified_batch:
    await _settle_verified_session(session, deps, verified_batch)
elif lookup_failed:
    session.state = INDETERMINATE
    await deps.mark_ticket_quarantined(session)
elif current_ledger <= session.last_ledger_sequence:
    return
elif session.ticket_sequence in await deps.list_ledger_tickets():
    session.state = EXPIRED
    await deps.release_ticket(session)
else:
    session.state = INDETERMINATE
    await deps.mark_ticket_quarantined(session)
```

On service startup, load `list_reconcilable_sessions()`, reconstruct `AtomicMintSession` values, insert them in `atomic_mint_sessions`, and schedule reconciliation. On cleanup, await/cancel only local preparation/poll tasks; persisted fixed hashes remain recoverable on next start.

- [ ] **Step 4: Write a failing full contract test**

```python
# tests/test_atomic_mint_contract.py
@pytest.mark.asyncio
async def test_action_contract_is_payment_first_single_request_and_same_ledger(
    authenticated_action_client, fake_action_ledger
):
    start = await authenticated_action_client.post(
        "/api/actions/mint", json={"account": authenticated_action_client.wallet}
    )
    assert start.status == 202
    ready = await authenticated_action_client.wait_for_state(start, "awaiting_signature")
    raw = ready["transaction"]["RawTransactions"]
    assert [item["RawTransaction"]["TransactionType"] for item in raw] == [
        "Payment", "NFTokenMint", "NFTokenAcceptOffer"
    ]
    assert raw[1]["RawTransaction"]["Amount"] == "0"
    assert ready["wallets"]["xaman"]["deeplink"]
    fake_action_ledger.resolve_xaman_and_validate_all(ready, ledger_index=500)
    done = await authenticated_action_client.wait_for_state(start, "done")
    assert done["nft_id"] == fake_action_ledger.nft_id
    assert done["ledger_index"] == 500
    assert fake_action_ledger.xaman_payload_count == 1
```

- [ ] **Step 5: Implement only the missing response/reconciliation wiring needed by the contract**

Ensure `AtomicMintSession.to_dict()` includes `transaction` only in `awaiting_signature`, includes `inner_hashes`, `outer_hash`, `nft_id`, and `ledger_index` only after validation, and never exposes Ticket lease internals or issuer key material. Ensure the action service creates exactly one Xaman payload per persisted Batch and status polling never regenerates it.

- [ ] **Step 6: Run feature suites**

Run: `.venv/bin/python -m pytest tests/test_xrpl_actions.py tests/test_action_store.py tests/test_mint_action_assets.py tests/test_xumm_batch.py tests/test_atomic_mint.py tests/test_actions_service.py tests/test_action_pure_js.py tests/test_batch_ticket_cli.py tests/test_atomic_mint_contract.py -q`

Expected: all feature tests PASS.

- [ ] **Step 7: Run static and dependency checks**

Run: `.venv/bin/python -m mypy lfg_core/xrpl_actions.py lfg_core/action_store.py lfg_core/atomic_mint.py lfg_core/xumm_ops.py lfg_service/app.py && git diff --check`

Expected: mypy exits 0 and `git diff --check` prints nothing.

- [ ] **Step 8: Run the complete regression suite**

Run: `.venv/bin/python -m pytest -q`

Expected: at least the baseline `2239 passed, 1 skipped`, plus the new action tests, with zero failures.

- [ ] **Step 9: Inspect the final diff against the design invariants**

Run:

```bash
git diff --stat main...HEAD
git diff --check main...HEAD
rg -n "OBSOLETE_BATCH_ID|BATCH_V1_1_ID|TF_ALL_OR_NOTHING|NFTokenAcceptOffer|TicketSequence|XRPL_ACTIONS_BATCH_ENABLED" lfg_core lfg_service tests docs
```

Expected: every invariant has production and test coverage; no obsolete amendment path enables the feature; no uncommitted whitespace errors.

- [ ] **Step 10: Commit final reconciliation and verification changes**

```bash
git add lfg_core/atomic_mint.py lfg_service/app.py tests/test_atomic_mint.py tests/test_actions_service.py tests/test_atomic_mint_contract.py
git commit -m "feat: reconcile atomic mint actions across restarts"
```

---

## Execution order and checkpoints

Execute Tasks 1-4 as the protocol/storage foundation, then checkpoint the diff and focused tests. Execute Tasks 5-7 as the backend vertical slice, then checkpoint the action contract without UI. Execute Tasks 8-10 as the client, standard, operations, and recovery slice, followed by the full verification gate.

Because this task is running under a no-delegation constraint unless the user explicitly requests subagents, use `superpowers:executing-plans` for inline execution in this isolated worktree.
