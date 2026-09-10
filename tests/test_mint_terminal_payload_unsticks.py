# A payment payload that reached a terminal-not-signed state (Joey sign
# failed/rejected, or a Xaman payload expired after scan) must not leave the
# session wedged on the "approve in your wallet" spinner — live smoke
# 2026-09-10: a dry Joey wallet's failed Payment stuck the mint on "Approve
# in Xaman" until the user cancelled. qr_scanned drops so the client falls
# back to the pay panel, whose regen affordance mints a fresh payload.
import asyncio

import lfg_core.mint_flow as mint_flow

WALLET = "rSessionWallet111111111111111111"


def _session() -> mint_flow.MintSession:
    s = mint_flow.MintSession("uid", WALLET, platform="web")
    s.payment_uuid = "wc-abc"
    return s


def _status(**over):
    base = {
        "signed": False,
        "opened": True,
        "expired": False,
        "account": WALLET,
        "txid": None,
        "user_token": None,
    }
    base.update(over)
    return base


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch(monkeypatch, status):
    async def fake_status(uuid):
        return status

    monkeypatch.setattr(mint_flow.xumm_ops, "get_payload_status", fake_status)


def test_terminal_unsigned_payment_drops_qr_scanned(monkeypatch):
    session = _session()
    session.qr_scanned = True  # a previous poll saw it opened
    _patch(monkeypatch, _status(expired=True))
    _run(mint_flow.update_scan_state(session))
    assert session.qr_scanned is False
    assert session.payment_signed is False


def test_opened_pending_payment_still_marks_scanned(monkeypatch):
    session = _session()
    _patch(monkeypatch, _status())
    _run(mint_flow.update_scan_state(session))
    assert session.qr_scanned is True


def test_signed_payload_is_never_treated_terminal(monkeypatch):
    # Belt and braces: a provider that reports signed+expired (already
    # resolved) must still count as signed.
    session = _session()
    _patch(monkeypatch, _status(signed=True, expired=True, txid="AB" * 32))
    _run(mint_flow.update_scan_state(session))
    assert session.qr_scanned is True
    assert session.payment_signed is True


def test_terminal_unsigned_accept_drops_accept_scanned(monkeypatch):
    session = _session()
    session.payment_uuid = None
    session.state = mint_flow.OFFER_READY
    session.accept_uuid = "wc-def"
    session.accept_scanned = True
    _patch(monkeypatch, _status(expired=True))
    _run(mint_flow.update_scan_state(session))
    assert session.accept_scanned is False
    assert session.accept_signed is False
