import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lfg_core import nft_index
from lfg_core.nft_index import OnchainNft


def _nft(nft_id, num, owner, burned=False):
    return OnchainNft(
        nft_id=nft_id,
        nft_number=num,
        owner=owner,
        is_burned=burned,
        mutable=True,
        uri_hex="",
        body="male",
        attributes=[],
        image="",
        ledger_index=1,
    )


def test_owner_live_nfts_filters_owner_and_burned():
    conn = nft_index.init_db(":memory:")
    nft_index.upsert(conn, _nft("A", 1, "rOwner"))
    nft_index.upsert(conn, _nft("B", 2, "rOther"))
    nft_index.upsert(conn, _nft("C", 3, "rOwner", burned=True))
    got = nft_index.owner_live_nfts(conn, "rOwner")
    assert [n.nft_id for n in got] == ["A"]
