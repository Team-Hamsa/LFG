# tests/test_index_raw_blank.py
# #534: the on-chain index must keep a token's RAW blankness apart from its
# padded attribute list.
#
# swap_meta.normalize_nft computes "blank" from the RAW metadata attributes
# (#523 / PR #528): a genuine harvested blank lists every TRAIT_ORDER slot
# explicitly as "None", while a dressed character whose metadata attributes
# are missing, malformed, partial or value-less is NOT blank.
# nft_index.token_record pads attributes at index time
# (normalize_attributes), so all of those shapes are stored as the same
# all-"None" list a genuine blank has. The roster's cache-miss fallback
# (lfg_service.app._index_roster) rebuilt metadata from that padded list and
# called a dressed-but-corrupt token blank, which refuses its swap with
# blank_character. The index now stores raw_blank (NULL = legacy/unknown)
# computed from the RAW list, and the fallback trusts it when it is known.
import json
import sqlite3

import pytest

from lfg_core import nft_index, swap_meta, trait_economy
from lfg_service import app as server
from scripts import import_bithomp_csv

WALLET = "rWallet"
_NAME = "Let's Effing Go! #12"
_DRESSED = [
    {"trait_type": "Background", "value": "Pastel Aqua"},
    {"trait_type": "Body", "value": "Straight Wood"},
    {"trait_type": "Head", "value": "Egg Head"},
]


def _token(nft_id="0019" + "A" * 60, uri_hex="68747470733a2f2f63646e2f31322e6a736f6e"):
    # 0x0019 = burnable + transferable + mutable (the NFTokenID's flag bytes)
    return {
        "nft_id": nft_id,
        "owner": WALLET,
        "is_burned": False,
        "flags": 0x19,
        "uri_hex": uri_hex,
        "ledger_index": 100,
    }


def _meta(**attributes):
    """Metadata that parses as a dict; `attributes=` is copied in verbatim
    when given, and the key is left out entirely when not."""
    meta = {"name": _NAME, "image": "https://cdn/12.png"}
    meta.update(attributes)
    return meta


def _blank_without(slot):
    return [a for a in trait_economy.blank_attributes() if a["trait_type"] != slot]


def _blank_valueless(slot):
    return [
        {"trait_type": a["trait_type"]} if a["trait_type"] == slot else a
        for a in trait_economy.blank_attributes()
    ]


# Metadata shapes that PARSE but do not prove a blank. After
# normalize_attributes() pads them, each one is byte-identical to a genuine
# blank's attribute list -- the ambiguity raw_blank exists to resolve.
_NOT_PROVEN_BLANK = {
    "attributes_missing": _meta(),
    "attributes_not_a_list": _meta(attributes={"Body": "None"}),
    "attributes_a_string": _meta(attributes="None"),
    "partial_all_none": _meta(attributes=_blank_without("Back")),
    "valueless_entry": _meta(attributes=_blank_valueless("Head")),
}


# --- token_record: raw blankness is computed before padding ----------------


@pytest.mark.parametrize("meta", _NOT_PROVEN_BLANK.values(), ids=_NOT_PROVEN_BLANK.keys())
def test_token_record_unproven_blank_metadata_is_not_raw_blank(meta):
    rec = nft_index.token_record(_token(), meta)
    genuine = nft_index.token_record(_token(), _meta(attributes=trait_economy.blank_attributes()))
    # The padded list really is indistinguishable from a genuine blank's ...
    assert rec.attributes == genuine.attributes
    # ... so only the raw flag can tell them apart.
    assert rec.raw_blank is False


def test_token_record_full_explicit_blank_is_raw_blank():
    rec = nft_index.token_record(_token(), _meta(attributes=trait_economy.blank_attributes()))
    assert rec.raw_blank is True


def test_token_record_dressed_is_not_raw_blank():
    rec = nft_index.token_record(_token(), _meta(attributes=_DRESSED))
    assert rec.raw_blank is False


