# B2/#179: xrpl_ops must distinguish committed / definitive-failure / indeterminate
# on-ledger outcomes. An INDETERMINATE outcome (submit raised and the exact tx
# hash can't be confirmed either way) must RAISE IndeterminateResultError, never
# collapse to a None that flows read as "definitely failed" and answer with an
# asset-destroying compensation. A validated-but-flaky post-hoc readback must be
# treated as committed, and a submission that raised must never be blind-
# resubmitted as a fresh (duplicate) transaction — the prior hash is looked up.

import asyncio
import os
import sys

import pytest

import lfg_core.xrpl_ops as xrpl_ops
from lfg_core import config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))


def _run(coro):
    # new_event_loop (not asyncio.run) so the policy's current loop is not
    # poisoned for later tests that rely on asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, result: dict) -> None:
        self.result = result


class _Signed:
    """Stand-in for autofill_and_sign's signed tx: only get_hash() is used."""

    def __init__(self, tx_hash: str = "TXHASH") -> None:
        self._hash = tx_hash

    def get_hash(self) -> str:
        return self._hash


def _stub_sign(monkeypatch, tx_hash: str = "TXHASH") -> None:
    monkeypatch.setattr(
        xrpl_ops, "autofill_and_sign", lambda tx, client, wallet, **k: _Signed(tx_hash)
    )


def _validated(result: str = "tesSUCCESS", tx_hash: str = "TXHASH") -> _Resp:
    return _Resp({"hash": tx_hash, "validated": True, "meta": {"TransactionResult": result}})


# --- indeterminate: submit raised AND the hash can't be confirmed on-ledger ---


def test_modify_indeterminate_raises(monkeypatch):
    _stub_sign(monkeypatch)

    def submit_boom(tx, client, wallet, **k):
        raise TimeoutError("ws closed mid-submit")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", submit_boom)
    # The follow-up hash lookup can't confirm it (never-validated / not found).
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient, "request", lambda self, req: _Resp({"error": "txnNotFound"})
    )
    with pytest.raises(xrpl_ops.IndeterminateResultError):
        _run(xrpl_ops.modify_nft("NFTID", "rOwner", "https://x/new.json"))


def test_burn_indeterminate_raises_when_lookup_itself_fails(monkeypatch):
    _stub_sign(monkeypatch)

    def submit_boom(tx, client, wallet, **k):
        raise TimeoutError("submission timed out")

    def request_boom(self, req):
        raise ConnectionError("clio unreachable")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", submit_boom)
    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request_boom)
    with pytest.raises(xrpl_ops.IndeterminateResultError):
        _run(xrpl_ops.burn_nft("NFTID", owner="rOwner"))


def test_mint_indeterminate_raises(monkeypatch):
    _stub_sign(monkeypatch)

    def submit_boom(tx, client, wallet, **k):
        raise TimeoutError("no reply")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", submit_boom)
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient, "request", lambda self, req: _Resp({"error": "txnNotFound"})
    )
    with pytest.raises(xrpl_ops.IndeterminateResultError):
        _run(xrpl_ops.mint_nft("https://x/m.json", taxon=1, issuer=config.SWAP_ISSUER_ADDRESS))


# --- committed despite a flaky post-hoc readback (the old false-failure bug) ---


def test_modify_committed_despite_flaky_readback(monkeypatch):
    # submit_and_wait already waits for validation, so its response is the
    # outcome. A separate Tx re-check must never be consulted on the success
    # path — a flake there previously turned a committed modify into a None.
    _stub_sign(monkeypatch)
    monkeypatch.setattr(
        xrpl_ops, "submit_and_wait", lambda tx, client, wallet, **k: _validated(tx_hash="H1")
    )

    def request_boom(self, req):
        raise TimeoutError("post-hoc readback flaked 5x")

    monkeypatch.setattr(xrpl_ops.JsonRpcClient, "request", request_boom)
    assert _run(xrpl_ops.modify_nft("NFTID", "rOwner", "https://x/new.json")) == "H1"


# --- never blind-resubmit: confirm the prior hash first, skip resubmit if landed ---


