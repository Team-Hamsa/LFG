# tests/test_sign_result_endpoint.py
# GET /api/sign/{id} + POST /api/sign/{id}/result (#447): the client fetches
# the stored txjson for a WalletConnect sign request and posts the outcome
# back; a claimed hash is only believed once it is VERIFIED on-ledger against
# the txjson we asked for.
import asyncio
import json
import logging
import threading
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
    # Late-signature journal records (#513) land in a per-test directory.
    monkeypatch.setattr(app.config, "ECONOMY_RECORDS_DIR", str(tmp_path / "records"))
    # A late hash posted before it validates gets one bounded background
    # re-check: poll fast, cap it short, keep the task registry per-test, and
    # never ask a real ledger for its index.
    monkeypatch.setattr(app, "_late_signature_rechecks", {})
    monkeypatch.setattr(app, "_LATE_RECHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(app, "_LATE_RECHECK_MAX_SECONDS", 0.2)

    async def _no_ledger_index():
        return None

    monkeypatch.setattr(app.xrpl_ops, "current_validated_ledger_index", _no_ledger_index)
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


def test_sweep_loop_starts_even_with_both_disabled(monkeypatch):
    # Fee cover (spec 2026-09-14) needs the loop on every stack.
    monkeypatch.setattr(app.config, "ECONOMY_ENABLED", False)
    monkeypatch.setattr(app.config, "REOWN_PROJECT_ID", "")
    started = {}

    async def _go():
        holder: dict = {}
        await app._start_settlement_sweep(holder)
        started["task"] = holder.get("settlement_sweep_task")
        if started["task"] is not None:
            started["task"].cancel()

    _run(_go())
    assert started["task"] is not None


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


# --- late signatures (#513) --------------------------------------------------
#
# Cancelling a WalletConnect request only retires OUR row (cancel_xumm_payload
# on a wc- id): the request stays open in Joey, and approving it there later
# still submits the payment. The row stays resolved and the answer stays 409,
# but a hash proving a VALIDATED, SUCCESSFUL transaction that is exactly what
# the row asked for means the wallet paid for nothing — it is journaled for a
# manual refund (owner decision D7).

_ALREADY_RESOLVED_CANCELLED = {
    "error": "already resolved",
    "code": "already_resolved",
    "state": "cancelled",
}


def _mint_payment():
    """A mint payment as the XRP path requests it: to the app's payment wallet."""
    return {
        "TransactionType": "Payment",
        "Account": DEV_OWNER,
        "Destination": app.xrpl_ops.bot_wallet_address(),
        "Amount": "5000000",
        "SourceTag": 2606160021,
        "Memos": _memos(),
    }


def _cancelled_payment_row():
    row = _row(txjson=_mint_payment())
    store.set_state(row["id"], "cancelled")  # what cancelling a wc- payload does
    return row


def _late_records(tmp_path, request_id, tx_hash):
    """Late-signature records on disk for one (request, hash) pair, matched by
    content so the test does not depend on how the files are named."""
    root = tmp_path / "records"
    if not root.is_dir():
        return []
    found = []
    for path in root.iterdir():
        if path.suffix != ".json":
            continue
        record = json.loads(path.read_text())
        if record.get("request_id") == request_id and record.get("tx_hash") == tx_hash:
            found.append(record)
    return found


def _error_logs_naming(caplog, *needles):
    return [
        rec
        for rec in caplog.records
        if rec.levelno == logging.ERROR and all(n in rec.getMessage() for n in needles)
    ]


def _assert_row_untouched(request_id):
    got = store.get(request_id)
    assert got["state"] == "cancelled"
    assert got["txid"] is None


def _not_validated(kind="pending"):
    """What `tx` answers before a submitted transaction validates."""
    if kind == "not_found":
        return {"error": "txnNotFound"}
    result = _rippled_shape(_mint_payment())  # LastLedgerSequence 99
    result["validated"] = False
    result["tx_json"]["validated"] = False
    result.pop("meta")
    return result


def _fake_tx_sequence(monkeypatch, *results):
    """get_tx answers each call with the next result; the last one repeats.
    Returns the list of hashes looked up."""
    calls = []

    async def _get_tx(tx_hash):
        calls.append(tx_hash)
        return results[min(len(calls), len(results)) - 1]

    monkeypatch.setattr(app.xrpl_ops, "get_tx", _get_tx)
    return calls


def _late_post(rid):
    return app.handle_sign_result(_Req(body={"hash": HASH}, match={"request_id": rid}))


def _post_and_settle(rid, timeout=5):
    """POST a hash, then wait out every late-signature re-check it scheduled,
    on the same loop (a re-check left pending would die with the loop)."""

    async def go():
        response = await _late_post(rid)
        pending = list(app._late_signature_rechecks.values())
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout)
        return response

    return _run(go())