def test_token_record_unreadable_metadata_leaves_raw_blank_unknown():
    """No metadata at all (fetch failed / ipfs never fetched): nothing is
    known about the token's attributes, so the flag stays NULL rather than
    claiming "not blank" for a token that might be a genuine blank."""
    rec = nft_index.token_record(_token(), None)
    assert rec.attributes == []
    assert rec.raw_blank is None


def test_token_record_uses_normalize_nfts_predicate():
    """One predicate, not two copies: the index flag and the roster's
    cache-hit path (normalize_nft) must agree on every shape."""
    shapes = [*_NOT_PROVEN_BLANK.values(), _meta(attributes=trait_economy.blank_attributes())]
    for meta in shapes:
        record = swap_meta.normalize_nft(_token()["nft_id"], meta, flags=0x19)
        assert record is not None
        assert nft_index.token_record(_token(), meta).raw_blank is record["blank"]


# --- storage: round trip, clobber guard, self-migration --------------------


def _stored_flag(conn, nft_id):
    rec = nft_index.nft_by_id(conn, nft_id)
    assert rec is not None
    return rec.raw_blank


@pytest.mark.parametrize("flag", [True, False, None])
def test_raw_blank_round_trips_through_upsert(tmp_path, flag):
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    rec = nft_index.token_record(_token(), _meta(attributes=_DRESSED))
    rec.raw_blank = flag
    nft_index.upsert(conn, rec)
    assert _stored_flag(conn, rec.nft_id) is flag


@pytest.mark.parametrize(
    "attributes", [trait_economy.blank_attributes(), _DRESSED], ids=["blank", "dressed"]
)
def test_unreadable_refresh_never_erases_known_raw_blank(tmp_path, attributes):
    """#229/#233 clobber guard: a re-index whose metadata fetch failed
    (attributes [] and flag NULL) must keep the known flag, exactly as it
    keeps attributes/body/image/video."""
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    good = nft_index.token_record(_token(), _meta(attributes=attributes))
    nft_index.upsert(conn, good)
    nft_index.upsert(conn, nft_index.token_record(_token(), None))
    assert _stored_flag(conn, good.nft_id) is good.raw_blank
    assert nft_index.nft_by_id(conn, good.nft_id).attributes == good.attributes


def test_readable_refresh_replaces_raw_blank_both_ways(tmp_path):
    """The flag describes the attributes stored beside it: a modify that
    blanks a character (harvest) or dresses one (assemble) must move it."""
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_id = _token()["nft_id"]
    nft_index.upsert(conn, nft_index.token_record(_token(), _meta(attributes=_DRESSED)))
    assert _stored_flag(conn, nft_id) is False
    blanked = _meta(attributes=trait_economy.blank_attributes())
    nft_index.upsert(conn, nft_index.token_record(_token(), blanked))
    assert _stored_flag(conn, nft_id) is True
    nft_index.upsert(conn, nft_index.token_record(_token(), _meta(attributes=_DRESSED)))
    assert _stored_flag(conn, nft_id) is False


def test_readable_write_without_flag_resets_to_unknown(tmp_path):
    """A writer that replaces the attributes without knowing their raw form
    (raw_blank=None) must not leave the OLD attributes' flag behind: the row
    falls back to deriving from what is now stored, as before #534."""
    conn = nft_index.init_db(str(tmp_path / "x.db"))
    nft_id = _token()["nft_id"]
    nft_index.upsert(conn, nft_index.token_record(_token(), _meta(attributes=_DRESSED)))
    unknown = nft_index.token_record(_token(), _meta(attributes=trait_economy.blank_attributes()))
    unknown.raw_blank = None
    nft_index.upsert(conn, unknown)
    assert _stored_flag(conn, nft_id) is None


