# tests/test_sign_result_endpoint.py
# GET /api/sign/{id} + POST /api/sign/{id}/result (#447): the client fetches
# the stored txjson for a WalletConnect sign request and posts the outcome
# back; a claimed hash is only believed once it is VERIFIED on-ledger against
# the txjson we asked for.
import asyncio
import json
import time

import pytest
from xrpl.wallet import Wallet

import lfg_service.app as app
from lfg_core import memos
from lfg_core.signing import store

DEV_OWNER = Wallet.create().classic_address
OTHER = Wallet.create().classic_address
HASH = "A" * 64
RIPPLE_EPOCH = 946684800


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Req:
    def __init__(self, body=None, match=None):
        self._body = body or {}
        self.headers: dict = {}
        self.match_info = match or {}
        self.remote = "1.2.3.4"
        self._store: dict = {}

    async def json(self):
        return self._body

    def __getitem__(self, k):
        return self._store[k]

    def __setitem__(self, k, v):
        self._store[k] = v


def _body(resp):
    return json.loads(resp.text)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(app.config, "WEBAPP_DEV_MODE", True)
    monkeypatch.setattr(app.mock_economy, "DEV_OWNER", DEV_OWNER, raising=False)
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "sign.db"))
    store.ensure_table()


TXJSON = {
    "TransactionType": "TrustSet",
    "Account": DEV_OWNER,
    "LimitAmount": {"currency": "USD", "issuer": OTHER, "value": "100"},
    "SourceTag": 2606160021,
    "Memos": [{"Memo": {"MemoData": "AB"}}],
}


def _row(wallet=DEV_OWNER, txjson=None, ttl=900, purpose="tx"):
    return store.create(
        wallet=wallet,
        purpose=purpose,
        txjson=TXJSON if txjson is None else txjson,
        nonce=None,
        ttl_seconds=ttl,
    )


def _fake_tx(monkeypatch, result=None, exc=None):
    async def _get_tx(tx_hash):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(app.xrpl_ops, "get_tx", _get_tx)