def test_late_hash_on_cancelled_row_is_recorded(monkeypatch, tmp_path, caplog):
    row = _cancelled_payment_row()
    _fake_tx(monkeypatch, result=_rippled_shape(_mint_payment()))
    with caplog.at_level(logging.ERROR):
        r = _post(row["id"], {"hash": HASH.lower()})
    assert r.status == 409
    assert _body(r) == _ALREADY_RESOLVED_CANCELLED
    records = _late_records(tmp_path, row["id"], HASH)
    assert len(records) == 1
    assert records[0]["wallet"] == DEV_OWNER
    assert records[0]["amount"] == "5000000"
    assert records[0]["destination"] == app.xrpl_ops.bot_wallet_address()
    assert _error_logs_naming(caplog, row["id"], DEV_OWNER, HASH)
    _assert_row_untouched(row["id"])


@pytest.mark.parametrize("lookup", ["not_found", "pending"])
def test_late_hash_not_validated_not_recorded(monkeypatch, tmp_path, lookup):
    """Never validated within the re-check's bound: no record, and the
    re-check ends on its own."""
    row = _cancelled_payment_row()
    calls = _fake_tx_sequence(monkeypatch, _not_validated(lookup))
    r = _post_and_settle(row["id"])
    assert r.status == 409 and _body(r) == _ALREADY_RESOLVED_CANCELLED
    assert len(calls) > 1  # the post's own lookup, then the re-check's polls
    assert app._late_signature_rechecks == {}
    assert _late_records(tmp_path, row["id"], HASH) == []
    _assert_row_untouched(row["id"])


def test_late_hash_validated_on_a_later_poll_is_recorded(monkeypatch, tmp_path, caplog):
    """Joey returns the hash right after submitting, so the post usually beats
    validation. The bounded re-check records the payment once it validates."""
    row = _cancelled_payment_row()
    monkeypatch.setattr(app, "_LATE_RECHECK_MAX_SECONDS", 10)
    calls = _fake_tx_sequence(
        monkeypatch, _not_validated(), _not_validated(), _rippled_shape(_mint_payment())
    )
    with caplog.at_level(logging.ERROR):
        r = _post_and_settle(row["id"])
    assert r.status == 409 and _body(r) == _ALREADY_RESOLVED_CANCELLED
    assert len(calls) == 3
    assert len(_late_records(tmp_path, row["id"], HASH)) == 1
    assert len(_error_logs_naming(caplog, row["id"], DEV_OWNER, HASH)) == 1
    _assert_row_untouched(row["id"])


def test_late_recheck_stops_once_the_ledger_passes_last_ledger_sequence(monkeypatch, tmp_path):
    """Past its LastLedgerSequence an unvalidated transaction can never
    validate, so the re-check ends there instead of polling out its cap."""
    row = _cancelled_payment_row()
    monkeypatch.setattr(app, "_LATE_RECHECK_MAX_SECONDS", 30)
    _fake_tx_sequence(monkeypatch, _not_validated())  # LastLedgerSequence 99

    async def _ledger_index():
        return 100

    monkeypatch.setattr(app.xrpl_ops, "current_validated_ledger_index", _ledger_index)
    r = _post_and_settle(row["id"], timeout=2)  # far inside the 30 s cap
    assert r.status == 409
    assert app._late_signature_rechecks == {}
    assert _late_records(tmp_path, row["id"], HASH) == []


def test_duplicate_late_posts_share_one_recheck(monkeypatch, tmp_path, caplog):
    row = _cancelled_payment_row()
    monkeypatch.setattr(app, "_LATE_RECHECK_MAX_SECONDS", 10)
    landed = {"validated": False}
    pending, validated = _not_validated(), _rippled_shape(_mint_payment())

    async def _get_tx(_tx_hash):
        return validated if landed["validated"] else pending

    monkeypatch.setattr(app.xrpl_ops, "get_tx", _get_tx)
    started = []
    real_recheck = app._recheck_late_signature

    async def _counted_recheck(*args):
        started.append(asyncio.current_task())
        await real_recheck(*args)

    monkeypatch.setattr(app, "_recheck_late_signature", _counted_recheck)

    async def go():
        for _ in range(3):
            assert (await _late_post(row["id"])).status == 409
        await asyncio.sleep(0.05)  # let every spawned re-check start polling
        landed["validated"] = True
        await asyncio.wait_for(asyncio.gather(*started), 5)

    with caplog.at_level(logging.ERROR):
        _run(go())
    assert len(started) == 1
    assert len(_late_records(tmp_path, row["id"], HASH)) == 1
    assert len(_error_logs_naming(caplog, HASH)) == 1