def _legacy_db(path):
    """An index DB from before #534 (post-#204: has video, no raw_blank)."""
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE onchain_nfts ("
        " nft_id TEXT PRIMARY KEY, nft_number INTEGER, owner TEXT,"
        " is_burned INTEGER DEFAULT 0, mutable INTEGER, uri_hex TEXT,"
        " body TEXT, attributes_json TEXT, image TEXT, video TEXT,"
        " ledger_index INTEGER, last_synced_at TIMESTAMP)"
    )
    legacy.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, attributes_json, image)"
        " VALUES (?, 12, ?, ?, 'https://cdn/blank.png')",
        ("L" * 64, WALLET, json.dumps(trait_economy.blank_attributes())),
    )
    legacy.commit()
    legacy.close()


def test_init_db_adds_raw_blank_to_a_legacy_table_idempotently(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    conn = nft_index.init_db(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(onchain_nfts)")}
    assert "raw_blank" in cols
    # Rows indexed before the fix stay unknown -- never backfilled to a guess.
    assert _stored_flag(conn, "L" * 64) is None
    conn.close()
    again = nft_index.init_db(path)  # second run: no ALTER, no error
    cols_again = [r[1] for r in again.execute("PRAGMA table_info(onchain_nfts)")]
    assert cols_again.count("raw_blank") == 1
    rec = nft_index.token_record(
        _token(nft_id="L" * 64), _meta(attributes=trait_economy.blank_attributes())
    )
    nft_index.upsert(again, rec)
    assert _stored_flag(again, "L" * 64) is True


def test_raw_blank_migration_race_duplicate_column_is_tolerated(tmp_path, monkeypatch):
    """Service + listener racing the check-then-ALTER: the loser's
    duplicate-column error must not break initialization (same tolerance as
    the #204 video column)."""
    path = str(tmp_path / "race.db")
    nft_index.init_db(path).close()  # column now exists

    class RacedConn:
        """Hides raw_blank from PRAGMA table_info (a stale view), so init_db
        takes the ALTER branch against a DB that already has the column."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a):
            cur = self._real.execute(sql, *a)
            if sql.startswith("PRAGMA table_info"):
                return iter([r for r in cur.fetchall() if r[1] != "raw_blank"])
            return cur

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_connect = nft_index.sqlite3.connect
    monkeypatch.setattr(
        nft_index.sqlite3, "connect", lambda p, *a, **k: RacedConn(real_connect(p, *a, **k))
    )
    conn = nft_index.init_db(path)  # would raise "duplicate column" unguarded
    cols = {r[1] for r in conn._real.execute("PRAGMA table_info(onchain_nfts)")}
    assert "raw_blank" in cols
    conn._real.close()


def test_row_without_raw_blank_column_reads_as_unknown():
    """Hand-built schemas (and connections opened elsewhere) may lack the
    column entirely; readers must treat that as unknown, not crash."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE onchain_nfts (nft_id TEXT PRIMARY KEY, nft_number INTEGER,"
        " owner TEXT, is_burned INTEGER, mutable INTEGER, uri_hex TEXT, body TEXT,"
        " attributes_json TEXT, image TEXT, ledger_index INTEGER)"
    )
    conn.execute(
        "INSERT INTO onchain_nfts VALUES ('X', 12, ?, 0, 1, '', 'male', '[]', '', 1)", (WALLET,)
    )
    assert nft_index.nft_by_id(conn, "X").raw_blank is None


# --- the roster's metadata-cache-miss fallback -----------------------------


def _roster(conn):
    nfts = server._index_roster(conn, WALLET)
    assert nfts is not None
    return {n["nft_id"]: n for n in nfts}


def test_roster_cache_miss_dressed_token_with_missing_attributes_is_not_blank(tmp_path):
    """THE #534 bug: a dressed character whose metadata parses but carries no
    `attributes`, indexed by the real listener/backfill path and missing the
    uri metadata cache, must not present as blank -- that would refuse its
    swap with blank_character."""
    conn = nft_index.init_db(str(tmp_path / "onchain.db"))
    rec = nft_index.token_record(_token(), _meta())
    nft_index.upsert(conn, rec)  # no meta_cache_put_many: a cache miss
    record = _roster(conn)[rec.nft_id]
    assert all(a["value"] == "None" for a in record["attributes"])  # padded
    assert record["blank"] is False


@pytest.mark.parametrize("meta", _NOT_PROVEN_BLANK.values(), ids=_NOT_PROVEN_BLANK.keys())
def test_roster_cache_miss_agrees_with_cache_hit(tmp_path, meta):
    """Whether or not the metadata cache holds the token, the roster must
    reach the same verdict."""
    conn = nft_index.init_db(str(tmp_path / "onchain.db"))
    rec = nft_index.token_record(_token(), meta)
    nft_index.upsert(conn, rec)
    miss = _roster(conn)[rec.nft_id]["blank"]
    nft_index.meta_cache_put_many(conn, {rec.uri_hex: meta})
    hit = _roster(conn)[rec.nft_id]["blank"]
    assert miss is hit is False


def test_roster_cache_miss_genuine_blank_is_blank(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "onchain.db"))
    rec = nft_index.token_record(_token(), _meta(attributes=trait_economy.blank_attributes()))
    nft_index.upsert(conn, rec)
    assert rec.raw_blank is True
    assert _roster(conn)[rec.nft_id]["blank"] is True


