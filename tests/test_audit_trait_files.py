# tests/test_audit_trait_files.py
# Reconciliation of every stored trait value against the local layer tree
# (issue #137). The audit must reproduce the swap path's resolver exactly
# (own dir -> shared -> matrix-permitted foreign + ape structural extras) and
# sweep the LFG table, the on-chain index, and the loose-trait economy stores.
#
# Env-guard preamble (config freezes constants at import time — set the same
# defaults test_swap_compose.py uses so collection order can't strand them).
import os

os.environ.setdefault("XUMM_API_KEY", "test")
os.environ.setdefault("XUMM_API_SECRET", "test")
os.environ.setdefault("SEED", "sEdTM1uX8pu2do5XvTnutH6HsouMaM2")
os.environ.setdefault("TOKEN_ISSUER_ADDRESS", "rrrrrrrrrrrrrrrrrrrrrhoLvTp")
os.environ.setdefault("TOKEN_CURRENCY_HEX", "4C46474F00000000000000000000000000000000")
os.environ.setdefault("BUNNY_CDN_ACCESS_KEY", "test")
os.environ.setdefault("BUNNY_CDN_STORAGE_ZONE", "test")
os.environ.setdefault("LAYER_SOURCE", "local")
os.environ.setdefault("BUNNY_PULL_ZONE", "nft.pullzone.example")

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import audit_trait_files as atf  # noqa: E402

from lfg_core import economy_store, layer_store, nft_index  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_layers(tmp_path, tree):
    """tree = {body: {slot: [values]}}; writes <body>/<slot>/<value>.png."""
    root = tmp_path / "layers"
    for body, slots in tree.items():
        for slot, values in slots.items():
            d = root / body / slot
            d.mkdir(parents=True, exist_ok=True)
            for v in values:
                (d / f"{v}.png").write_bytes(b"x")
    return layer_store.LocalLayerStore(str(root))


def _char(attrs_map, body, ref="e1", source="onchain"):
    attrs = [{"trait_type": k, "value": v} for k, v in attrs_map.items()]
    return atf.CharacterRecord(source, ref, body, attrs)


# --- pure helpers -----------------------------------------------------------


def test_lfg_row_maps_hat_to_head_and_drops_empty():
    row = {"Background": "Blue", "Hat": "Wizard", "Eyes": "None", "Mouth": "", "Body": "Straight"}
    cols = set(atf.LFG_COLUMN_TO_SLOT)
    attrs = atf._attrs_from_lfg_row(row, cols)
    by_type = {a["trait_type"]: a["value"] for a in attrs}
    assert by_type["Head"] == "Wizard"  # Hat column -> Head slot
    assert by_type["Background"] == "Blue"
    # 'None' / '' values are not carried as real values (normalize fills them 'None')
    assert by_type["Eyes"] == "None" and by_type["Mouth"] == "None"


def test_parse_missing_path_character_and_structural():
    assert atf._parse_missing_path("male/Head/Wizard Hat") == (
        "male",
        "Head",
        "Wizard Hat",
        "character",
    )
    # value may itself contain a slash — everything after slot is the value
    assert atf._parse_missing_path("male/Head/Top/Bit") == ("male", "Head", "Top/Bit", "character")
    assert atf._parse_missing_path("ape/Nose.png") == (
        "ape",
        "(structural)",
        "Nose.png",
        "structural",
    )


# --- run_audit: characters --------------------------------------------------


def test_clean_character_has_no_missing(tmp_path):
    store = _make_layers(
        tmp_path, {"male": {"Background": ["Blue"], "Head": ["Cap"], "Body": ["Straight Body"]}}
    )
    rec = _char({"Body": "Straight Body", "Background": "Blue", "Head": "Cap"}, "male")
    res = _run(atf.run_audit([rec], [], store))
    assert res.ok and res.missing == []


def test_missing_character_value_reported_with_ref_and_absent_everywhere(tmp_path):
    store = _make_layers(tmp_path, {"male": {"Background": ["Blue"], "Body": ["Straight Body"]}})
    rec = _char(
        {"Body": "Straight Body", "Background": "Blue", "Head": "Ghostface"}, "male", ref="4321"
    )
    res = _run(atf.run_audit([rec], [], store))
    assert not res.ok
    (entry,) = res.missing
    assert entry.slot == "Head" and entry.value == "Ghostface"
    assert entry.missing_bodies == {"male"} and entry.refs == ["4321"]
    assert entry.resolved_bodies == []  # art absent everywhere -> needs art