def test_concurrent_late_posts_record_and_alarm_once(monkeypatch, tmp_path, caplog):
    """Two posts of one validated late hash racing into the journal: exactly
    one creates the record, so the refund alarm fires once."""
    row = _cancelled_payment_row()
    _fake_tx(monkeypatch, result=_rippled_shape(_mint_payment()))
    # Hold both writers inside the write until both have got that far, so
    # their creates really race instead of running one after the other.
    barrier = threading.Barrier(2, timeout=5)
    real_dump = json.dump

    def _racing_dump(obj, fp, **kwargs):
        barrier.wait()
        return real_dump(obj, fp, **kwargs)

    monkeypatch.setattr(app.json, "dump", _racing_dump)

    async def go():
        return await asyncio.gather(_late_post(row["id"]), _late_post(row["id"]))

    with caplog.at_level(logging.ERROR):
        responses = _run(go())
    assert [r.status for r in responses] == [409, 409]
    assert len(_late_records(tmp_path, row["id"], HASH)) == 1
    assert len(_error_logs_naming(caplog, "LATE SIGNATURE", HASH)) == 1


def test_cleanup_cancels_pending_late_rechecks(monkeypatch):
    row = _cancelled_payment_row()
    monkeypatch.setattr(app, "_LATE_RECHECK_MAX_SECONDS", 60)
    _fake_tx_sequence(monkeypatch, _not_validated())

    async def go():
        await _late_post(row["id"])
        (recheck,) = app._late_signature_rechecks.values()
        await app._stop_late_signature_rechecks(None)
        return recheck

    recheck = _run(go())
    assert recheck.cancelled()
    assert app._late_signature_rechecks == {}
    assert app._stop_late_signature_rechecks in app.create_app().on_cleanup


@pytest.mark.parametrize(
    "field,value", [("Destination", OTHER), ("Amount", "4000000")], ids=["destination", "amount"]
)
def test_late_hash_mismatched_tx_not_recorded(monkeypatch, tmp_path, field, value):
    row = _cancelled_payment_row()
    _fake_tx(monkeypatch, result=_rippled_shape(_mint_payment(), **{field: value}))
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r) == _ALREADY_RESOLVED_CANCELLED
    assert _late_records(tmp_path, row["id"], HASH) == []
    _assert_row_untouched(row["id"])


def test_late_hash_repost_idempotent(monkeypatch, tmp_path, caplog):
    row = _cancelled_payment_row()
    _fake_tx(monkeypatch, result=_rippled_shape(_mint_payment()))
    with caplog.at_level(logging.ERROR):
        first = _post(row["id"], {"hash": HASH})
        second = _post(row["id"], {"hash": HASH})
    assert first.status == second.status == 409
    assert _body(second) == _ALREADY_RESOLVED_CANCELLED
    assert len(_late_records(tmp_path, row["id"], HASH)) == 1
    # One refund alarm per payment: a repost must not read as a second charge.
    assert len(_error_logs_naming(caplog, HASH)) == 1
    _assert_row_untouched(row["id"])


def test_late_hash_that_failed_on_ledger_not_recorded(monkeypatch, tmp_path):
    """A tec result is final but moved nothing beyond the fee. The 9 late prod
    payments were all tecUNFUNDED_PAYMENT, and none of them was owed a refund."""
    row = _cancelled_payment_row()
    result = _rippled_shape(_mint_payment())
    result["meta"] = {"TransactionResult": "tecUNFUNDED_PAYMENT"}
    _fake_tx(monkeypatch, result=result)
    r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r) == _ALREADY_RESOLVED_CANCELLED
    assert _late_records(tmp_path, row["id"], HASH) == []
    _assert_row_untouched(row["id"])


def test_late_hash_lookup_failure_still_answers_409(monkeypatch, tmp_path, caplog):
    """An unreachable ledger proves nothing either way: the answer stays 409,
    and the unverifiable hash is left in the error log for a manual check."""
    row = _cancelled_payment_row()
    _fake_tx(monkeypatch, exc=RuntimeError("rpc down"))
    with caplog.at_level(logging.ERROR):
        r = _post(row["id"], {"hash": HASH})
    assert r.status == 409 and _body(r) == _ALREADY_RESOLVED_CANCELLED
    assert _late_records(tmp_path, row["id"], HASH) == []
    assert _error_logs_naming(caplog, row["id"], HASH)
    _assert_row_untouched(row["id"])