def test_roster_cache_miss_legacy_row_keeps_todays_derivation(tmp_path):
    """A row indexed before #534 has raw_blank NULL. Its padded all-"None"
    attributes must still read as blank: the live mainnet blanks (all of
    which miss the cache) would otherwise slip past the #523 guard."""
    conn = nft_index.init_db(str(tmp_path / "onchain.db"))
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, uri_hex,"
        " attributes_json, image) VALUES (?, 12, ?, 0, ?, ?, 'https://cdn/blank.png')",
        (
            _token()["nft_id"],
            WALLET,
            _token()["uri_hex"],
            json.dumps(trait_economy.blank_attributes()),
        ),
    )
    conn.commit()
    assert _stored_flag(conn, _token()["nft_id"]) is None
    assert _roster(conn)[_token()["nft_id"]]["blank"] is True


def test_roster_cache_miss_legacy_dressed_row_stays_not_blank(tmp_path):
    conn = nft_index.init_db(str(tmp_path / "onchain.db"))
    conn.execute(
        "INSERT INTO onchain_nfts (nft_id, nft_number, owner, is_burned, uri_hex,"
        " attributes_json, image) VALUES (?, 12, ?, 0, ?, ?, 'https://cdn/12.png')",
        (_token()["nft_id"], WALLET, _token()["uri_hex"], json.dumps(_DRESSED)),
    )
    conn.commit()
    assert _roster(conn)[_token()["nft_id"]]["blank"] is False


# --- other index writers carry the flag ------------------------------------

_BLANK_CSV_ROW = {
    "NFT ID": "00BLANK",
    "Name": "Let's Effing Go! #12",
    "URI": "https://cdn/blank/12.json",
    **{f"Attribute {slot}": "None" for slot in swap_meta.TRAIT_ORDER},
}


def test_csv_record_genuine_blank_row_is_raw_blank():
    assert import_bithomp_csv.csv_record(_BLANK_CSV_ROW).raw_blank is True


def test_csv_record_dressed_row_is_not_raw_blank():
    row = {**_BLANK_CSV_ROW, "Attribute Body": "Straight Wood", "Attribute Head": "Egg Head"}
    assert import_bithomp_csv.csv_record(row).raw_blank is False


def test_csv_record_row_missing_a_slot_column_is_not_raw_blank():
    """Judged on the raw columns, not the padded list: an export with no
    `Attribute Back` column has not shown this token's Back to be "None"."""
    row = {k: v for k, v in _BLANK_CSV_ROW.items() if k != "Attribute Back"}
    rec = import_bithomp_csv.csv_record(row)
    assert all(a["value"] == "None" for a in rec.attributes)  # padding hides it
    assert rec.raw_blank is False
