# tests/test_mint_signer_mismatch.py
# Issue #275: a mint payment signed by a different Xaman account than the
# session wallet was silently dropped — the ledger watch only matches
# expected_sender == session wallet, so the session could only ever
# payment_timeout while the wrong wallet's money had already moved. #314
# pins Account on the payload (Xaman refuses to sign from another account);
# this is the defense-in-depth layer the issue asks for: poll the payload,
# compare the signer, and fail loudly (state=failed, reason=signer_mismatch,
# WARNING log with the txid) — mirroring market_flow's signer_mismatch guard.
#
# Env-guard preamble (verbatim from tests/test_swap_cross_body_api.py):
# importing lfg_core.config freezes its constants (e.g. LAYER_SOURCE,
# BUNNY_PULL_ZONE) at import time; set the same defaults test_smoke.py uses
# so collection order can't strand them.
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import logging  # noqa: E402

import lfg_core.mint_flow as mint_flow  # noqa: E402

WALLET = "rSessionWallet111111111111111111"
OTHER = "rOtherSigner22222222222222222222"


def _session() -> mint_flow.MintSession:
    s = mint_flow.MintSession("uid", WALLET, platform="discord-activity")
    s.payment_uuid = "payload-uuid"
    return s


def _payload_status(**over):
    base = {
        "signed": True,
        "opened": True,
        "expired": False,
        "account": WALLET,
        "txid": "ABCD1234",
        "user_token": "tok-1",
    }
    base.update(over)
    return base


def _run(coro):
    # Repo pattern (tests/test_mint_cancel.py): a private loop instead of
    # asyncio.run, which poisons the global event loop policy for later
    # tests that call asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_status(monkeypatch, status):
    async def fake_status(uuid):
        return status

    monkeypatch.setattr(mint_flow.xumm_ops, "get_payload_status", fake_status)


def test_mismatched_signer_fails_session_and_cancels_task(monkeypatch, caplog):
    _patch_status(monkeypatch, _payload_status(account=OTHER))
    session = _session()

    async def scenario():
        session.task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        with caplog.at_level(logging.WARNING):
            await mint_flow.update_scan_state(session)
        await asyncio.sleep(0)  # let the cancellation propagate

    _run(scenario())

    assert session.state == mint_flow.FAILED
    assert session.reason == "signer_mismatch"
    assert session.error and OTHER in session.error
    # push token from the wrong signer must NOT be captured
    assert session.issued_user_token is None
    # background wait_for_payment is stopped
    assert session.task.cancelled() or session.task.done()
    # ops log carries signer + txid for reconciliation
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert OTHER in joined and "ABCD1234" in joined


def test_mismatched_signer_releases_headroom_reservation(monkeypatch, tmp_path):
    # CodeRabbit on PR #348: the base mismatch test carries no headroom
    # reservation, so the release path was unverified. Take a real 1-unit
    # reservation (the same claimant shape handle_mint_start uses) and assert
    # the signer-mismatch guard settles it — the cancelled task may never run
    # its finally, so update_scan_state must release directly, like cancel().
    from lfg_core import db_path, headroom, supply

    db = str(tmp_path / "app.db")
    monkeypatch.setattr(db_path, "app_db_path", lambda net=None: db)
    monkeypatch.setattr(mint_flow.db_path, "app_db_path", lambda net=None: db)
    monkeypatch.setattr(supply, "current_supply", lambda net: 0)
    monkeypatch.setattr(headroom.nft_index, "index_db_path", lambda net: str(tmp_path / "idx.db"))

    _patch_status(monkeypatch, _payload_status(account=OTHER))
    session = _session()
    claimant = f"mint:{session.id}"
    granted = headroom.try_reserve(db, claimant, 1, mint_flow.config.XRPL_NETWORK)
    assert granted == 1
    session.headroom_reserved = True
    assert headroom.outstanding(db) == 1

    async def scenario():
        session.task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        await mint_flow.update_scan_state(session)
        await asyncio.sleep(0)  # let the cancellation propagate

    _run(scenario())

    assert session.state == mint_flow.FAILED
    assert session.reason == "signer_mismatch"
    assert session.task.cancelled() or session.task.done()
    # the reservation is released outright (no mint landed) and the flag drops
    assert session.headroom_reserved is False
    assert headroom.outstanding(db) == 0


def test_matching_signer_unchanged_behavior(monkeypatch):
    _patch_status(monkeypatch, _payload_status())
    session = _session()
    _run(mint_flow.update_scan_state(session))

    assert session.state == mint_flow.AWAITING_PAYMENT
    assert session.payment_signed is True
    assert session.qr_scanned is True
    assert session.reason is None
    assert session.issued_user_token == "tok-1"


def test_unsigned_payload_no_mismatch_fire(monkeypatch):
    # An unsigned payload (opened by another device, not yet signed) must not
    # trip the guard — account can be present pre-signature.
    _patch_status(
        monkeypatch, _payload_status(signed=False, account=OTHER, txid=None, user_token=None)
    )
    session = _session()
    _run(mint_flow.update_scan_state(session))

    assert session.state == mint_flow.AWAITING_PAYMENT
    assert session.reason is None


def test_to_dict_exposes_reason():
    session = _session()
    d = session.to_dict()
    assert "reason" in d and d["reason"] is None
    session.reason = "signer_mismatch"
    assert session.to_dict()["reason"] == "signer_mismatch"
