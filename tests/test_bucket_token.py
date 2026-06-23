# Bucket NFToken metadata builder/parser round-trips (pure).

from lfg_core import bucket_token as bt


def test_metadata_roundtrips():
    assets = [("Head", "None", 3), ("Background", "Blue", 1)]
    bodies = [3536, 12]
    meta = bt.build_bucket_metadata("rUser", assets, bodies)
    assert meta["lfg_bucket"]["bodies"] == [12, 3536]  # sorted
    assert meta["name"] == "LFG Bucket — rUser"
    got_assets, got_bodies = bt.parse_bucket_metadata(meta)
    assert sorted(got_assets) == sorted(assets)
    assert got_bodies == [12, 3536]


def test_none_assets_preserved():
    meta = bt.build_bucket_metadata("rUser", [("Head", "None", 2)], [])
    got_assets, got_bodies = bt.parse_bucket_metadata(meta)
    assert got_assets == [("Head", "None", 2)]
    assert got_bodies == []


def test_empty_bucket():
    meta = bt.build_bucket_metadata("rUser", [], [])
    assert bt.parse_bucket_metadata(meta) == ([], [])


def test_parse_tolerates_garbage():
    assert bt.parse_bucket_metadata({}) == ([], [])
    assert bt.parse_bucket_metadata({"lfg_bucket": "x"}) == ([], [])
    assert bt.parse_bucket_metadata({"lfg_bucket": {"assets": "x"}}) == ([], [])
    # malformed entries are skipped, valid ones kept
    mixed = {
        "lfg_bucket": {
            "assets": [{"slot": "Head"}, {"slot": "Eyes", "value": "Blue", "count": 1}],
            "bodies": ["x", 7],
        }
    }
    assert bt.parse_bucket_metadata(mixed) == ([("Eyes", "Blue", 1)], [7])
