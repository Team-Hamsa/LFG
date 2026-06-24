import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

from lfg_core import nft_index, economy_store
from lfg_core.nft_index import OnchainNft
from webapp import economy_api


def _seed_conn():
    conn = nft_index.init_db(":memory:")
    economy_store.init_economy_schema(conn)
    nft_index.upsert(conn, OnchainNft(
        nft_id="A", nft_number=3537, owner="rOwner", is_burned=False, mutable=True,
        uri_hex="", body="male",
        attributes=[{"trait_type": "Head", "value": "Crown"}],
        image="https://cdn.example/3537.png", ledger_index=1))
    economy_store.set_bucket_contents(conn, "rOwner", [("Head", "Halo", 2)], [42])
    return conn


def test_read_economy_state_shape():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rOwner")
    assert state["characters"][0]["edition"] == 3537
    assert state["characters"][0]["attributes"][0]["value"] == "Crown"
    assert state["characters"][0]["image_url"] == "https://cdn.example/3537.png"
    assert state["characters"][0]["mutable"] is True
    assert state["bucket"]["assets"][0] == {"slot": "Head", "value": "Halo", "count": 2}
    assert state["bucket"]["bodies"] == [42]
    assert state["trait_order"][0] == "Background"
    assert "Body" not in state["slots"]


def test_read_economy_state_excludes_other_owners():
    conn = _seed_conn()
    state = economy_api.read_economy_state(conn, "rNobody")
    assert state["characters"] == []
    assert state["bucket"]["assets"] == []
    assert state["bucket"]["bodies"] == []
