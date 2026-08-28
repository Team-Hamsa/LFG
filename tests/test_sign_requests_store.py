import time

import pytest

from lfg_core.signing import store


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE", str(tmp_path / "app.db"))
    store.ensure_table()


def test_create_and_get_round_trip():
    row = store.create(
        wallet="rA",
        purpose="tx",
        txjson={"TransactionType": "Payment"},
        nonce=None,
        ttl_seconds=900,
        ip="1.1.1.1",
    )
    assert row["id"].startswith("wc-") and len(row["id"]) == 3 + 32
    got = store.get(row["id"])
    assert got["state"] == "pending" and got["txjson"] == {"TransactionType": "Payment"}
    assert got["expires_at"] > time.time() + 800


def test_get_unknown_is_none():
    assert store.get("wc-nope") is None


def test_set_state_is_compare_and_set():
    row = store.create(wallet="rA", purpose="signin", txjson=None, nonce="abc", ttl_seconds=300)
    assert store.set_state(row["id"], "consumed") is True
    assert store.set_state(row["id"], "consumed") is False  # already consumed
    assert store.get(row["id"])["state"] == "consumed"


def test_set_state_records_txid_and_result():
    row = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    assert store.set_state(row["id"], "signed", txid="ABC", result={"ok": 1})
    got = store.get(row["id"])
    assert got["txid"] == "ABC" and got["result"] == {"ok": 1}


def test_expire_stale_flips_only_pending_past_deadline():
    old = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=1)
    fresh = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    done = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=1)
    store.set_state(done["id"], "signed")
    assert store.expire_stale(now=time.time() + 5) == 1
    assert store.get(old["id"])["state"] == "expired"
    assert store.get(fresh["id"])["state"] == "pending"
    assert store.get(done["id"])["state"] == "signed"


def test_delete_terminal_older_than_spares_pending_and_fresh():
    old_done = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    old_pending = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    fresh_done = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    store.set_state(old_done["id"], "signed")
    store.set_state(fresh_done["id"], "signed")
    conn = store._conn()
    conn.execute(
        "UPDATE sign_requests SET created_at = ? WHERE id IN (?, ?)",
        (time.time() - 8 * 86400, old_done["id"], old_pending["id"]),
    )
    conn.commit()
    conn.close()
    assert store.delete_terminal_older_than(seconds=7 * 86400) == 1
    assert store.get(old_done["id"]) is None
    assert store.get(old_pending["id"])["state"] == "pending"
    assert store.get(fresh_done["id"])["state"] == "signed"


def test_txid_in_use_ignores_the_excluded_row():
    a = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    b = store.create(wallet="rB", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    store.set_state(a["id"], "signed", txid="DEAD")
    assert store.txid_in_use("DEAD") is True
    assert store.txid_in_use("DEAD", exclude_id=a["id"]) is False
    assert store.txid_in_use("DEAD", exclude_id=b["id"]) is True
    assert store.txid_in_use("BEEF") is False


def test_a_txid_can_only_be_claimed_once():
    """The unique index — not the txid_in_use pre-check — is what makes the
    hash claim atomic: two rows can never both settle on one transaction."""
    a = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    b = store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
    assert store.set_state(a["id"], "signed", txid="H" * 64) is True
    with pytest.raises(store.TxidClaimed):
        store.set_state(b["id"], "signed", txid="H" * 64)
    assert store.get(b["id"])["state"] == "pending"


def test_many_rows_may_hold_a_null_txid():
    rows = [
        store.create(wallet="rA", purpose="tx", txjson={}, nonce=None, ttl_seconds=900)
        for _ in range(3)
    ]
    for r in rows:
        assert store.set_state(r["id"], "rejected") is True