def test_resolved_bodies_reports_where_art_lives(tmp_path):
    # The backfill-hint helper: which bodies' own dir (or the shared fallback)
    # provide a given (slot, value). Used to annotate a gap with where the art
    # actually is, independent of the swap matrix.
    store = _make_layers(
        tmp_path,
        {
            "male": {"Head": ["Cap"]},
            "female": {"Head": ["Cap", "Wings"]},
            "shared": {"Accessory": ["Chain"]},
        },
    )
    assert _run(atf.resolved_bodies(store, "Head", "Cap")) == ["female", "male"]
    assert _run(atf.resolved_bodies(store, "Head", "Wings")) == ["female"]
    # A shared value resolves under every body via the shared fallback in resolve.
    assert _run(atf.resolved_bodies(store, "Accessory", "Chain")) == ["female", "male"]
    assert _run(atf.resolved_bodies(store, "Head", "Nonexistent")) == []


def test_shared_value_resolves_no_gap(tmp_path):
    store = _make_layers(
        tmp_path,
        {
            "male": {"Background": ["Blue"], "Body": ["Straight Body"]},
            "shared": {"Accessory": ["Chain"]},
        },
    )
    rec = _char({"Body": "Straight Body", "Background": "Blue", "Accessory": "Chain"}, "male")
    res = _run(atf.run_audit([rec], [], store))
    assert res.ok


def test_ape_structural_nose_reported(tmp_path):
    # An ape with no Nose.png structural asset -> a structural gap surfaces.
    store = _make_layers(tmp_path, {"ape": {"Background": ["Blue"], "Body": ["Ape Body"]}})
    rec = _char({"Body": "Ape Body", "Background": "Blue"}, "ape")
    res = _run(atf.run_audit([rec], [], store))
    paths = {e.path for e in res.missing}
    assert "ape/Nose.png" in paths
    nose = next(e for e in res.missing if e.path == "ape/Nose.png")
    assert nose.kind == "structural"


def test_duplicate_gaps_aggregate_across_records(tmp_path):
    store = _make_layers(tmp_path, {"male": {"Background": ["Blue"], "Body": ["Straight Body"]}})
    recs = [
        _char({"Body": "Straight Body", "Background": "Blue", "Head": "X"}, "male", ref="1"),
        _char({"Body": "Straight Body", "Background": "Blue", "Head": "X"}, "male", ref="2"),
    ]
    res = _run(atf.run_audit(recs, [], store))
    (entry,) = res.missing
    assert entry.count == 2 and set(entry.refs) == {"1", "2"}


# --- run_audit: loose traits ------------------------------------------------


def test_loose_trait_missing_everywhere_reported(tmp_path):
    store = _make_layers(tmp_path, {"male": {"Head": ["Cap"]}})
    loose = [atf.LooseTrait("trait_token", "rOwner", "Head", "Sombrero", ref="00NFT")]
    res = _run(atf.run_audit([], loose, store))
    (entry,) = res.missing
    assert entry.kind == "loose" and entry.value == "Sombrero" and entry.refs == ["00NFT"]


def test_loose_trait_present_somewhere_ok(tmp_path):
    store = _make_layers(tmp_path, {"male": {"Head": ["Cap"]}})
    loose = [atf.LooseTrait("closet", "rOwner", "Head", "Cap")]
    res = _run(atf.run_audit([], loose, store))
    assert res.ok


def test_loose_none_value_ignored(tmp_path):
    store = _make_layers(tmp_path, {"male": {"Head": ["Cap"]}})
    loose = [atf.LooseTrait("closet", "rOwner", "Head", "None")]
    res = _run(atf.run_audit([], loose, store))
    assert res.ok


def test_unreadable_passthrough_not_a_gap(tmp_path):
    store = _make_layers(tmp_path, {"male": {"Background": ["Blue"]}})
    res = _run(atf.run_audit([], [], store, unreadable=["00AA", "00BB"]))
    assert res.ok and res.unreadable == ["00AA", "00BB"]