def test_no_resubmit_when_prior_hash_already_validated(monkeypatch):
    _stub_sign(monkeypatch, "PRIORHASH")
    calls = {"submit": 0}

    def submit_raises(tx, client, wallet, **k):
        calls["submit"] += 1
        raise TimeoutError("submission timed out (tx may still land)")

    monkeypatch.setattr(xrpl_ops, "submit_and_wait", submit_raises)
    # The prior signed tx already validated tesSUCCESS on-ledger.
    monkeypatch.setattr(
        xrpl_ops.JsonRpcClient,
        "request",
        lambda self, req: _validated(tx_hash="PRIORHASH"),
    )
    result = _run(xrpl_ops.burn_nft("NFTID", owner="rOwner"))
    assert result == "PRIORHASH"  # treated as committed, not indeterminate/failed
    assert calls["submit"] == 1  # signed once, submitted once, NEVER resubmitted


# --- definitive validated failure stays a None (compensation-safe) ---


def test_modify_definitive_failure_returns_none(monkeypatch):
    _stub_sign(monkeypatch)
    monkeypatch.setattr(
        xrpl_ops,
        "submit_and_wait",
        lambda tx, client, wallet, **k: _validated("tecNO_ENTRY", "H"),
    )
    assert _run(xrpl_ops.modify_nft("NFTID", "rOwner", "https://x/new.json")) is None


def test_burn_success_returns_hash(monkeypatch):
    _stub_sign(monkeypatch)
    monkeypatch.setattr(
        xrpl_ops, "submit_and_wait", lambda tx, client, wallet, **k: _validated(tx_hash="BH")
    )
    assert _run(xrpl_ops.burn_nft("NFTID", owner="rOwner")) == "BH"


# --- economy boundary: _economy_deps._closet_modify maps indeterminate to the
#     closet_token taxonomy so the phase-aware _sync_then_persist (#107) engages ---


def test_closet_modify_translates_indeterminate_to_closet_taxonomy(monkeypatch):
    import _economy_deps as deps

    from lfg_core import closet_token

    async def modify_indeterminate(nft_id, owner, url):
        raise xrpl_ops.IndeterminateResultError("outcome unknown")

    monkeypatch.setattr(deps.xrpl_ops, "modify_nft", modify_indeterminate)
    with pytest.raises(closet_token.ClosetIndeterminateError):
        _run(deps._closet_modify("NFTID", "rOwner", "https://x/new.json"))


def test_closet_modify_definitive_failure_stays_none(monkeypatch):
    import _economy_deps as deps

    async def modify_none(nft_id, owner, url):
        return None

    monkeypatch.setattr(deps.xrpl_ops, "modify_nft", modify_none)
    # A definitive failure must NOT masquerade as indeterminate — sync_closet
    # turns this None into a plain ClosetError (on-chain compensation safe).
    assert _run(deps._closet_modify("NFTID", "rOwner", "https://x/new.json")) is None


# --- committed mint whose meta lacks the convenience nftoken_id (#188) ---


def test_mint_committed_without_convenience_id_resolves_from_meta(monkeypatch):
    # A validated NFTokenMint whose meta omits the convenience `nftoken_id`
    # field must resolve the id from the affected nodes, not return None
    # (None reads as a definitive failure and triggers asset compensation).
    _stub_sign(monkeypatch)
    meta = {"TransactionResult": "tesSUCCESS", "AffectedNodes": []}
    monkeypatch.setattr(
        xrpl_ops, "submit_and_wait", lambda tx, client, wallet, **k: _Resp({"meta": meta})
    )
    monkeypatch.setattr(xrpl_ops, "get_nftoken_id", lambda m: "DERIVEDID")
    got = _run(xrpl_ops.mint_nft("https://x/m.json", 1763, config.SIGNING_ACCOUNT))
    assert got == "DERIVEDID"


def test_mint_committed_but_unidentifiable_raises_indeterminate(monkeypatch):
    # Validated but the id cannot be resolved at all: fail closed as
    # indeterminate, never as a definitive-failure None.
    _stub_sign(monkeypatch)
    meta = {"TransactionResult": "tesSUCCESS", "AffectedNodes": []}
    monkeypatch.setattr(
        xrpl_ops, "submit_and_wait", lambda tx, client, wallet, **k: _Resp({"meta": meta})
    )

    def _boom(m):
        raise ValueError("no nft node")

    monkeypatch.setattr(xrpl_ops, "get_nftoken_id", _boom)
    with pytest.raises(xrpl_ops.IndeterminateResultError):
        _run(xrpl_ops.mint_nft("https://x/m.json", 1763, config.SIGNING_ACCOUNT))
