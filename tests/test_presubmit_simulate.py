"""Pre-submit `simulate` pre-flight on backend-signed transactions (#58).

`xrpl_ops._presubmit_simulate` runs rippled's `simulate` on the UNSIGNED
model before anything is signed. A deterministic engine result (tem*/tef*/
tec*) short-circuits the submit — nothing is signed, no fee is burned, and
`_submit_and_confirm` returns the existing `None` "definitive failure"
signal. Everything else (transport errors, unknown shapes, tes/ter/tel)
degrades OPEN: the ledger stays the authority. `PRESUBMIT_SIMULATE=0` is a
kill switch read at call time (the root conftest pins it to 0 so the
existing stub-based suites never reach the network)."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from xrpl.asyncio.clients import XRPLRequestFailureException
from xrpl.models.transactions import NFTokenBurn

from lfg_core import config, xrpl_ops

ACCOUNT = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tx() -> NFTokenBurn:
    return NFTokenBurn(account=ACCOUNT, nftoken_id="00" * 32, source_tag=config.SOURCE_TAG)


def _fake_simulate(engine_result=None, *, exc=None, result=None):
    calls: list = []

    def fake(tx, client, *, binary=False):
        calls.append(tx)
        if exc is not None:
            raise exc
        if result is not None:
            return SimpleNamespace(result=result)
        return SimpleNamespace(result={"engine_result": engine_result, "applied": False})

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


def test_shipped_default_is_on():
    assert config.PRESUBMIT_SIMULATE_DEFAULT == "1"


@pytest.mark.parametrize("code", ["tesSUCCESS", "terQUEUED", "telINSUF_FEE_P"])
def test_non_deterministic_results_proceed(monkeypatch, code):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    fake = _fake_simulate(code)
    with patch.object(xrpl_ops, "simulate", fake):
        assert _run(xrpl_ops._presubmit_simulate(_tx(), None, "t")) is None
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "code",
    ["temREDUNDANT", "tefNFTOKEN_IS_NOT_TRANSFERABLE", "tecPATH_DRY", "tecUNFUNDED_PAYMENT"],
)
def test_deterministic_results_reject(monkeypatch, code):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    with patch.object(xrpl_ops, "simulate", _fake_simulate(code)):
        assert _run(xrpl_ops._presubmit_simulate(_tx(), None, "t")) == code


def test_unclassified_prefix_proceeds(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    with patch.object(xrpl_ops, "simulate", _fake_simulate("txWEIRD")):
        assert _run(xrpl_ops._presubmit_simulate(_tx(), None, "t")) is None


@pytest.mark.parametrize(
    "exc",
    [
        XRPLRequestFailureException({"error": "transactionSigned"}),
        ConnectionError("boom"),
        TimeoutError(),
    ],
)
def test_request_failures_degrade_open(monkeypatch, caplog, exc):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    with (
        patch.object(xrpl_ops, "simulate", _fake_simulate(exc=exc)),
        caplog.at_level(logging.WARNING),
    ):
        assert _run(xrpl_ops._presubmit_simulate(_tx(), None, "t")) is None
    assert any("simulate" in r.message for r in caplog.records)


def test_missing_engine_result_degrades_open(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    with patch.object(xrpl_ops, "simulate", _fake_simulate(result={"status": "success"})):
        assert _run(xrpl_ops._presubmit_simulate(_tx(), None, "t")) is None


def test_flag_off_never_calls_simulate(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "0")
    fake = _fake_simulate("tecPATH_DRY")
    with patch.object(xrpl_ops, "simulate", fake):
        assert _run(xrpl_ops._presubmit_simulate(_tx(), None, "t")) is None
    assert fake.calls == []


# --- wiring: _submit_and_confirm -------------------------------------------


def _never(*a, **k):
    raise AssertionError("must not be called after a simulate rejection")


def test_submit_and_confirm_short_circuits_on_rejection(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    tx = _tx()
    fake = _fake_simulate("tecNO_PERMISSION")
    wallet = SimpleNamespace(classic_address=ACCOUNT)
    with (
        patch.object(xrpl_ops, "simulate", fake),
        patch.object(xrpl_ops, "autofill_and_sign", _never),
        patch.object(xrpl_ops, "submit_and_wait", _never),
    ):
        # Returns None (definitive failure), never IndeterminateResultError.
        assert _run(xrpl_ops._submit_and_confirm(tx, wallet, None, "NFTokenBurn")) is None
    # The simulated object is the very unsigned model the caller built.
    assert fake.calls == [tx]
    assert not tx.is_signed()


def test_submit_and_confirm_simulates_before_lock_then_proceeds(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    order: list[str] = []

    def fake_sim(tx, client, *, binary=False):
        order.append("simulate")
        return SimpleNamespace(result={"engine_result": "tesSUCCESS"})

    real_scope = xrpl_ops._submission_scope

    def recording_scope(account, coordinator_held):
        order.append("lock")
        return real_scope(account, coordinator_held)

    def fake_sign(tx, client, wallet):
        order.append("sign")
        return SimpleNamespace(get_hash=lambda: "H1")

    def fake_submit(signed, client, wallet, autofill):
        order.append("submit")
        return SimpleNamespace(
            result={"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}
        )

    wallet = SimpleNamespace(classic_address=ACCOUNT)
    with (
        patch.object(xrpl_ops, "simulate", fake_sim),
        patch.object(xrpl_ops, "_submission_scope", recording_scope),
        patch.object(xrpl_ops, "autofill_and_sign", fake_sign),
        patch.object(xrpl_ops, "submit_and_wait", fake_submit),
    ):
        res = _run(xrpl_ops._submit_and_confirm(_tx(), wallet, None, "NFTokenBurn"))
    assert res is not None and res["hash"] == "H1"
    assert order == ["simulate", "lock", "sign", "submit"]


def test_submit_and_confirm_flag_off_unchanged(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "0")
    wallet = SimpleNamespace(classic_address=ACCOUNT)
    with (
        patch.object(xrpl_ops, "simulate", _never),
        patch.object(
            xrpl_ops,
            "autofill_and_sign",
            lambda tx, c, w: SimpleNamespace(get_hash=lambda: "H2"),
        ),
        patch.object(
            xrpl_ops,
            "submit_and_wait",
            lambda s, c, w, autofill: SimpleNamespace(
                result={"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}}
            ),
        ),
    ):
        res = _run(xrpl_ops._submit_and_confirm("tx1", wallet, None, "x"))
    assert res is not None and res["hash"] == "H2"


# --- wiring: sponsored prepare paths ---------------------------------------


def test_prepare_sponsored_mint_rejection_fails_without_signing(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")

    async def floor(client):
        return 100

    with (
        patch.object(xrpl_ops, "simulate", _fake_simulate("tecUNFUNDED_PAYMENT")),
        patch.object(xrpl_ops, "_current_validated_ledger_index", floor),
        patch.object(xrpl_ops, "autofill_and_sign", _never),
    ):
        prep = _run(
            xrpl_ops.prepare_sponsored_mint(
                "https://cdn.example/meta.json", 0, ACCOUNT, campaign="c1"
            )
        )
    assert prep.state == "failed"
    assert prep.tx_blob is None
    assert "tecUNFUNDED_PAYMENT" in (prep.error or "")


def test_prepare_sponsored_mint_tes_proceeds(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")

    async def floor(client):
        return 100

    with (
        patch.object(xrpl_ops, "simulate", _fake_simulate("tesSUCCESS")),
        patch.object(xrpl_ops, "_current_validated_ledger_index", floor),
        patch.object(
            xrpl_ops,
            "autofill_and_sign",
            lambda tx, c, w: SimpleNamespace(get_hash=lambda: "H3", blob=lambda: "BLOB"),
        ),
    ):
        prep = _run(
            xrpl_ops.prepare_sponsored_mint(
                "https://cdn.example/meta.json", 0, ACCOUNT, campaign="c1"
            )
        )
    assert prep.state == "prepared"
    assert prep.tx_hash == "H3"


def test_prepare_sponsored_burn_rejection_fails_without_signing(monkeypatch):
    monkeypatch.setenv("PRESUBMIT_SIMULATE", "1")
    monkeypatch.setattr(config, "XRPL_NETWORK", "testnet")

    async def floor(client):
        return 100

    with (
        patch.object(xrpl_ops, "simulate", _fake_simulate("tecPATH_DRY")),
        patch.object(xrpl_ops, "_current_validated_ledger_index", floor),
        patch.object(xrpl_ops, "autofill_and_sign", _never),
    ):
        prep = _run(
            xrpl_ops.prepare_sponsored_burn(
                "m1",
                amount="1",
                source_account=ACCOUNT,
                issuer="rrrrrrrrrrrrrrrrrrrrBZbvji",
            )
        )
    assert prep.state == "failed"
    assert prep.tx_blob is None
    assert "tecPATH_DRY" in (prep.error or "")