# --- collectors -------------------------------------------------------------


def _lfg_db(tmp_path):
    path = tmp_path / "lfg_nfts.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE LFG (nft_number INTEGER PRIMARY KEY, nft_id TEXT, Background TEXT, "
        "Body TEXT, Hat TEXT, Eyes TEXT, network TEXT, body_type TEXT)"
    )
    conn.execute(
        "INSERT INTO LFG VALUES (10, '000A', 'Blue', 'Straight Body', 'Wizard', 'Laser', "
        "'testnet', 'male')"
    )
    conn.execute(
        "INSERT INTO LFG VALUES (11, '000B', 'Red', 'Ape Body', 'Crown', 'Sleepy', "
        "'mainnet', 'ape')"
    )
    # Phantom never-minted draft (nft_id NULL) — must be skipped.
    conn.execute(
        "INSERT INTO LFG VALUES (12, NULL, 'Blue', 'Straight Body', 'Ghostface', 'Laser', "
        "'testnet', 'male')"
    )
    conn.commit()
    conn.close()
    return str(path)


def test_collect_lfg_filters_network_and_maps_head(tmp_path):
    db = _lfg_db(tmp_path)
    recs = atf.collect_lfg_records(db, "testnet")
    # edition 10 only: 11 is mainnet, 12 is a phantom (NULL nft_id) draft.
    assert [r.ref for r in recs] == ["10"]
    rec = recs[0]
    assert rec.source == "LFG" and rec.body == "male"
    head = next(a for a in rec.attributes if a["trait_type"] == "Head")
    assert head["value"] == "Wizard"


def test_collect_lfg_missing_db_returns_empty(tmp_path):
    assert atf.collect_lfg_records(str(tmp_path / "nope.db"), "testnet") == []


def _onchain_db(tmp_path):
    path = str(tmp_path / "onchain_testnet.db")
    conn = nft_index.init_db(path)
    economy_store.init_economy_schema(conn)
    nft_index.upsert(
        conn,
        nft_index.OnchainNft(
            nft_id="00NFT1",
            nft_number=10,
            owner="rA",
            is_burned=False,
            mutable=True,
            uri_hex="",
            body="male",
            attributes=[{"trait_type": "Head", "value": "Wizard"}],
            image="",
            ledger_index=1,
        ),
    )
    nft_index.upsert(
        conn,
        nft_index.OnchainNft(
            nft_id="00NFT2",
            nft_number=11,
            owner="rB",
            is_burned=False,
            mutable=True,
            uri_hex="",
            body="male",
            attributes=[],  # unreadable metadata
            image="",
            ledger_index=2,
        ),
    )
    conn.execute("INSERT INTO closet_assets VALUES ('rA', 'Head', 'Sombrero', 1)")
    conn.execute("INSERT INTO trait_tokens VALUES ('00TOK', 'rB', 'Head', 'Crown')")
    conn.commit()
    return conn


def test_collect_onchain_splits_unreadable(tmp_path):
    conn = _onchain_db(tmp_path)
    try:
        recs, unreadable = atf.collect_onchain_records(conn)
    finally:
        conn.close()
    assert [r.ref for r in recs] == ["00NFT1"]
    assert unreadable == ["00NFT2"]


def test_collect_loose_traits(tmp_path):
    conn = _onchain_db(tmp_path)
    try:
        loose = atf.collect_loose_traits(conn)
    finally:
        conn.close()
    kinds = {(lt.source, lt.slot, lt.value) for lt in loose}
    assert ("closet", "Head", "Sombrero") in kinds
    assert ("trait_token", "Head", "Crown") in kinds


def test_format_report_clean_and_dirty(tmp_path):
    clean = atf.AuditResult(missing=[], unreadable=[], character_count=3, loose_count=1)
    assert "0 missing" in atf.format_report(clean, "testnet", "2026-07-07T00:00:00Z")
    entry = atf.MissingEntry(
        "male/Head/X", "Head", "X", "character", {"male"}, {"onchain"}, ["9"], 1, []
    )
    dirty = atf.AuditResult(missing=[entry], unreadable=[], character_count=1, loose_count=0)
    out = atf.format_report(dirty, "testnet", "2026-07-07T00:00:00Z")
    assert "1 distinct missing" in out and "male/Head/X" in out
