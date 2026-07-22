import os
import sqlite3

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import lfg_core.free_mint as free_mint  # noqa: E402
import lfg_core.nft_index as nft_index  # noqa: E402
import lfg_core.user_db as user_db  # noqa: E402
import lfg_service.identity as identity  # noqa: E402


def _setup(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(user_db, "DATABASE", str(db))
    monkeypatch.setattr(identity, "DATABASE", str(db))
    monkeypatch.setattr(free_mint, "DATABASE", str(db))
    identity.ensure_identities_table()
    free_mint.ensure_tables()
    # point ownership lookups at a controlled index db
    idx = tmp_path / "onchain_testnet.db"
    monkeypatch.setattr(nft_index, "index_db_path", lambda network: str(idx))
    conn = nft_index.init_db(str(idx))
    conn.close()
    return str(db), str(idx)


def _own(idx, owner, nft_id="00A", burned=0):
    conn = sqlite3.connect(idx)
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, owner, is_burned) VALUES (?, ?, ?)",
        (nft_id, owner, burned),
    )
    conn.commit()
    conn.close()


def test_newcomer_is_eligible(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    assert free_mint.is_eligible("discord", "u1", "testnet") is True


def test_owner_not_eligible(tmp_path, monkeypatch):
    _db, idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    _own(idx, "rA")
    assert free_mint.is_eligible("discord", "u1", "testnet") is False


def test_owner_under_historical_wallet_not_eligible(tmp_path, monkeypatch):
    _db, idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rOLD")
    identity.link("discord", "u1", "alice", "rNEW")  # switched; still owns via rOLD
    _own(idx, "rOLD")
    assert free_mint.is_eligible("discord", "u1", "testnet") is False


def test_reserved_or_claimed_not_eligible(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    assert free_mint.reserve_claim("discord", "u1", "testnet", "rA") is True
    assert free_mint.is_eligible("discord", "u1", "testnet") is False


def test_reserve_is_single_winner(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    first = free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    second = free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    assert (first, second) == (True, False)


def test_release_restores_eligibility(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    free_mint.release_claim("discord", "u1", "testnet")
    assert free_mint.is_eligible("discord", "u1", "testnet") is True


def test_confirm_blocks_and_records(tmp_path, monkeypatch):
    db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    free_mint.reserve_claim("discord", "u1", "testnet", "rA")
    free_mint.confirm_claim("discord", "u1", "testnet", "rA", 4242)
    assert free_mint.is_eligible("discord", "u1", "testnet") is False
    row = (
        sqlite3.connect(db)
        .execute(
            "SELECT status, nft_number FROM free_mint_claims "
            "WHERE platform='discord' AND platform_user_id='u1'"
        )
        .fetchone()
    )
    assert row == ("claimed", 4242)


def test_missing_index_fails_closed(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    identity.link("discord", "u1", "alice", "rA")
    monkeypatch.setattr(nft_index, "index_db_path", lambda network: str(tmp_path / "nope.db"))
    assert free_mint.is_eligible("discord", "u1", "testnet") is False


def test_cap_blocks_new_reservations(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(free_mint.config, "FREE_MINT_CAP", 2)
    # two identities claim the whole giveaway
    assert free_mint.reserve_claim("discord", "a", "testnet", "rA") is True
    assert free_mint.reserve_claim("discord", "b", "testnet", "rB") is True
    # third is refused by the atomic cap, even though it's a fresh identity
    identity.link("discord", "c", "carol", "rC")
    assert free_mint.reserve_claim("discord", "c", "testnet", "rC") is False
    # and is_eligible reflects the exhausted cap up front
    assert free_mint.is_eligible("discord", "c", "testnet") is False
    assert free_mint.active_claim_count("testnet") == 2


def test_release_frees_a_cap_slot(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(free_mint.config, "FREE_MINT_CAP", 1)
    assert free_mint.reserve_claim("discord", "a", "testnet", "rA") is True
    assert free_mint.reserve_claim("discord", "b", "testnet", "rB") is False  # full
    free_mint.release_claim("discord", "a", "testnet")  # a backs out
    assert free_mint.active_claim_count("testnet") == 0
    assert free_mint.reserve_claim("discord", "b", "testnet", "rB") is True  # slot freed


def test_confirmed_claim_still_counts_toward_cap(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(free_mint.config, "FREE_MINT_CAP", 1)
    free_mint.reserve_claim("discord", "a", "testnet", "rA")
    free_mint.confirm_claim("discord", "a", "testnet", "rA", 7)
    assert free_mint.active_claim_count("testnet") == 1
    assert free_mint.reserve_claim("discord", "b", "testnet", "rB") is False


def test_cap_is_per_network(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(free_mint.config, "FREE_MINT_CAP", 1)
    assert free_mint.reserve_claim("discord", "a", "testnet", "rA") is True
    # mainnet has its own budget
    assert free_mint.reserve_claim("discord", "a2", "mainnet", "rA2") is True
    assert free_mint.active_claim_count("testnet") == 1
    assert free_mint.active_claim_count("mainnet") == 1


def test_cap_zero_disables_giveaway(tmp_path, monkeypatch):
    _db, _idx = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(free_mint.config, "FREE_MINT_CAP", 0)
    identity.link("discord", "a", "alice", "rA")
    assert free_mint.is_eligible("discord", "a", "testnet") is False
    assert free_mint.reserve_claim("discord", "a", "testnet", "rA") is False


def test_concurrent_reservations_never_exceed_cap(tmp_path, monkeypatch):
    # The money invariant: under a stampede of concurrent reservers at the
    # boundary, EXACTLY FREE_MINT_CAP win — never more. Proves the
    # BEGIN IMMEDIATE count-then-insert is atomic across threads.
    import concurrent.futures

    _db, _idx = _setup(tmp_path, monkeypatch)
    cap = 5
    monkeypatch.setattr(free_mint.config, "FREE_MINT_CAP", cap)

    def _try(i):
        return free_mint.reserve_claim("discord", f"u{i}", "testnet", f"r{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        results = list(ex.map(_try, range(24)))

    assert sum(1 for r in results if r) == cap
    assert free_mint.active_claim_count("testnet") == cap
