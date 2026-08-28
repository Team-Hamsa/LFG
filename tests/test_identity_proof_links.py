# tests/test_identity_proof_links.py
# wallet_proof_links (#447): an explicit, signature-proved wallet<->wallet edge
# in the bucket graph, alongside identity—wallet (wallet_links, #206) and the
# token co-observation edge (wallet_token_links, #445).

import pytest

from lfg_service import identity


@pytest.fixture
def _db(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "DATABASE", str(tmp_path / "identity.db"))
    identity.ensure_identities_table()
    return str(tmp_path / "identity.db")


def test_proof_link_merges_buckets(_db):
    identity.link("web", "rA", "rA", "rA")
    identity.link("web", "rB", "rB", "rB")
    assert identity.bucket_for_wallet("rA")["wallets"] == ["rA"]
    assert identity.link_proof("rB", "rA", "wc-signed-tx") is True
    # ordered PK -> idempotent regardless of the argument order
    assert identity.link_proof("rA", "rB", "wc-signed-tx") is False
    assert identity.bucket_for_wallet("rA")["wallets"] == ["rA", "rB"]
    assert (
        identity.bucket_for_wallet("rB")["bucket_id"]
        == identity.bucket_for_wallet("rA")["bucket_id"]
    )


def test_proof_link_reaches_wallets_known_only_by_proof(_db):
    identity.link("web", "rA", "rA", "rA")
    identity.link_proof("rA", "rZ", "wc-signed-tx")  # rZ has no identity row
    assert identity.bucket_for_wallet("rZ")["wallets"] == ["rA", "rZ"]


def test_same_wallet_rejected(_db):
    with pytest.raises(ValueError):
        identity.link_proof("rA", "rA", "x")


def test_lookup_failure_still_raises(_db, monkeypatch):
    monkeypatch.setattr(identity, "DATABASE", "/nonexistent/dir/x.db")
    with pytest.raises(identity.BucketLookupError):
        identity.bucket_for_wallet("rA")


def test_write_failure_raises_instead_of_returning_false(_db, monkeypatch):
    """False means "already linked" (a success). A DB failure must be loud, or
    a caller answers "linked" for an edge that was never written."""
    monkeypatch.setattr(identity, "DATABASE", "/nonexistent/dir/x.db")
    with pytest.raises(identity.LinkWriteError):
        identity.link_proof("rA", "rB", "wc-signed-tx")


def test_proof_link_is_transitive_across_three_wallets(_db):
    identity.link("web", "rA", "rA", "rA")
    identity.link_proof("rA", "rB", "wc-signed-tx")
    identity.link_proof("rB", "rC", "xaman-signin")
    assert identity.bucket_for_wallet("rC")["wallets"] == ["rA", "rB", "rC"]
