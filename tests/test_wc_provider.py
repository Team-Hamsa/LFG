# WalletConnect provider + ambient dispatch at the XUMM chokepoints (#447).
import asyncio

import pytest

from lfg_core import config, memos, signing, xumm_ops
from lfg_core.signing import context, store
from lfg_core.signing import walletconnect as wc

W = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"
OTHER = "rrrrrrrrrrrrrrrrrrrrrhoLvTp"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "app.db"))
    store.ensure_table()


def _memos():
    return memos.build_memos_json(
        memos.INITIATOR_USER, memos.PLATFORM_WEBAPP, memos.ACTION_TRUSTSET
    )


def test_registry_returns_walletconnect_provider():
    assert isinstance(signing.get_provider("walletconnect"), wc.WalletConnectProvider)


def test_create_stores_stamped_txjson_and_returns_wc_handle():
    with context.use("walletconnect", W):
        h = _run(
            xumm_ops._create_xumm_payload(
                {
                    "TransactionType": "TrustSet",
                    "Account": W,
                    "LimitAmount": {"currency": "USD", "issuer": OTHER, "value": "1"},
                },
                memos_json=_memos(),
            )
        )
    assert h["uuid"].startswith("wc-") and h["xumm_url"] == f"lfg-wc://{h['uuid']}"
    assert h["qr_url"] is None and h["sign_mode"] == "walletconnect" and h["push"] is None
    row = store.get(h["uuid"])
    assert row["txjson"]["SourceTag"] == config.SOURCE_TAG and row["wallet"] == W
    assert row["purpose"] == "tx"


def test_foreign_account_falls_back_to_xaman(monkeypatch):
    calls = []

    async def fake_post(payload):
        calls.append(payload)
        return {
            "qr_url": "q",
            "xumm_url": "x",
            "uuid": "11111111-1111-1111-1111-111111111111",
            "pushed": False,
        }

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    with context.use("walletconnect", W):
        h = _run(
            xumm_ops._create_xumm_payload(
                {"TransactionType": "TrustSet", "Account": OTHER}, memos_json=_memos()
            )
        )
    assert calls and h["uuid"] == "11111111-1111-1111-1111-111111111111"


def test_signin_always_goes_to_xaman(monkeypatch):
    calls = []

    async def fake_post(payload):
        calls.append(payload)
        return {
            "qr_url": "q",
            "xumm_url": "x",
            "uuid": "11111111-1111-1111-1111-111111111111",
            "pushed": False,
        }

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    with context.use("walletconnect", W):
        _run(xumm_ops._create_xumm_payload({"TransactionType": "SignIn"}))
    assert calls


def test_xaman_session_never_touches_the_store(monkeypatch):
    async def fake_post(payload):
        return {
            "qr_url": "q",
            "xumm_url": "x",
            "uuid": "11111111-1111-1111-1111-111111111111",
            "pushed": False,
        }

    monkeypatch.setattr(xumm_ops, "_post_xumm_payload", fake_post)
    h = _run(
        xumm_ops._create_xumm_payload(
            {"TransactionType": "TrustSet", "Account": W}, memos_json=_memos()
        )
    )
    assert not h["uuid"].startswith("wc-")


def test_status_maps_states():
    with context.use("walletconnect", W):
        h = _run(
            xumm_ops._create_xumm_payload(
                {"TransactionType": "TrustSet", "Account": W}, memos_json=_memos()
            )
        )
    s = _run(xumm_ops.get_payload_status(h["uuid"]))
    assert s["signed"] is False and s["expired"] is False and s["account"] == W
    assert s["txid"] is None
    store.set_state(h["uuid"], "signed", txid="ABC")
    s = _run(xumm_ops.get_payload_status(h["uuid"]))
    assert s["signed"] is True and s["txid"] == "ABC" and s["user_token"] is None
    store.set_state(h["uuid"], "rejected", expect=None)
    s = _run(xumm_ops.get_payload_status(h["uuid"]))
    assert s["signed"] is False and s["expired"] is True


def test_status_unknown_id_is_none():
    assert _run(xumm_ops.get_payload_status("wc-" + "0" * 32)) is None


def test_status_expires_stale_pending():
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ttl_seconds=-1)
    s = _run(xumm_ops.get_payload_status(row["id"]))
    assert s["expired"] is True


def test_cancel_marks_cancelled():
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    assert _run(xumm_ops.cancel_xumm_payload(row["id"])) is True
    assert store.get(row["id"])["state"] == "cancelled"


def test_provider_status_object():
    row = store.create(wallet=W, purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    st = _run(wc.WalletConnectProvider().status(row["id"]))
    assert st.signed is False and st.resolved is False and st.signer == W
