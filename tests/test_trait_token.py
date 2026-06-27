from lfg_core import trait_token as tt


def test_build_and_parse_roundtrip():
    meta = tt.build_trait_metadata("Hat", "Red Cap", "https://cdn/x.png")
    assert meta["lfg_trait"] == {"slot": "Hat", "value": "Red Cap"}
    assert meta["image"] == "https://cdn/x.png"
    assert "Hat" in meta["name"] and "Red Cap" in meta["name"]
    assert tt.parse_trait_metadata(meta) == ("Hat", "Red Cap")


def test_parse_tolerates_garbage():
    assert tt.parse_trait_metadata({}) is None
    assert tt.parse_trait_metadata({"lfg_trait": {"slot": "Hat"}}) is None  # missing value
    assert tt.parse_trait_metadata({"lfg_trait": "nope"}) is None