def _onledger(**over):
    tx = dict(TXJSON)
    tx.update(
        {
            "Fee": "12",
            "Sequence": 4,
            "SigningPubKey": "ED00",
            "hash": HASH,
            "date": int(time.time()) - RIPPLE_EPOCH + 5,
        }
    )
    tx.update(over)
    return {"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}, "tx_json": tx}


# --- GET -------------------------------------------------------------------


def test_get_returns_txjson():
    row = _row()
    r = _run(app.handle_sign_request(_Req(match={"request_id": row["id"]})))
    assert r.status == 200
    b = _body(r)
    assert b["id"] == row["id"] and b["state"] == "pending" and b["txjson"] == TXJSON
    assert b["expires_at"] > time.time()


def test_get_foreign_row_is_404():
    row = _row(wallet=OTHER)
    r = _run(app.handle_sign_request(_Req(match={"request_id": row["id"]})))
    assert r.status == 404


def test_get_unknown_is_404():
    r = _run(app.handle_sign_request(_Req(match={"request_id": "wc-" + "0" * 32})))
    assert r.status == 404


def test_get_expires_stale_row_first():
    row = _row(ttl=-1)
    r = _run(app.handle_sign_request(_Req(match={"request_id": row["id"]})))
    assert _body(r)["state"] == "expired"


# --- POST result -----------------------------------------------------------


def _post(rid, body):
    return _run(app.handle_sign_result(_Req(body=body, match={"request_id": rid})))


def test_validated_match_signs(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger())
    r = _post(row["id"], {"hash": HASH.lower()})
    assert r.status == 200 and _body(r) == {"state": "signed", "txid": HASH}
    got = store.get(row["id"])
    assert got["state"] == "signed" and got["txid"] == HASH
    assert got["result"]["meta_result"] == "tesSUCCESS"


def test_flat_tx_shape_without_tx_json(monkeypatch):
    row = _row()
    res = _onledger()
    flat = dict(res.pop("tx_json"))
    flat.update(res)
    _fake_tx(monkeypatch, result=flat)
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 200 and _body(r)["state"] == "signed"


def test_semantic_mismatch_flags_row(monkeypatch):
    row = _row()
    _fake_tx(
        monkeypatch,
        result=_onledger(LimitAmount={"currency": "USD", "issuer": OTHER, "value": "999"}),
    )
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"
    assert store.get(row["id"])["state"] == "mismatch"


def test_foreign_account_is_mismatch(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger(Account=OTHER))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409
    assert store.get(row["id"])["state"] == "mismatch"


def test_wrong_transaction_type_is_mismatch(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger(TransactionType="Payment"))
    assert _post(row["id"], {"hash": HASH}).status == 409


def test_autofill_fields_do_not_break_match(monkeypatch):
    row = _row(txjson={**TXJSON, "Fee": "10", "Sequence": 1, "Flags": 0})
    _fake_tx(monkeypatch, result=_onledger())
    assert _post(row["id"], {"hash": HASH}).status == 200


def test_nonzero_stored_flags_are_compared(monkeypatch):
    row = _row(txjson={**TXJSON, "Flags": 131072})
    _fake_tx(monkeypatch, result=_onledger(Flags=0))
    assert _post(row["id"], {"hash": HASH}).status == 409


def test_fully_canonical_sig_flag_is_masked(monkeypatch):
    row = _row(txjson={**TXJSON, "Flags": 1})
    _fake_tx(monkeypatch, result=_onledger(Flags=0x80000001))
    assert _post(row["id"], {"hash": HASH}).status == 200


def test_wallet_may_add_only_fully_canonical_sig(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger(Flags=0x80000000))
    assert _post(row["id"], {"hash": HASH}).status == 200


def test_wallet_added_flag_is_mismatch(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger(Flags=0x00020000))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"


def test_added_ledger_field_is_mismatch(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger(Expiration=99))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"
    assert store.get(row["id"])["state"] == "mismatch"


def test_added_destination_is_mismatch(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger(Destination=OTHER))
    assert _post(row["id"], {"hash": HASH}).status == 409


def test_deliver_max_is_read_as_amount(monkeypatch):
    payment = {
        "TransactionType": "Payment",
        "Account": DEV_OWNER,
        "Destination": OTHER,
        "Amount": "100",
    }
    row = _row(txjson=payment)
    res = _onledger()
    tx = dict(payment)
    tx.pop("Amount")
    tx.update({"DeliverMax": "100", "date": res["tx_json"]["date"], "hash": HASH})
    _fake_tx(monkeypatch, result={**res, "tx_json": tx})
    assert _post(row["id"], {"hash": HASH}).status == 200


def test_hash_already_claimed_by_another_row_is_mismatch(monkeypatch):
    first = _row()
    _fake_tx(monkeypatch, result=_onledger())
    assert _post(first["id"], {"hash": HASH}).status == 200
    second = _row()
    r = _post(second["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"
    assert store.get(second["id"])["state"] == "mismatch"


def test_tx_older_than_the_request_is_mismatch(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger(date=int(time.time()) - RIPPLE_EPOCH - 3600))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"


def test_close_time_iso_is_preferred(monkeypatch):
    row = _row()
    res = _onledger()
    res["tx_json"].pop("date")
    res["close_time_iso"] = "2100-01-01T00:00:00Z"
    _fake_tx(monkeypatch, result=res)
    assert _post(row["id"], {"hash": HASH}).status == 200


def test_no_timestamp_fails_closed(monkeypatch):
    row = _row()
    res = _onledger()
    res["tx_json"].pop("date")
    _fake_tx(monkeypatch, result=res)
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"


def test_lost_cas_answers_the_rows_real_state(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger())
    real_set_state = store.set_state

    def _racing(request_id, state, **kw):
        # Simulate a concurrent post resolving the row a moment earlier.
        real_set_state(request_id, "rejected")
        return real_set_state(request_id, state, **kw)

    monkeypatch.setattr(store, "set_state", _racing)
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["state"] == "rejected"


def test_not_yet_validated_is_202(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result={"error": "txnNotFound"})
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 202 and _body(r) == {"state": "pending", "code": "tx_not_found"}
    assert store.get(row["id"])["state"] == "pending"


def test_not_validated_past_deadline_expires(monkeypatch):
    row = _row(ttl=-1)
    _fake_tx(monkeypatch, result={"error": "txnNotFound"})
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 410 and _body(r)["code"] == "tx_not_found"
    assert store.get(row["id"])["state"] == "expired"


def test_ledger_lookup_failure_is_503(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, exc=RuntimeError("rpc down"))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 503 and _body(r)["code"] == "ledger_unavailable"
    assert store.get(row["id"])["state"] == "pending"


def test_rejected_body():
    row = _row()
    r = _post(row["id"], {"rejected": True})
    assert r.status == 200 and _body(r)["state"] == "rejected"
    assert store.get(row["id"])["state"] == "rejected"


def test_error_body_records_reason():
    row = _row()
    r = _post(row["id"], {"error": "x" * 500})
    assert r.status == 200 and _body(r)["state"] == "failed"
    got = store.get(row["id"])
    assert got["state"] == "failed" and len(got["result"]["error"]) == 200


def test_empty_body_is_400():
    row = _row()
    assert _post(row["id"], {}).status == 400


def test_bad_hash_is_400():
    row = _row()
    r = _post(row["id"], {"hash": "nope"})
    assert r.status == 400 and _body(r)["code"] == "bad_request"


def test_foreign_row_is_403():
    row = _row(wallet=OTHER)
    r = _post(row["id"], {"rejected": True})
    assert r.status == 403 and _body(r)["code"] == "not_your_request"


def test_unknown_row_is_404():
    assert _post("wc-" + "0" * 32, {"rejected": True}).status == 404


def test_non_tx_purpose_is_404():
    row = _row(purpose="signin", txjson=None)
    assert _post(row["id"], {"rejected": True}).status == 404


def test_idempotent_repost_of_same_hash(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger())
    assert _post(row["id"], {"hash": HASH}).status == 200
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 200 and _body(r) == {"state": "signed", "txid": HASH}


def test_conflicting_repost_is_409(monkeypatch):
    row = _row()
    _fake_tx(monkeypatch, result=_onledger())
    _post(row["id"], {"hash": HASH})
    r = _post(row["id"], {"hash": "B" * 64})
    assert r.status == 409 and _body(r)["code"] == "already_resolved"


# --- sweep + routes --------------------------------------------------------


def test_sweep_expires_and_prunes():
    stale = _row(ttl=-1)
    old_done = _row()
    store.set_state(old_done["id"], "signed")
    conn = store._conn()
    conn.execute(
        "UPDATE sign_requests SET created_at = ? WHERE id = ?",
        (time.time() - 8 * 86400, old_done["id"]),
    )
    conn.commit()
    conn.close()
    fresh = _row()
    _run(app.sweep_sign_requests())
    assert store.get(stale["id"])["state"] == "expired"
    assert store.get(old_done["id"]) is None
    assert store.get(fresh["id"])["state"] == "pending"


def test_routes_registered():
    routes = {
        (r.method, r.resource.canonical) for r in app.create_app().router.routes() if r.resource
    }
    assert ("GET", "/api/sign/{request_id}") in routes
    assert ("POST", "/api/sign/{request_id}/result") in routes


def test_sweep_loop_starts_with_economy_off_and_wc_on(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "pid")
    started = {}

    async def _go():
        holder: dict = {}
        await app._start_settlement_sweep(holder)
        started["task"] = holder.get("settlement_sweep_task")
        if started["task"] is not None:
            started["task"].cancel()

    _run(_go())
    assert started["task"] is not None


def test_sweep_loop_stays_off_with_both_disabled(monkeypatch):
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    holder: dict = {}
    _run(app._start_settlement_sweep(holder))
    assert "settlement_sweep_task" not in holder


# --- rippled's own encoding is not tampering (#447 review) ------------------
#
# The `tx` RPC hands blob fields back UPPER-case hex and canonicalizes IOU
# `value` strings, while `memos.build_memos_json` writes LOWER-case hex. A raw
# `!=` between the two flagged EVERY honest Joey transaction `mismatch`, so
# these fixtures are deliberately NOT copies of the stored dict: they are
# rebuilt the way a real server returns them.


def _rippled_shape(stored, **over):
    """`stored` as rippled's `tx` RPC would hand it back."""
    tx = json.loads(json.dumps(stored))

    def _upper(node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key in ("MemoType", "MemoData", "MemoFormat", "URI", "InvoiceID") and isinstance(
                    value, str
                ):
                    node[key] = value.upper()
                elif key == "currency" and isinstance(value, str) and len(value) == 40:
                    node[key] = value.upper()
                else:
                    _upper(value)
        elif isinstance(node, list):
            for item in node:
                _upper(item)

    _upper(tx)
    tx.update(
        {
            "Fee": "12",
            "Sequence": 4,
            "LastLedgerSequence": 99,
            "Flags": 0x80000000,
            "SigningPubKey": "ED00",
            "TxnSignature": "3045FF",
            "hash": HASH,
            "ctid": "C005D1EC00000000",
            "date": int(time.time()) - RIPPLE_EPOCH + 5,
            "ledger_index": 90,
            "inLedger": 90,
            "validated": True,
        }
    )
    tx.update(over)
    return {
        "validated": True,
        "ledger_index": 90,
        "hash": HASH,
        "ctid": "C005D1EC00000000",
        "meta": {"TransactionResult": "tesSUCCESS"},
        "tx_json": tx,
    }


def _memos():
    return memos.build_memos_json("user", "webapp", "payment")


def test_memos_we_write_are_lower_case_hex():
    # The premise of the fix: our side is lower-case, rippled's is upper-case.
    memo = _memos()[0]["Memo"]
    assert memo["MemoType"] != memo["MemoType"].upper()


def test_xrp_payment_survives_rippled_encoding(monkeypatch):
    payment = {
        "TransactionType": "Payment",
        "Account": DEV_OWNER,
        "Destination": OTHER,
        "Amount": "1000000",
        "SourceTag": 2606160021,
        "Memos": _memos(),
    }
    row = _row(txjson=payment)
    _fake_tx(monkeypatch, result=_rippled_shape(payment, Amount="1000000"))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 200 and _body(r)["state"] == "signed"


def test_iou_trustset_survives_value_canonicalization(monkeypatch):
    trustset = {
        "TransactionType": "TrustSet",
        "Account": DEV_OWNER,
        "LimitAmount": {
            "currency": "4252495800000000000000000000000000000000",
            "issuer": OTHER,
            "value": "5",
        },
        "Memos": _memos(),
    }
    row = _row(txjson=trustset)
    onledger = _rippled_shape(trustset)
    # rippled re-renders the amount: "5" comes back as "5.0", the currency hex
    # upper-cased by _rippled_shape.
    onledger["tx_json"]["LimitAmount"]["value"] = "5.0"
    _fake_tx(monkeypatch, result=onledger)
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 200 and _body(r)["state"] == "signed"


def test_nftoken_create_offer_with_memos_survives(monkeypatch):
    offer = {
        "TransactionType": "NFTokenCreateOffer",
        "Account": DEV_OWNER,
        "NFTokenID": "000800001234567890abcdef1234567890abcdef1234567890abcdef12345678",
        "Amount": "0100",
        "Flags": 1,
        "Memos": _memos(),
    }
    row = _row(txjson=offer)
    _fake_tx(monkeypatch, result=_rippled_shape(offer, Flags=0x80000001))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 200 and _body(r)["state"] == "signed"


def test_changed_memo_data_is_still_a_mismatch(monkeypatch):
    payment = {
        "TransactionType": "Payment",
        "Account": DEV_OWNER,
        "Destination": OTHER,
        "Amount": "1000000",
        "Memos": _memos(),
    }
    row = _row(txjson=payment)
    onledger = _rippled_shape(payment)
    onledger["tx_json"]["Memos"][0]["Memo"]["MemoData"] = "DEADBEEF"
    _fake_tx(monkeypatch, result=onledger)
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"
    assert store.get(row["id"])["state"] == "mismatch"


def test_different_iou_value_is_still_a_mismatch(monkeypatch):
    row = _row()
    res = _onledger()
    res["tx_json"]["LimitAmount"] = {"currency": "USD", "issuer": OTHER, "value": "100.5"}
    _fake_tx(monkeypatch, result=res)
    assert _post(row["id"], {"hash": HASH}).status == 409


# --- validated but not successful / long abandoned --------------------------


def test_tec_result_is_failed_not_signed(monkeypatch):
    row = _row()
    res = _onledger()
    res["meta"] = {"TransactionResult": "tecUNFUNDED_PAYMENT"}
    _fake_tx(monkeypatch, result=res)
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409
    assert _body(r) == {
        "error": "transaction failed on-ledger",
        "code": "tx_failed",
        "state": "failed",
    }
    got = store.get(row["id"])
    assert got["state"] == "failed" and got["result"]["meta_result"] == "tecUNFUNDED_PAYMENT"
    assert got["txid"] is None


def test_result_posted_long_after_expiry_is_410(monkeypatch):
    row = _row(ttl=-(app._SIGN_RESULT_GRACE_SECONDS + 30))
    _fake_tx(monkeypatch, result=_onledger())
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 410 and _body(r)["code"] == "expired"
    assert store.get(row["id"])["state"] == "expired"


def test_result_just_inside_the_grace_still_signs(monkeypatch):
    row = _row(ttl=-5)
    _fake_tx(monkeypatch, result=_onledger())
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 200 and _body(r)["state"] == "signed"


def test_lost_txid_race_still_ends_mismatch(monkeypatch):
    """txid_in_use is only a pre-check. If it comes back clean because another
    post claimed the hash a microsecond later, the unique index refuses the
    write and the loser must still land `mismatch` + 409 — never a second
    `signed` row on one transaction."""
    first = _row()
    _fake_tx(monkeypatch, result=_onledger())
    assert _post(first["id"], {"hash": HASH}).status == 200
    second = _row()
    monkeypatch.setattr(store, "txid_in_use", lambda *a, **kw: False)
    r = _post(second["id"], {"hash": HASH})
    assert r.status == 409 and _body(r)["code"] == "tx_mismatch"
    assert store.get(second["id"])["state"] == "mismatch"
    assert store.get(second["id"])["txid"] is None
    assert store.get(first["id"])["state"] == "signed"
    assert store.get(first["id"])["txid"] == HASH
